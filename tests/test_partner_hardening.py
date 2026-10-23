"""Tanda 8a: Partner mode hardening (required headers, error reasons, device settings, E2E, watermark).
No test makes a network call outside 127.0.0.1."""
import json
import uuid

import pytest
from aiohttp import web

from src.utils import ring_partner_client as rpc
from src.utils.ring_partner_client import RingBlockedError, RingPartnerClient, RingRateLimitError
from tests.test_ring_partner_client import (
    CAPABILITY_ATTRIBUTES,
    ONLINE_ID,
    discovery_routes,
    jsonapi_error,
    run_against,
)

FAKE_TOKEN = "fake-hardening-token-000111222"


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.delenv("RING_PARTNER_HARDWARE_ID", raising=False)


# ---------------------------------------------------------------------------
# T20: required headers, base URL, 406, rate-limit headers
# ---------------------------------------------------------------------------

def assert_ring_headers(requests, expected_hardware_id):
    assert requests, "no request reached the fake server"
    for request in requests:
        assert request.headers.get("User-Agent") == f"RingTelemetryOps/{rpc.APP_VERSION}", request.path
        assert request.headers.get("Hardware_Id") == expected_hardware_id, request.path


def test_every_route_sends_user_agent_and_hardware_id(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_HARDWARE_ID", "11111111-2222-3333-4444-555555555555")

    async def calls(client):
        await client.list_devices()
        await client.get_status(ONLINE_ID)
        await client.get_capabilities(ONLINE_ID)

    _, requests = run_against(discovery_routes(), calls)
    paths = {r.path for r in requests}
    assert {"/v1/devices", f"/v1/devices/{ONLINE_ID}/status", f"/v1/devices/{ONLINE_ID}/capabilities"} <= paths
    assert_ring_headers(requests, "11111111-2222-3333-4444-555555555555")


def test_hardware_id_default_is_a_stable_uuid5(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_DEVICE_ID", "ava1.ring.device.X")
    first, second = rpc.hardware_id(), rpc.hardware_id()
    assert first == second
    assert uuid.UUID(first).version == 5
    monkeypatch.setenv("RING_PARTNER_DEVICE_ID", "ava1.ring.device.Y")
    assert rpc.hardware_id() != first


def test_base_url_comes_from_env_and_defaults_to_official_host(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_BASE_URL", "http://127.0.0.1:5/")
    assert RingPartnerClient().base_url == "http://127.0.0.1:5"
    monkeypatch.delenv("RING_PARTNER_BASE_URL")
    assert RingPartnerClient().base_url == "https://api.amazonvision.com"
    assert RingPartnerClient(base_url="http://127.0.0.1:6").base_url == "http://127.0.0.1:6"


def test_406_is_an_explicit_blocked_error_without_token():
    async def blocked(_request):
        return jsonapi_error(406, title="Not Acceptable")

    async def call(client):
        with pytest.raises(RingBlockedError) as info:
            await client.list_devices()
        return info.value

    error, requests = run_against({"/v1/devices": blocked}, call)
    assert error.status == 406
    assert "Ring blocked the request: missing headers or a datacenter IP" in str(error)
    assert FAKE_TOKEN not in str(error)
    assert len(requests) == 1   # not retried


def test_rate_limit_remaining_is_read_and_retry_after_is_honoured():
    calls = {"n": 0}
    sleeps = []

    async def devices(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return jsonapi_error(429, headers={"Retry-After": "3", "X-RateLimit-Remaining": "0"})
        return web.json_response({"data": []}, headers={"X-RateLimit-Remaining": "42"})

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def call(client):
        devices_found = await client.list_devices()
        return devices_found, client.rate_limit_remaining

    (found, remaining), _ = run_against({"/v1/devices": devices}, call, sleep=fake_sleep)
    assert found == [] and sleeps == [3.0] and remaining == 42


def test_429_without_retry_budget_raises_rate_limit_error():
    async def limited(_request):
        return jsonapi_error(429, headers={"Retry-After": "1"})

    async def call(client):
        with pytest.raises(RingRateLimitError):
            await client.list_devices()

    run_against({"/v1/devices": limited}, call, max_rate_limit_retries=0)


# ---------------------------------------------------------------------------
# T21: machine-readable error reasons
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

import src.api.server as server  # noqa: E402
from src.utils.ring_partner_client import (  # noqa: E402
    RingForbiddenError,
    RingNotFoundError,
    RingPartnerError,
    RingServiceUnavailableError,
    RingTimeoutError,
    RingUnauthorizedError,
)
from tests.test_partner_api_server import FakePartnerClient  # noqa: E402

api = TestClient(server.app)


@pytest.fixture
def partner_mode(monkeypatch):
    fake = FakePartnerClient()
    monkeypatch.setattr(server, "ring_source", "partner")
    monkeypatch.setattr(server, "ring_source_error", None)
    monkeypatch.setattr(server, "partner_client", fake)
    monkeypatch.setattr(server, "_partner_known_devices", {})
    return fake


@pytest.mark.parametrize("error, reason", [
    (RingUnauthorizedError(f"HTTP 401 {FAKE_TOKEN}", status=401), "token_rejected"),
    (RingForbiddenError("HTTP 403", status=403), "token_rejected"),
    (RingBlockedError("HTTP 406", status=406), "blocked_406"),
    (RingRateLimitError("HTTP 429", retry_after=2.0), "rate_limited"),
    (RingServiceUnavailableError("HTTP 503"), "device_offline_503"),
    (RingTimeoutError("timed out"), "network_error"),
    (RingPartnerError("connection error (ClientConnectorError)"), "network_error"),
    (RingNotFoundError("HTTP 404", status=404), "api_error"),
])
def test_status_and_telemetry_errors_carry_a_reason(partner_mode, error, reason):
    partner_mode.error = error

    status = api.get("/api/ring/status").json()
    assert status["mode"] == "ERROR"
    assert status["error_reason"] == reason and status["error"]["reason"] == reason
    assert FAKE_TOKEN not in json.dumps(status)

    telemetry = api.get("/api/ring/telemetry")
    assert telemetry.json()["detail"]["reason"] == reason
    assert FAKE_TOKEN not in telemetry.text


def test_missing_token_reason(monkeypatch):
    monkeypatch.setattr(server, "ring_source", "partner")
    monkeypatch.setattr(server, "partner_client", None)
    monkeypatch.setattr(server, "ring_source_error", "RING_PARTNER_ACCESS_TOKEN is not set")
    status = api.get("/api/ring/status").json()
    assert status["error_reason"] == "missing_token"
    assert status["error"] == "RING_PARTNER_ACCESS_TOKEN is not set"   # header text source unchanged


def test_live_partner_status_has_no_reason(partner_mode):
    assert api.get("/api/ring/status").json()["error_reason"] is None


def test_406_from_the_client_is_never_a_mode_change(partner_mode):
    partner_mode.error = RingBlockedError(
        "Ring blocked the request: missing headers or a datacenter IP (GET /v1/devices, HTTP 406)", status=406)
    status = api.get("/api/ring/status").json()
    assert status["source"] == "partner" and status["devices"] == []
    assert "Ring blocked the request" in status["error"]["message"]


def test_simulator_status_is_unchanged():
    status = api.get("/api/ring/status").json()
    assert status["source"] == "simulator" and "error_reason" not in status
