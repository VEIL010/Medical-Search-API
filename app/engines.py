"""검색 엔진. API 는 요청의 scheme 에 맞는 엔진에 계산을 넘긴다.

    plaintext   평문 벡터끼리 코사인 유사도를 계산하고 서버가 Top-K 를 고른다.
                암호화 검색 결과와 비교할 기준값이다. 서버가 질의를 그대로 본다.
    ckks-hec3   BlindMatchCore(CKKS + HE-C³)에 암호문을 넘기고 암호화된 점수를 받는다.
                서버는 비밀키가 없으므로 Top-K 는 의료기관이 복호화한 뒤에 고른다.
                BlindMatchCore 가 준비되면 CoreEngine.match 만 채우면 된다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .store import Store

PLAINTEXT = "plaintext"
CKKS_HEC3 = "ckks-hec3"


class EngineNotConnected(RuntimeError):
    """해당 방식의 검색 엔진이 아직 연결되지 않았다."""


@dataclass(frozen=True)
class Hit:
    rank: int
    record_id: str
    score: float


class PlaintextEngine:
    scheme = PLAINTEXT
    encrypted = False
    connected = True

    def __init__(self, store: Store) -> None:
        self._store = store

    def search(self, vector: np.ndarray, top_k: int) -> list[Hit]:
        """단위 벡터끼리의 내적 = 코사인 유사도. 동점이면 먼저 등록된 기록이 앞선다."""
        scores = self._store.vectors @ vector
        order = np.argsort(-scores, kind="stable")[: min(top_k, self._store.size)]
        return [
            Hit(rank=rank, record_id=self._store.ids[index], score=round(float(scores[index]), 8))
            for rank, index in enumerate(order, start=1)
        ]


class CoreEngine:
    """BlindMatchCore 연결 자리.

    payload 는 의료기관 쪽 BlindMatchCore 가 만든 직렬화된 질의 암호문(평가 키 포함 여부는
    Core 규격을 따른다)이고, 돌려주는 값은 직렬화된 점수 암호문이다. API 는 내용을 해석하지 않는다.
    """

    scheme = CKKS_HEC3
    encrypted = True
    connected = False

    def match(self, payload: bytes) -> bytes:
        raise EngineNotConnected("CKKS·HE-C³ 검색 엔진(BlindMatchCore)이 아직 연결되지 않았습니다.")
