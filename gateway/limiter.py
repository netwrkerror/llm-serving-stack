import time

from .config import settings


class _Window:
    __slots__ = ("start", "requests", "tokens")

    def __init__(self) -> None:
        self.start = time.time()
        self.requests = 0
        self.tokens = 0


class RateLimiter:
    """
    Fixed-window rate limiter (per API key, resets every 60 s).
    Safe for asyncio — no awaits between state mutations, single-threaded event loop.
    """

    def __init__(self) -> None:
        self._windows: dict[str, _Window] = {}

    def _window(self, key: str) -> _Window:
        w = self._windows.get(key)
        if w is None or time.time() - w.start >= 60:
            w = _Window()
            self._windows[key] = w
        return w

    def check_request(self, api_key: str) -> tuple[bool, str]:
        """Check and increment request counter. Returns (allowed, reason)."""
        if not settings.rate_limit_enabled:
            return True, ""
        w = self._window(api_key or "anonymous")
        if w.requests >= settings.max_requests_per_minute:
            return False, f"rate limit: {settings.max_requests_per_minute} req/min exceeded"
        w.requests += 1
        return True, ""

    def check_tokens(self, api_key: str, requested_tokens: int) -> tuple[bool, str]:
        """
        Pessimistic token budget check using max_tokens from the request.
        Charged upfront so streaming responses don't exceed the budget mid-flight.
        """
        if not settings.rate_limit_enabled:
            return True, ""
        w = self._window(api_key or "anonymous")
        if w.tokens + requested_tokens > settings.max_tokens_per_minute:
            return False, f"token budget: {settings.max_tokens_per_minute} tokens/min exceeded"
        w.tokens += requested_tokens
        return True, ""

    def remaining(self, api_key: str) -> dict:
        w = self._window(api_key or "anonymous")
        reset_in = max(0, 60 - int(time.time() - w.start))
        return {
            "requests_remaining": max(0, settings.max_requests_per_minute - w.requests),
            "tokens_remaining": max(0, settings.max_tokens_per_minute - w.tokens),
            "reset_in_seconds": reset_in,
        }


rate_limiter = RateLimiter()
