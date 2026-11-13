"""API 테스트. 작은 합성 DB 를 임시 폴더에 만들어 쓴다.

실제 파이프라인 산출물과 맞춰 보려면 VEIL_TEST_DATA_DIR 에 그 폴더를 지정한다
(queries.npy, query_ids.csv, plaintext_topk.csv 가 함께 있어야 한다).
"""
from __future__ import annotations

import base64
import csv
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.store import StoreError, load_store  # noqa: E402

RECORDS, DIM = 40, 8


def unit(rows: np.ndarray) -> np.ndarray:
    return rows / np.linalg.norm(rows, axis=-1, keepdims=True)


def write_data(folder: Path, vectors: np.ndarray, ids: list[str]) -> None:
    np.save(folder / "database.npy", vectors)
    (folder / "database_ids.csv").write_text("record_id\n" + "\n".join(ids) + "\n", encoding="utf-8")
    (folder / "metadata.json").write_text(json.dumps({"dataset": "synthetic"}), encoding="utf-8")


@pytest.fixture()
def data(tmp_path: Path):
    vectors = unit(np.random.default_rng(7).normal(size=(RECORDS, DIM)))
    ids = [f"T{i:03d}" for i in range(1, RECORDS + 1)]
    write_data(tmp_path, vectors, ids)
    return tmp_path, vectors, ids


@pytest.fixture()
def client(data):
    folder, _, _ = data
    with TestClient(create_app(Settings(data_dir=folder))) as test_client:
        yield test_client


def query(vector, top_k=5) -> dict:
    return {"scheme": "plaintext", "vector": [float(x) for x in vector], "top_k": top_k}


def test_health_and_meta(client):
    assert client.get("/health").json() == {"ok": True}
    meta = client.get("/v1/meta").json()
    assert meta["dataset"] == {"name": "synthetic", "records": RECORDS, "dimension": DIM}
    assert {s["name"]: s["connected"] for s in meta["schemes"]} == {"plaintext": True, "ckks-hec3": False}


def test_search_matches_brute_force(client, data):
    _, vectors, ids = data
    q = unit(np.random.default_rng(1).normal(size=DIM))
    body = client.post("/v1/search", json=query(q, top_k=5)).json()

    scores = vectors @ q
    expected = np.argsort(-scores, kind="stable")[:5]
    assert [r["record_id"] for r in body["results"]] == [ids[i] for i in expected]
    assert [r["rank"] for r in body["results"]] == [1, 2, 3, 4, 5]
    assert body["results"][0]["score"] == pytest.approx(float(scores[expected[0]]), abs=1e-8)
    assert body["encrypted"] is False and body["searched"] == RECORDS and body["top_k"] == 5


def test_registered_vector_finds_itself(client, data):
    _, vectors, ids = data
    top = client.post("/v1/search", json=query(vectors[11], top_k=1)).json()["results"][0]
    assert top["record_id"] == ids[11] and top["score"] == pytest.approx(1.0)


def test_top_k_larger_than_database_returns_all(tmp_path):
    write_data(tmp_path, unit(np.eye(3)), ["A", "B", "C"])
    with TestClient(create_app(Settings(data_dir=tmp_path))) as c:
        body = c.post("/v1/search", json=query([1.0, 0.0, 0.0], top_k=10)).json()
    assert body["top_k"] == 3 and [r["record_id"] for r in body["results"]] == ["A", "B", "C"]


