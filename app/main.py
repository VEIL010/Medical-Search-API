"""VEIL Medical-Search-API — 검색 요청을 받아 검색 엔진에 넘기고 결과를 돌려준다.

    GET  /health        살아 있는지 확인 (인증 없음)
    GET  /v1/meta       등록 DB 크기·차원, 지원 방식
    POST /v1/search     유사 기록 검색

실행 (저장소 루트에서):
    VEIL_DATA_DIR=<파이프라인 산출물 폴더> python -m uvicorn app.main:app --port 8200

이 서버는 원본 의료 속성을 받지 않는다. 의료기관(Interface-)이 전처리한 벡터 또는 암호문만 받고,
기록 ID 와 유사도 점수만 돌려준다. 질의 내용은 로그에도 오류 응답에도 남기지 않는다.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import time
from contextlib import asynccontextmanager

import numpy as np
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .engines import CoreEngine, EngineNotConnected, PlaintextEngine
from .schemas import EncryptedQuery, EncryptedResult, HitOut, PlaintextResult, SearchRequest
from .store import NORM_TOLERANCE, load_store


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}}, status_code=status)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 등록 DB 가 없거나 깨져 있으면 여기서 멈춘다. 빈 서버가 떠 있는 것보다 낫다.
        store = load_store(settings.data_dir)
        app.state.store = store
        app.state.plaintext = PlaintextEngine(store)
        app.state.core = CoreEngine()
        yield

    app = FastAPI(title="VEIL Medical-Search-API", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > settings.max_body_bytes):
            return _error(413, "body_too_large", "요청 본문이 너무 큽니다.")
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, exc: ApiError):
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def on_invalid(request: Request, exc: RequestValidationError):
        # 기본 422 응답은 받은 값(input)을 그대로 되돌려 준다. 질의 벡터가 응답·로그에 남지 않도록
        # 위치와 사유만 추린다.
        fields = [
            {"field": ".".join(str(part) for part in err["loc"] if part != "body"), "reason": err["msg"]}
            for err in exc.errors()
        ]
        return _error(422, "invalid_request", "요청 형식이 올바르지 않습니다.", fields=fields)

    def require_key(request: Request) -> None:
        if settings.api_key is None:
            return
        given = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(given.encode(), settings.api_key.encode()):
            raise ApiError(401, "unauthorized", "API 키가 없거나 올바르지 않습니다.")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/v1/meta", dependencies=[Depends(require_key)])
    def meta(request: Request):
        store = request.app.state.store
        engines = (request.app.state.plaintext, request.app.state.core)
        return {
            "service": "veil-medical-search-api",
            "version": app.version,
            "dataset": {"name": store.dataset, "records": store.size, "dimension": store.dimension},
            "schemes": [
                {"name": e.scheme, "encrypted": e.encrypted, "connected": e.connected} for e in engines
            ],
            "limits": {"max_top_k": settings.max_top_k},
        }

    @app.post(
        "/v1/search",
        dependencies=[Depends(require_key)],
        response_model=PlaintextResult | EncryptedResult,
    )
    def search(body: SearchRequest, request: Request):
        store = request.app.state.store
        started = time.perf_counter()

        if isinstance(body, EncryptedQuery):
            try:
                payload = base64.b64decode(body.payload, validate=True)
            except (binascii.Error, ValueError):
                raise ApiError(422, "invalid_request", "payload 는 base64 여야 합니다.") from None
            try:
                scores = request.app.state.core.match(payload)
            except EngineNotConnected as exc:
                raise ApiError(501, "engine_not_connected", str(exc)) from None
            return EncryptedResult(
                dataset=store.dataset,
                searched=store.size,
                encrypted_scores=base64.b64encode(scores).decode("ascii"),
                elapsed_ms=_elapsed_ms(started),
            )

        if len(body.vector) != store.dimension:
            raise ApiError(422, "invalid_request", f"vector 는 {store.dimension}차원이어야 합니다.")
        if body.top_k > settings.max_top_k:
            raise ApiError(422, "invalid_request", f"top_k 는 {settings.max_top_k} 이하여야 합니다.")
        vector = np.asarray(body.vector, dtype=np.float64)
        if abs(float(np.linalg.norm(vector)) - 1.0) > NORM_TOLERANCE:
            raise ApiError(422, "invalid_request", "vector 는 L2 정규화되어 있어야 합니다.")

        hits = request.app.state.plaintext.search(vector, body.top_k)
        return PlaintextResult(
            dataset=store.dataset,
            searched=store.size,
            top_k=len(hits),
            results=[HitOut(rank=h.rank, record_id=h.record_id, score=h.score) for h in hits],
            elapsed_ms=_elapsed_ms(started),
        )

    return app


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


app = create_app()
