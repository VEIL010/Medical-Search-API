"""실행 설정. 모두 환경 변수로 받는다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    """Medical-Data-Pipeline 산출물 폴더 (database.npy, database_ids.csv)."""

    api_key: str | None = None
    """설정하면 /v1/* 요청에 X-API-Key 헤더가 필요하다."""

    max_top_k: int = 50
    max_body_bytes: int = 64 * 1024 * 1024
    """요청 본문 상한. CKKS 암호문이 수 MB 이므로 넉넉히 잡는다."""

    dataset_dir: Path | None = None
    """일괄 등록 벡터 스냅샷 저장 위치. 직접 구성할 때는 data_dir/.datasets."""
    enrollment_api_key: str | None = None
    """DB 교체 권한의 키. 생략하면 api_key 를 사용하며, 둘 다 없으면 등록은 비활성화."""

    @property
    def enrollment_key(self) -> str | None:
        return self.enrollment_api_key or self.api_key or None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("VEIL_DATA_DIR", "data/heart")),
            api_key=os.environ.get("VEIL_API_KEY") or None,
            max_top_k=int(os.environ.get("VEIL_MAX_TOP_K", "50")),
            max_body_bytes=int(os.environ.get("VEIL_MAX_BODY_BYTES", str(64 * 1024 * 1024))),
            dataset_dir=Path(os.environ.get("VEIL_DATASET_DIR", ".local/datasets")),
            enrollment_api_key=os.environ.get("VEIL_ENROLLMENT_API_KEY") or None,
        )