@pytest.mark.parametrize(
    "body",
    [
        query(unit(np.ones(DIM - 1))),                      # 차원이 다름
        query(np.ones(DIM)),                                # 정규화되지 않음
        query(np.zeros(DIM)),                               # 0벡터
        query(unit(np.ones(DIM)), top_k=0),
        query(unit(np.ones(DIM)), top_k=51),                # 기본 상한 50 초과
        {**query(unit(np.ones(DIM))), "patient_name": "x"},  # 정해지지 않은 필드
        {"scheme": "rot13", "vector": [1.0]},
        {"vector": [1.0]},
    ],
)
def test_invalid_requests_are_rejected(client, body):
    response = client.post("/v1/search", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_nan_is_rejected(client):
    raw = '{"scheme": "plaintext", "vector": [NaN, 0, 0, 0, 0, 0, 0, 0]}'
    response = client.post("/v1/search", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422


def test_validation_error_does_not_echo_the_query(client):
    marker = 0.123456789
    response = client.post("/v1/search", json={"scheme": "plaintext", "vector": [marker, "oops"]})
    assert response.status_code == 422
    assert str(marker) not in response.text and "oops" not in response.text


def test_encrypted_scheme_reports_not_connected(client):
    payload = base64.b64encode(b"ciphertext").decode()
    response = client.post("/v1/search", json={"scheme": "ckks-hec3", "payload": payload})
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "engine_not_connected"

    bad = client.post("/v1/search", json={"scheme": "ckks-hec3", "payload": "not base64!"})
    assert bad.status_code == 422


def test_api_key_is_required_when_configured(data):
    folder, vectors, _ = data
    with TestClient(create_app(Settings(data_dir=folder, api_key="s3cret"))) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/v1/meta").status_code == 401
        assert c.post("/v1/search", json=query(vectors[0])).status_code == 401
        assert c.post("/v1/search", json=query(vectors[0]), headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.post("/v1/search", json=query(vectors[0]), headers={"X-API-Key": "s3cret"}).status_code == 200


def test_oversized_body_is_rejected(data):
    folder, vectors, _ = data
    with TestClient(create_app(Settings(data_dir=folder, max_body_bytes=64))) as c:
        assert c.post("/v1/search", json=query(vectors[0])).status_code == 413


def test_store_rejects_bad_files(tmp_path):
    with pytest.raises(StoreError):
        load_store(tmp_path)  # 파일 없음

    write_data(tmp_path, np.ones((3, 4)), ["A", "B", "C"])  # 정규화되지 않음
    with pytest.raises(StoreError):
        load_store(tmp_path)

    write_data(tmp_path, unit(np.eye(3)), ["A", "B"])  # 개수 불일치
    with pytest.raises(StoreError):
        load_store(tmp_path)

    write_data(tmp_path, unit(np.eye(3)), ["A", "A", "B"])  # 중복 ID
    with pytest.raises(StoreError):
        load_store(tmp_path)


def test_server_does_not_start_without_database(tmp_path):
    with pytest.raises(StoreError):
        with TestClient(create_app(Settings(data_dir=tmp_path))):
            pass


REAL = os.environ.get("VEIL_TEST_DATA_DIR")


@pytest.mark.skipif(not REAL, reason="VEIL_TEST_DATA_DIR 가 지정되지 않음")
def test_matches_pipeline_plaintext_topk():
    """파이프라인이 저장한 plaintext_topk.csv 와 순위·점수가 같아야 한다."""
    folder = Path(REAL)
    queries = np.load(folder / "queries.npy", allow_pickle=False)
    query_ids = [row["query_id"] for row in csv.DictReader((folder / "query_ids.csv").open(encoding="utf-8"))]
    expected: dict[str, list[tuple[str, float]]] = {}
    for row in csv.DictReader((folder / "plaintext_topk.csv").open(encoding="utf-8")):
        expected.setdefault(row["query_id"], []).append((row["record_id"], float(row["cosine_similarity"])))

    with TestClient(create_app(Settings(data_dir=folder))) as c:
        for query_id, vector in zip(query_ids, queries):
            want = expected[query_id]
            got = c.post("/v1/search", json=query(vector, top_k=len(want))).json()["results"]
            assert [r["record_id"] for r in got] == [record_id for record_id, _ in want]
            assert [r["score"] for r in got] == pytest.approx([score for _, score in want], abs=1e-8)
