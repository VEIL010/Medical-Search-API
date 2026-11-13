"""요청·응답 모양."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# 등록 벡터 차원과 정확히 같은지는 적재된 DB 를 아는 라우트에서 확인한다. 여기서는 상한만 둔다.
MAX_VECTOR_LENGTH = 4096

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class PlaintextQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme: Literal["plaintext"]
    vector: list[FiniteFloat] = Field(min_length=1, max_length=MAX_VECTOR_LENGTH)
    top_k: int = Field(default=5, ge=1)


class EncryptedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme: Literal["ckks-hec3"]
    payload: str = Field(min_length=1, description="BlindMatchCore 가 만든 질의 암호문 (base64)")


SearchRequest = Annotated[Union[PlaintextQuery, EncryptedQuery], Field(discriminator="scheme")]


class HitOut(BaseModel):
    rank: int
    record_id: str
    score: float


class PlaintextResult(BaseModel):
    scheme: Literal["plaintext"] = "plaintext"
    encrypted: Literal[False] = False
    dataset: str
    searched: int
    top_k: int
    results: list[HitOut]
    elapsed_ms: float


class EncryptedResult(BaseModel):
    scheme: Literal["ckks-hec3"] = "ckks-hec3"
    encrypted: Literal[True] = True
    dataset: str
    searched: int
    encrypted_scores: str = Field(description="점수 암호문 (base64). 의료기관이 복호화한 뒤 Top-K 를 고른다")
    elapsed_ms: float
