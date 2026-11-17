from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import POLICY_AUTO, settings

# These paths bypass auth — infrastructure endpoints that must stay open
_OPEN_PATHS = {"/health", "/metrics", "/docs", "/openapi.json", "/v1/models"}


def api_key_from(request: Request) -> str:
    """The bearer token on a request, or "anonymous".

    One place that knows how the key is carried, so the routing policy and the rate
    limiter cannot disagree about which key a request belongs to.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return "anonymous"
    return auth[len("Bearer "):].strip() or "anonymous"


def routing_policy_for(request: Request) -> str:
    """The routing policy governing this request.

    `auto` when auth is disabled: there is no key, so there is nothing to attach a
    policy to. Enforcing a policy against an unauthenticated caller would be
    security theatre — anyone could pick their own policy by omitting the key.
    """
    if not settings.auth_enabled:
        return POLICY_AUTO
    return settings.policy_for(api_key_from(request))


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
