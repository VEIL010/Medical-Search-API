"""VEIL Medical-Search-API — 검색 요청을 받아 검색 엔진에 넘기고 결과를 돌려준다.

    GET  /health        살아 있는지 확인 (인증 없음)
    GET  /v1/meta       등록 DB 크기·차원, 지원 방식
    POST /v1/datasets   인증된 기관 서비스의 전처리 벡터를 일괄 등록
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

from .config import Settings
from .engines import CoreEngine, EngineNotConnected, PlaintextEngine
from .datasets import DatasetRequest, SnapshotStore
from .errors import ApiError, error_response as _error
from .middleware import BodyLimitMiddleware
from .schemas import EncryptedQuery, EncryptedResult, HitOut, PlaintextResult, SearchRequest
from .store import NORM_TOLERANCE, Store, load_store


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.datasets = SnapshotStore(settings.dataset_dir or settings.data_dir / ".datasets")
        app.state.store = None
        if app.state.datasets.current() is None:
            # 최초 등록은 비어 있는 서버에서 시작할 수 있다. 일부 파일만 있거나 깨진 기존 DB는
            # '빈 DB'로 취급하지 않고 기존 적재 검증으로 실패시킨다.
            legacy_present = any((settings.data_dir / name).exists() for name in (
                "database.npy", "database_ids.csv", "metadata.json"
            ))
            if legacy_present or not settings.enrollment_key:
                app.state.store = load_store(settings.data_dir)
        app.state.core = CoreEngine()
        yield

    app = FastAPI(title="VEIL Medical-Search-API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

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

    def require_enrollment_key(request: Request) -> None:
        key = settings.enrollment_key
        if not key:
            raise ApiError(503, "token_not_configured", "DB 등록 API 인증 설정이 필요합니다.")
        given = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(given.encode(), key.encode()):
            raise ApiError(401, "unauthorized", "API 키가 없거나 올바르지 않습니다.")
        if request.headers.get("origin") or request.headers.get("sec-fetch-site"):
            raise ApiError(403, "browser_request_rejected", "DB 등록 API는 기관 서비스의 요청만 받습니다.")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "json_required", "JSON 요청이 필요합니다.")

    def current_store(request: Request):
        # Capture one immutable snapshot per request so concurrent activation cannot mix IDs/vectors.
        snapshot = request.app.state.datasets.current()
        if snapshot is not None:
            return Store(snapshot.vectors, snapshot.ids, snapshot.manifest["name"]), snapshot.manifest
        return request.app.state.store, None

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/v1/meta", dependencies=[Depends(require_key)])
    def meta(request: Request):
        store, manifest = current_store(request)
        return {
            "service": "veil-medical-search-api",
            "version": app.version,
            "dataset": {"name": store.dataset, "records": store.size, "dimension": store.dimension} if store else None,
            "schemes": [
                {"name": "plaintext", "encrypted": False, "connected": store is not None},
                {"name": "ckks-hec3", "encrypted": True, "connected": False},
            ],
            "limits": {"max_top_k": settings.max_top_k},
            "active": manifest,
            "encrypted": False,
            "mode": "local_plaintext",
        }

    @app.post("/v1/datasets", dependencies=[Depends(require_enrollment_key)], status_code=201)
    def register(body: DatasetRequest, request: Request):
        manifest = request.app.state.datasets.register(body)
        return {"active": manifest, "encrypted": False, "mode": "local_plaintext"}

    @app.post(
        "/v1/search",
        dependencies=[Depends(require_key)],
        response_model=PlaintextResult | EncryptedResult,
        response_model_exclude_none=True,
    )
    def search(body: SearchRequest, request: Request):
        store, manifest = current_store(request)
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

        if store is None:
            raise ApiError(409, "dataset_not_registered", "먼저 데이터셋을 등록하세요.")
        if manifest is not None:
            if (body.dataset_id != manifest["dataset_id"]
                    or body.transformer_hash != manifest["transformer_hash"]):
                raise ApiError(409, "dataset_changed", "현재 등록본과 질의의 전처리 버전이 다릅니다. DB 검색 화면을 새로고침하세요.")
        elif body.dataset_id is not None:
            raise ApiError(409, "dataset_changed", "요청한 등록본이 현재 활성화되어 있지 않습니다.")
        if len(body.vector) != store.dimension:
            raise ApiError(422, "invalid_request", f"vector 는 {store.dimension}차원이어야 합니다.")
        if body.top_k > settings.max_top_k:
            raise ApiError(422, "invalid_request", f"top_k 는 {settings.max_top_k} 이하여야 합니다.")
        vector = np.asarray(body.vector, dtype=np.float64)
        if abs(float(np.linalg.norm(vector)) - 1.0) > NORM_TOLERANCE:
            raise ApiError(422, "invalid_request", "vector 는 L2 정규화되어 있어야 합니다.")

        hits = PlaintextEngine(store).search(vector, body.top_k)
        return PlaintextResult(
            dataset=store.dataset,
            searched=store.size,
            top_k=len(hits),
            results=[HitOut(rank=h.rank, record_id=h.record_id, score=h.score) for h in hits],
            elapsed_ms=_elapsed_ms(started),
            dataset_id=manifest["dataset_id"] if manifest else None,
        )

    return app


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


app = create_app()
