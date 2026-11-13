"""등록 DB 적재.

Medical-Data-Pipeline 의 preprocess.py 가 만든 파일 가운데 두 개만 읽는다.
    database.npy        (N × D) float64, 행마다 L2 정규화된 벡터
    database_ids.csv    머리글 record_id, 한 줄에 ID 하나 (행 순서 = 벡터 순서)

preprocessor.joblib 은 읽지 않는다. pickle 이라 신뢰할 수 없는 파일을 열면 코드가 실행되고,
원본 속성을 벡터로 바꾸는 일은 의료기관(클라이언트) 쪽의 몫이기 때문이다.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

NORM_TOLERANCE = 1e-6


class StoreError(RuntimeError):
    """등록 DB 파일이 없거나 형식이 맞지 않는다."""


@dataclass(frozen=True)
class Store:
    vectors: np.ndarray  # (N, D) float64
    ids: tuple[str, ...]
    dataset: str

    @property
    def size(self) -> int:
        return self.vectors.shape[0]

    @property
    def dimension(self) -> int:
        return self.vectors.shape[1]


def load_store(data_dir: Path) -> Store:
    data_dir = Path(data_dir)
    vectors_path = data_dir / "database.npy"
    ids_path = data_dir / "database_ids.csv"
    for path in (vectors_path, ids_path):
        if not path.is_file():
            raise StoreError(f"등록 DB 파일이 없습니다: {path}")

    try:
        vectors = np.load(vectors_path, allow_pickle=False)
    except ValueError as exc:
        raise StoreError(f"database.npy 를 읽을 수 없습니다: {exc}") from exc
    if vectors.ndim != 2 or vectors.shape[0] == 0 or vectors.shape[1] == 0:
        raise StoreError(f"database.npy 는 비어 있지 않은 2차원 배열이어야 합니다 (현재 {vectors.shape})")
    if not np.issubdtype(vectors.dtype, np.floating):
        raise StoreError(f"database.npy 는 실수 배열이어야 합니다 (현재 {vectors.dtype})")
    vectors = np.ascontiguousarray(vectors, dtype=np.float64)
    if not np.all(np.isfinite(vectors)):
        raise StoreError("database.npy 에 NaN 또는 무한대가 있습니다")
    norms = np.linalg.norm(vectors, axis=1)
    if np.any(np.abs(norms - 1.0) > NORM_TOLERANCE):
        raise StoreError("database.npy 의 모든 행은 L2 정규화되어 있어야 합니다")

    with ids_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows or [cell.strip() for cell in rows[0]] != ["record_id"]:
        raise StoreError("database_ids.csv 의 첫 줄은 record_id 여야 합니다")
    ids = tuple(row[0].strip() for row in rows[1:] if row and row[0].strip())
    if len(ids) != vectors.shape[0]:
        raise StoreError(f"ID {len(ids)}개와 벡터 {vectors.shape[0]}개의 개수가 다릅니다")
    if len(set(ids)) != len(ids):
        raise StoreError("database_ids.csv 에 중복된 ID 가 있습니다")

    vectors.setflags(write=False)
    return Store(vectors=vectors, ids=ids, dataset=_dataset_name(data_dir))


def _dataset_name(data_dir: Path) -> str:
    """metadata.json 이 있으면 데이터셋 이름만 가져온다. 없어도 된다."""
    try:
        metadata = json.loads((data_dir / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown"
    name = metadata.get("dataset") if isinstance(metadata, dict) else None
    return name if isinstance(name, str) and name else "unknown"
