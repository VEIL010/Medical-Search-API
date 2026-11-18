"""Limit actual received bytes, including chunked requests without Content-Length."""
from .errors import error_response


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int, dataset_max_bytes: int = 5 * 1024 * 1024):
        self.app, self.max_bytes, self.dataset_max_bytes = app, max_bytes, dataset_max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = min(self.max_bytes, self.dataset_max_bytes) if scope["path"] == "/v1/datasets" else self.max_bytes
        headers = {key.lower(): value for key, value in scope["headers"]}
        declared = headers.get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            return await error_response(413, "body_too_large", "요청 본문이 너무 큽니다.")(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                return await error_response(413, "body_too_large", "요청 본문이 너무 큽니다.")(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body, delivered = b"".join(chunks), False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
