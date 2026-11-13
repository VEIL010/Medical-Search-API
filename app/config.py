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

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("VEIL_DATA_DIR", "data/heart")),
            api_key=os.environ.get("VEIL_API_KEY") or None,
            max_top_k=int(os.environ.get("VEIL_MAX_TOP_K", "50")),
            max_body_bytes=int(os.environ.get("VEIL_MAX_BODY_BYTES", str(64 * 1024 * 1024))),
        )
