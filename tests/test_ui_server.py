"""E3 / E8: engine status and favicon routes used by the web UI."""
from fastapi.testclient import TestClient

from src.agent import concierge
from src.api.server import app

client = TestClient(app)


def test_favicon_is_served():
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


def test_engine_reports_missing_credentials(monkeypatch):
    monkeypatch.setattr("src.api.server.bedrock_credentials_configured", lambda: False)
    body = client.get("/api/engine").json()
    assert body == {"primary": "bedrock", "model": concierge.SupportConcierge.default_model_id(),
                    "credentials_configured": False, "fallback": "local_fallback"}


def test_explicit_keys_count_as_configured(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST_ONLY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-test-only")
    assert concierge.bedrock_credentials_configured() is True
