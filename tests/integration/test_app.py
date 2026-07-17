"""Integration tests for the app skeleton, health, and dev-route gating."""

from __future__ import annotations

from app.core.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient

from tests.conftest import make_test_settings


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def test_health_liveness(test_settings: Settings) -> None:
    with _client(test_settings) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert "X-Request-ID" in response.headers


def test_security_headers_present(test_settings: Settings) -> None:
    with _client(test_settings) as client:
        response = client.get("/api/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"


def test_docs_disabled_in_test(test_settings: Settings) -> None:
    with _client(test_settings) as client:
        assert client.get("/openapi.json").status_code == 404


def test_dev_routes_absent_outside_development(test_settings: Settings) -> None:
    with _client(test_settings) as client:
        assert client.get("/api/dev/ping").status_code == 404


def test_dev_routes_present_in_development() -> None:
    settings = make_test_settings(ENVIRONMENT="development")
    with _client(settings) as client:
        assert client.get("/api/dev/ping").status_code == 200
