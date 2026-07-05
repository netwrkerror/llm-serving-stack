from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

# These paths bypass auth — infrastructure endpoints that must stay open
_OPEN_PATHS = {"/health", "/metrics", "/docs", "/openapi.json", "/v1/models"}


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.auth_enabled:
            return await call_next(request)

        if request.url.path in _OPEN_PATHS:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Missing API key", "type": "auth_error"}},
            )

        key = auth[len("Bearer "):]
        if key not in settings.api_key_set:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "auth_error"}},
            )

        return await call_next(request)
