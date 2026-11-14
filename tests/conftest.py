import pytest

from gateway.config import settings

_GUARDED = (
    "auth_enabled",
    "api_keys",
    "rate_limit_enabled",
    "max_requests_per_minute",
    "max_tokens_per_minute",
)


@pytest.fixture(autouse=True)
def restore_settings():
    """Settings is a module-level singleton, so tests that flip it must put it back."""
    saved = {name: getattr(settings, name) for name in _GUARDED}
    yield
    for name, value in saved.items():
        setattr(settings, name, value)
