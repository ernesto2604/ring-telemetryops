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


# ---------------------------------------------------------------------------
# T22: device settings from GET /v1/devices/{id}?include=...
# ---------------------------------------------------------------------------

from src.utils.partner_telemetry import partner_device_to_telemetry  # noqa: E402
from src.utils.ring_partner_client import build_device_context  # noqa: E402

DEVICE_ID = "ava1.ring.device.CTX1"
ZONE = {"id": "z1", "vertices": [{"x": 0.1, "y": 0.1}, {"x": 0.9, "y": 0.1}, {"x": 0.5, "y": 0.9}]}

# Fictitious document shaped like the documented JSON:API format (top-level `included`).
INCLUDE_DOCUMENT = {
    "data": {"type": "devices", "id": DEVICE_ID, "attributes": {"name": "Porch"}},
    "included": [
        {"type": "device-status", "id": DEVICE_ID,
         "attributes": {"online": True, "reported_at": "2026-10-20T10:00:00Z",
                        "audio": {"snooze": {"active": True, "until": "2026-10-20T11:00:00Z"}}}},
        {"type": "device-capabilities", "id": DEVICE_ID,
         "attributes": {"video": {"codecs": ["AVC"], "max_resolution": 1080, "supported_resolutions": [1080, 720]},
                        "image_enhancements": {"configurations": ["hdr", "privacy_zones"]}}},
        {"type": "device-configurations", "id": DEVICE_ID,
         "attributes": {"e2e_encryption": {"enabled": False},
                        "motion_detection": {"enabled": "on", "motion_zones": [ZONE]},
                        "image_enhancements": {"color_night_vision": "off", "hdr": "off",
                                               "ir_led_night_vision": "off", "auto_zoom_track": "on",
                                               "privacy_zones": []},
                        "video": {"component_id": None}}},
        {"type": "device-location", "id": DEVICE_ID, "attributes": {"country": "US", "state": "WA"}},
    ],
}


def single_device_routes(device_handler, status_attrs=None, capability_attrs=None):
    async def devices(_request):
        return web.json_response({"data": [{"type": "devices", "id": DEVICE_ID, "attributes": {"name": "Porch"}}]})

    async def status(_request):
        return web.json_response({"data": {"type": "device-status", "id": DEVICE_ID,
                                           "attributes": status_attrs or {"online": True}}})

    async def capabilities(_request):
        return web.json_response({"data": {"type": "device-capabilities", "id": DEVICE_ID,
                                           "attributes": capability_attrs or {}}})

    return {"/v1/devices": devices, f"/v1/devices/{DEVICE_ID}": device_handler,
            f"/v1/devices/{DEVICE_ID}/status": status, f"/v1/devices/{DEVICE_ID}/capabilities": capabilities}


def test_include_call_fills_device_context_in_one_request(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_HARDWARE_ID", "hw-test")

    async def device(request):
        assert request.query["include"] == "status,capabilities,configurations,location"
        return web.json_response(INCLUDE_DOCUMENT)

    (found,), requests = run_against(single_device_routes(device), lambda c: c.list_devices())

    assert [r.path for r in requests] == ["/v1/devices", f"/v1/devices/{DEVICE_ID}"]  # no individual calls
    assert_ring_headers(requests, "hw-test")
    assert found["device_health"]["online"] is True
    assert found["capabilities"]["video"]["max_resolution"] == 1080
    assert found["device_context"] == {
        "e2e_encryption_enabled": False,
        "motion_detection": {"enabled": True, "zones": 1},
        "image_enhancements": {"color_night_vision": False, "hdr": False, "ir_led_night_vision": False,
                               "auto_zoom_track": True, "privacy_zones": 0},
        "video": {"max_resolution": 1080, "supported_resolutions": [1080, 720]},
        "location": {"country": "US", "state": "WA"},
        "snooze": {"active": True, "until": "2026-10-20T11:00:00Z"},
    }


@pytest.mark.parametrize("reply", [
    lambda: web.json_response({"data": INCLUDE_DOCUMENT["data"]}),          # no `included`
    lambda: jsonapi_error(500, title="Internal"),                            # combined call fails
])
def test_without_included_falls_back_to_individual_calls(reply):
    async def device(_request):
        return reply()

    (found,), requests = run_against(single_device_routes(device, capability_attrs={"video": {"max_resolution": 720}}),
                                     lambda c: c.list_devices(), max_transient_retries=0)

    paths = [r.path for r in requests]
    assert f"/v1/devices/{DEVICE_ID}/status" in paths and f"/v1/devices/{DEVICE_ID}/capabilities" in paths
    assert found["device_health"]["online"] is True and found["fetch_errors"] == {}
    context = found["device_context"]
    assert context["e2e_encryption_enabled"] is None
    assert context["image_enhancements"]["hdr"] is None and context["location"] == {"country": None, "state": None}
    assert context["video"]["max_resolution"] == 720   # capabilities still come from the fallback


def test_unauthorized_combined_call_aborts_listing():
    async def device(_request):
        return jsonapi_error(401, title="Unauthorized")

    async def call(client):
        with pytest.raises(RingUnauthorizedError):
            await client.list_devices()

    run_against(single_device_routes(device), call)


def test_null_and_malformed_fields_stay_null():
    context = build_device_context(
        {"audio": {"snooze": {"active": "yes", "until": None}}},
        {"video": {"supported_resolutions": "1080"}},
        {"e2e_encryption": {"enabled": "true"},
         "motion_detection": {"enabled": None, "motion_zones": None},
         "image_enhancements": {"color_night_vision": "maybe", "privacy_zones": None}},
        None,
    )
    assert context["e2e_encryption_enabled"] is None          # the string "true" is not the boolean
    assert context["motion_detection"] == {"enabled": None, "zones": None}
    assert context["image_enhancements"]["color_night_vision"] is None
    assert context["image_enhancements"]["privacy_zones"] is None
    assert context["video"] == {"max_resolution": None, "supported_resolutions": None}
    assert context["snooze"] == {"active": None, "until": None}
    assert context["location"] == {"country": None, "state": None}


def test_status_endpoint_and_telemetry_expose_device_context(partner_mode):
    context = build_device_context(None, None, INCLUDE_DOCUMENT["included"][2]["attributes"], None)
    partner_mode.devices[0]["device_context"] = context

    status = api.get("/api/ring/status").json()
    assert status["devices"][0]["device_context"] == context
    assert partner_device_to_telemetry(partner_mode.devices[0])["device_context"] == context
    assert api.get("/api/ring/telemetry").json()["device_context"] == context


def test_bedrock_prompt_gets_device_settings_only_in_partner_mode(monkeypatch):
    from src.agent import concierge as concierge_module

    section = concierge_module._device_settings_section(
        build_device_context(None, None, INCLUDE_DOCUMENT["included"][2]["attributes"], None))
    assert "DEVICE SETTINGS REPORTED BY RING" in section
    assert "- Color night vision   : off" in section and "(zones: 1)" in section
    assert DEVICE_ID not in section and "z1" not in section          # no IDs
    assert concierge_module._device_settings_section(None) == ""     # simulator telemetry has no context
