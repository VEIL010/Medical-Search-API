"""Immutable enrolled vector snapshots; raw records and transformers never enter this store."""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ApiError
from .store import NORM_TOLERANCE

FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
RecordId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", strict=True)]


class SnapshotRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(strict=True, min_length=36, max_length=36)
    transformer_hash: Hash
    scheme: str = Field(strict=True, max_length=32)

    @field_validator("dataset_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        if str(uuid.UUID(value)) != value:
            raise ValueError("Expected canonical UUID")
        return value


class DatasetRequest(SnapshotRef):
    name: str = Field(strict=True, min_length=1, max_length=80)
    dimension: int = Field(strict=True, ge=1, le=128)
    records: int = Field(strict=True, ge=1, le=2000)
    ids: list[RecordId] = Field(min_length=1, max_length=2000)
    vectors: list[Annotated[list[FiniteNumber], Field(min_length=1, max_length=128)]] = Field(
        min_length=1, max_length=2000
    )

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("Invalid dataset name")
        return value


@dataclass(frozen=True)
class Snapshot:
    manifest: dict
    ids: tuple[str, ...]
    vectors: np.ndarray


def validate_vectors(vectors, records: int, dimension: int) -> np.ndarray:
    try:
        matrix = np.asarray(vectors, dtype=np.float64)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_vectors", "벡터 배열의 형식이 올바르지 않습니다.") from None
    if matrix.shape != (records, dimension) or not np.all(np.isfinite(matrix)):
        raise ApiError(422, "invalid_vectors", "벡터 개수와 차원을 확인해 주세요.")
    if np.any(np.abs(np.linalg.norm(matrix, axis=1) - 1.0) > NORM_TOLERANCE):
        raise ApiError(422, "invalid_vectors", "모든 벡터는 L2 정규화되어 있어야 합니다.")
    matrix.setflags(write=False)
    return matrix


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.active: Snapshot | None = None
        self._restore()

    def _restore(self) -> None:
        pointer = self.root / "active.json"
        if not pointer.exists():
            return
        # Local files are trusted outputs, but still validate references and vectors.
        try:
            dataset_id = json.loads(pointer.read_text(encoding="utf-8"))["dataset_id"]
            if str(uuid.UUID(dataset_id)) != dataset_id:
                raise ValueError("Invalid pointer")
            data = json.loads((self.root / "snapshots" / f"{dataset_id}.json").read_text(encoding="utf-8"))
            manifest = data["manifest"]
            body = DatasetRequest.model_validate({
                **{key: manifest[key] for key in DatasetRequest.model_fields if key not in ("ids", "vectors")},
                "ids": data["ids"], "vectors": data["vectors"],
            })
            if (body.dataset_id != dataset_id or body.scheme != "plaintext"
                    or len(body.ids) != body.records or len(set(body.ids)) != body.records):
                raise ValueError("Inconsistent snapshot")
            matrix = validate_vectors(body.vectors, body.records, body.dimension)
            self.active = Snapshot(manifest, tuple(body.ids), matrix)
        except (OSError, ValueError, KeyError, TypeError, ApiError) as exc:
            raise RuntimeError("로컬 등록본을 읽지 못했습니다. 서버 저장 폴더를 확인하세요.") from exc

    def register(self, body: DatasetRequest) -> dict:
        if body.scheme != "plaintext":
            raise ApiError(501, "encryption_not_connected", "암호화 코어가 아직 연결되지 않았습니다.")
        if len(body.ids) != body.records or len(set(body.ids)) != body.records:
            raise ApiError(422, "invalid_ids", "기록 ID는 벡터 수와 같고 서로 달라야 합니다.")
        matrix = validate_vectors(body.vectors, body.records, body.dimension)
        manifest = {
            "dataset_id": body.dataset_id, "name": body.name, "records": body.records,
            "dimension": body.dimension, "scheme": "plaintext", "transformer_hash": body.transformer_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        snapshot = Snapshot(manifest, tuple(body.ids), matrix)
        with self.lock:
            path = self.root / "snapshots" / f"{body.dataset_id}.json"
            if path.exists():
                raise ApiError(409, "dataset_exists", "이미 등록된 데이터셋 버전입니다.")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_json(path, {"manifest": manifest, "ids": body.ids, "vectors": body.vectors})
                atomic_json(self.root / "active.json", {"dataset_id": body.dataset_id})
            except OSError:
                # An unactivated immutable snapshot may remain. Never alter the old pointer/state.
                raise ApiError(500, "storage_failed", "등록본을 저장하지 못했습니다. 기존 등록본을 유지합니다.") from None
            self.active = snapshot
        return manifest.copy()

    def current(self) -> Snapshot | None:
        with self.lock:
            return self.active
