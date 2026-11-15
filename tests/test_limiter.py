"""Rate limiter: per-key isolation, both budgets, and window reset.

The limiter reads module-level settings, so each test flips them and restores
afterwards via the autouse fixture in conftest.
"""
import pytest

from gateway.config import settings
from gateway.limiter import RateLimiter


@pytest.fixture
def limiter():
    return RateLimiter()


def test_disabled_by_default_allows_everything(limiter):
    settings.rate_limit_enabled = False
    settings.max_requests_per_minute = 1
    for _ in range(50):
        assert limiter.check_request("k")[0] is True
    assert limiter.check_tokens("k", 10_000_000)[0] is True


def test_request_budget_blocks_past_the_limit(limiter):
    settings.rate_limit_enabled = True
    settings.max_requests_per_minute = 3

    for i in range(3):
        allowed, reason = limiter.check_request("k")
        assert allowed is True, f"request {i} should be allowed"
        assert reason == ""

    allowed, reason = limiter.check_request("k")
    assert allowed is False
    assert "3 req/min" in reason


def test_token_budget_blocks_past_the_limit(limiter):
    settings.rate_limit_enabled = True
    settings.max_tokens_per_minute = 100

    assert limiter.check_tokens("k", 60)[0] is True
    assert limiter.check_tokens("k", 40)[0] is True  # exactly at the limit

    allowed, reason = limiter.check_tokens("k", 1)
    assert allowed is False
    assert "100 tokens/min" in reason


def test_token_budget_is_charged_upfront(limiter):
    """A single oversized request is refused rather than half-admitted."""
    settings.rate_limit_enabled = True
    settings.max_tokens_per_minute = 100

    allowed, _ = limiter.check_tokens("k", 101)
    assert allowed is False
    # the refused request must not have consumed budget
    assert limiter.check_tokens("k", 100)[0] is True


def test_budgets_are_per_key(limiter):
    settings.rate_limit_enabled = True
    settings.max_requests_per_minute = 2
    settings.max_tokens_per_minute = 50

    assert limiter.check_request("alice")[0] is True
    assert limiter.check_request("alice")[0] is True
    assert limiter.check_request("alice")[0] is False

    # bob has his own window
    assert limiter.check_request("bob")[0] is True
    assert limiter.check_tokens("bob", 50)[0] is True
    assert limiter.check_tokens("alice", 50)[0] is True


def test_window_resets_after_60s(limiter, monkeypatch):
    settings.rate_limit_enabled = True
    settings.max_requests_per_minute = 1
    settings.max_tokens_per_minute = 10

    now = 1_000.0
    monkeypatch.setattr("gateway.limiter.time.time", lambda: now)

    assert limiter.check_request("k")[0] is True
    assert limiter.check_request("k")[0] is False

    now += 59.0  # still inside the window
    assert limiter.check_request("k")[0] is False

    now += 2.0  # 61s in: new window
    assert limiter.check_request("k")[0] is True
    assert limiter.check_tokens("k", 10)[0] is True


def test_remaining_reports_headroom(limiter, monkeypatch):
    settings.rate_limit_enabled = True
    settings.max_requests_per_minute = 5
    settings.max_tokens_per_minute = 100

    now = 500.0
    monkeypatch.setattr("gateway.limiter.time.time", lambda: now)

    limiter.check_request("k")
    limiter.check_tokens("k", 30)

    left = limiter.remaining("k")
    assert left["requests_remaining"] == 4
    assert left["tokens_remaining"] == 70
    assert left["reset_in_seconds"] == 60

    now += 25.0
    assert limiter.remaining("k")["reset_in_seconds"] == 35


def test_anonymous_requests_share_one_bucket(limiter):
    settings.rate_limit_enabled = True
    settings.max_requests_per_minute = 2

    assert limiter.check_request("")[0] is True
    assert limiter.check_request("anonymous")[0] is True
    # "" is normalised to "anonymous", so the third is refused
    assert limiter.check_request("")[0] is False
