"""API key middleware: open paths, missing key, bad key, good key.

Builds a bare app with just the middleware, so these exercise the auth rules
without needing a backend.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.auth import APIKeyMiddleware
from gateway.config import settings


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return {"metrics": True}

    @app.get("/v1/models")
    async def models():
        return {"object": "list"}

    @app.post("/v1/chat/completions")
    async def chat():
        return {"ok": True}

    return TestClient(app)


def test_disabled_by_default_lets_everything_through(client):
    settings.auth_enabled = False
    assert client.post("/v1/chat/completions").status_code == 200


def test_missing_key_is_rejected(client):
    settings.auth_enabled = True
    settings.api_keys = "secret"

    resp = client.post("/v1/chat/completions")
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "auth_error"
    assert "Missing" in resp.json()["error"]["message"]


def test_bad_key_is_rejected(client):
    settings.auth_enabled = True
    settings.api_keys = "secret"

    resp = client.post("/v1/chat/completions", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert "Invalid" in resp.json()["error"]["message"]


def test_valid_key_is_accepted(client):
    settings.auth_enabled = True
    settings.api_keys = "secret"

    resp = client.post("/v1/chat/completions", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200


def test_any_configured_key_works(client):
    settings.auth_enabled = True
    settings.api_keys = "alpha, beta ,gamma"

    for key in ("alpha", "beta", "gamma"):
        resp = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200, f"{key} should be accepted"


def test_non_bearer_scheme_is_rejected(client):
    settings.auth_enabled = True
    settings.api_keys = "secret"

    resp = client.post("/v1/chat/completions", headers={"Authorization": "Basic secret"})
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/health", "/metrics", "/v1/models"])
def test_infrastructure_paths_stay_open(client, path):
    """Scrapers and probes must not need a key, or Prometheus stops collecting."""
    settings.auth_enabled = True
    settings.api_keys = "secret"
    assert client.get(path).status_code == 200


def test_empty_key_list_rejects_everything(client):
    """Auth on with no keys configured is a misconfiguration: fail closed."""
    settings.auth_enabled = True
    settings.api_keys = ""

    resp = client.post("/v1/chat/completions", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 401
