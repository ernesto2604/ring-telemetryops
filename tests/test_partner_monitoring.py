"""Partner monitoring: status-only refresh, poll config, error semantics, reported_at."""

import pytest
from aiohttp import web

import src.api.server as server
from src.utils.ring_partner_client import (
    RingNotFoundError,
    RingServiceUnavailableError,
    RingTimeoutError,
    build_device_health,
)
from src.utils.partner_telemetry import partner_device_to_telemetry
from tests.test_partner_api_server import BACK_ID, FRONT_ID, client, device, partner_mode, no_llm  # noqa: F401
from tests.test_ring_partner_client import FAKE_TOKEN as CLIENT_TOKEN, run_against  # noqa: F401

FRONT_CAPS = {"battery_status": {"supported": True}}


@pytest.fixture
def battery_partner(partner_mode):
    """One battery device whose capabilities come from discovery."""
    partner_mode.devices = [
        device(FRONT_ID, "Front Door", {"online": True, "battery_status": {"percentage": 80}}, FRONT_CAPS),
        device(BACK_ID, "Back Yard", {"online": True}, {}),
    ]
    partner_mode.status_attrs = {FRONT_ID: {"online": True, "battery_status": {"percentage": 79}}}
    return partner_mode


@pytest.fixture
def forbid_agent(monkeypatch):
    """Monitoring must never build the diagnostic agent (and so never reach Bedrock)."""
    class NoAgent:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Status polling must not run the diagnostic agent.")
    monkeypatch.setattr(server, "SupportConcierge", NoAgent)


# ---------------------------------------------------------------------------
# Request budget per poll
# ---------------------------------------------------------------------------

def test_known_device_poll_reads_status_only(battery_partner, forbid_agent):
    assert client.get("/api/ring/status").json()["mode"] == "LIVE"   # page load: one discovery
    for _ in range(3):                                                 # three heartbeat polls
        telemetry = client.get("/api/ring/telemetry").json()

    assert battery_partner.calls == 1                                  # no rediscovery / capabilities
    assert battery_partner.status_calls == [(FRONT_ID, FRONT_CAPS)] * 3
    assert telemetry["device_id"] == FRONT_ID
    assert telemetry["device_name"] == "Front Door"                    # kept from discovery
    assert telemetry["capabilities"] == FRONT_CAPS
    assert telemetry["device_health"]["battery_percentage"] == 79      # fresh status, discovery caps


def test_first_poll_without_discovery_discovers_once(battery_partner):
    client.get("/api/ring/telemetry")
    client.get("/api/ring/telemetry")
    client.get("/api/ring/telemetry", params={"device_id": BACK_ID})

    assert battery_partner.calls == 1
    assert [call[0] for call in battery_partner.status_calls] == [FRONT_ID, BACK_ID]


def test_unknown_device_id_triggers_discovery_and_404(battery_partner):
    client.get("/api/ring/devices")

    response = client.get("/api/ring/telemetry", params={"device_id": "ava1.ring.device.GONE"})

    assert response.status_code == 404
    assert battery_partner.calls == 2
    assert battery_partner.status_calls == []


def test_removed_device_404_forces_rediscovery_next_time(battery_partner):
    client.get("/api/ring/devices")
    battery_partner.status_error = RingNotFoundError("HTTP 404", status=404)

    assert client.get("/api/ring/telemetry").status_code == 404
    battery_partner.status_error = None
    client.get("/api/ring/telemetry", params={"device_id": FRONT_ID})

    assert battery_partner.calls == 2   # rediscovered after the 404


def test_partner_analysis_reuses_known_device(battery_partner, no_llm):
    client.get("/api/ring/status")

    body = client.post("/api/analyze", json={"source_mode": "ring", "device_id": FRONT_ID}).json()

    assert battery_partner.calls == 1
    assert battery_partner.status_calls == [(FRONT_ID, FRONT_CAPS)]
    assert body["telemetry"]["device_health"]["battery_percentage"] == 79


# ---------------------------------------------------------------------------
# API errors are not device state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error, http_status, retry_after",
    [
        (RingTimeoutError("Ring API GET /v1/devices/x/status timed out after 10.0s"), 504, None),
        (RingServiceUnavailableError("HTTP 503", retry_after=7.0, title="Device Unavailable",
                                     detail="Device is currently offline"), 503, "7"),
    ],
)
def test_status_poll_errors_are_reported_not_converted_to_offline(battery_partner, error, http_status, retry_after):
    client.get("/api/ring/devices")
    battery_partner.status_error = error

    response = client.get("/api/ring/telemetry")

    assert response.status_code == http_status
    assert response.headers.get("Retry-After") == retry_after
    assert "device_health" not in response.json()          # no telemetry, so no online=False
    assert response.json()["detail"]["error"] == type(error).__name__
    battery_partner.status_error = None
    assert client.get("/api/ring/telemetry").status_code == 200
    assert battery_partner.calls == 1                      # a transient error keeps the known device


def test_explicit_offline_and_unknown_online_pass_through(battery_partner):
    client.get("/api/ring/devices")

    battery_partner.status_attrs = {FRONT_ID: {"online": False}}
    offline = client.get("/api/ring/telemetry").json()["device_health"]
    battery_partner.status_attrs = {FRONT_ID: {}}
    unknown = client.get("/api/ring/telemetry").json()["device_health"]

    assert offline["online"] is False
    assert unknown["online"] is None


# ---------------------------------------------------------------------------
# Poll configuration (prototype defaults, not Ring recommendations)
# ---------------------------------------------------------------------------

def test_monitoring_defaults(partner_mode, monkeypatch):
    monkeypatch.delenv("RING_PARTNER_POLL_SECONDS", raising=False)
    monkeypatch.delenv("RING_PARTNER_OFFLINE_ALERT_READS", raising=False)
    monkeypatch.delenv("RING_PARTNER_OFFLINE_ALERT_MAX_GAP_SECONDS", raising=False)

    monitoring = client.get("/api/ring/status").json()["monitoring"]

    assert monitoring == {
        "mechanism": "polling",
        "status_endpoint": "GET /v1/devices/{device_id}/status",
        "poll_interval_seconds": 15,
        "offline_alert_consecutive_reads": 3,
        "offline_alert_max_gap_seconds": 60,   # 4 x poll interval
    }


@pytest.mark.parametrize(
    "poll, reads, expected",
    [
        ("60", "5", (60, 5)),
        ("abc", "zero", (15, 3)),     # invalid -> defaults
        ("2", "0", (15, 3)),          # below bounds -> defaults
        ("99999", "500", (15, 3)),    # above bounds -> defaults
    ],
)
def test_monitoring_is_configurable(partner_mode, monkeypatch, poll, reads, expected):
    monkeypatch.setenv("RING_PARTNER_POLL_SECONDS", poll)
    monkeypatch.setenv("RING_PARTNER_OFFLINE_ALERT_READS", reads)

    monitoring = client.get("/api/ring/status").json()["monitoring"]

    assert (monitoring["poll_interval_seconds"], monitoring["offline_alert_consecutive_reads"]) == expected


@pytest.mark.parametrize(
    "poll, gap, expected_gap",
    [
        ("60", None, 240),      # default follows the poll interval (4 x)
        ("60", "600", 600),
        ("60", "30", 240),      # shorter than one poll interval -> default
        ("15", "abc", 60),
        ("15", "999999", 60),   # above 24 h -> default
    ],
)
def test_offline_alert_gap_is_configurable(partner_mode, monkeypatch, poll, gap, expected_gap):
    monkeypatch.setenv("RING_PARTNER_POLL_SECONDS", poll)
    if gap is None:
        monkeypatch.delenv("RING_PARTNER_OFFLINE_ALERT_MAX_GAP_SECONDS", raising=False)
    else:
        monkeypatch.setenv("RING_PARTNER_OFFLINE_ALERT_MAX_GAP_SECONDS", gap)

    monitoring = client.get("/api/ring/status").json()["monitoring"]

    assert monitoring["offline_alert_max_gap_seconds"] == expected_gap


def test_rate_limit_without_retry_after_has_no_header(battery_partner):
    from src.utils.ring_partner_client import RingRateLimitError
    client.get("/api/ring/devices")
    battery_partner.status_error = RingRateLimitError("HTTP 429")   # no Retry-After from Ring

    response = client.get("/api/ring/telemetry")

    assert response.status_code == 429
    assert "Retry-After" not in response.headers   # the UI then applies its own backoff
    assert "device_health" not in response.json()


def test_simulator_status_has_no_partner_monitoring():
    assert "monitoring" not in client.get("/api/ring/status").json()


# ---------------------------------------------------------------------------
# reported_at semantics (last device check-in)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "online, value, expected, state",
    [
        (True, "2026-03-10T14:20:05.118Z", "2026-03-10T14:20:05.118Z", "reported_by_api"),
        (True, "1970-01-01T00:00:00Z", None, "not_reported"),   # documented placeholder
        (True, None, None, "not_reported"),
        (True, "yesterday", None, "invalid"),
        (True, 1710080405, None, "invalid"),
        (False, "2026-03-10T14:20:05Z", None, "not_reported"),  # gate on online first
        (None, "2026-03-10T14:20:05Z", None, "not_reported"),
    ],
)
def test_reported_at_is_validated(online, value, expected, state):
    attrs = {"reported_at": value}
    if online is not None:
        attrs["online"] = online

    health = build_device_health(attrs, None)
    telemetry = partner_device_to_telemetry({"device_id": FRONT_ID, "device_health": health})

    assert health["reported_at"] == expected
    assert health["measurement_status"]["reported_at"] == state
    assert telemetry["device_health"]["updated_at"] == expected
    assert telemetry["measurement_status"]["device_health"]["updated_at"] == state


def test_client_get_status_uses_given_capabilities(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_ACCESS_TOKEN", CLIENT_TOKEN)  # fake token; local test server only

    async def status(_request):
        return web.json_response({"data": {"type": "device-status", "id": "s",
                                           "attributes": {"online": True, "battery_status": {"percentage": 64}}}})

    route = {f"/v1/devices/{FRONT_ID}/status": status}
    with_caps, requests = run_against(route, lambda c: c.get_status(FRONT_ID, capabilities=FRONT_CAPS))
    without_caps, _ = run_against(route, lambda c: c.get_status(FRONT_ID))

    assert [r.path for r in requests] == [f"/v1/devices/{FRONT_ID}/status"]   # one request
    assert with_caps["battery_percentage"] == 64
    assert without_caps["battery_percentage"] is None
    assert without_caps["measurement_status"]["battery_percentage"] == "capabilities_unknown"


def test_status_refresh_keeps_capabilities_failure_from_discovery(partner_mode):
    caps_error = {"error": "RingTimeoutError", "status": None, "code": None, "message": "timed out"}
    partner_mode.devices = [device(FRONT_ID, "Front Door", None, None,
                                   fetch_errors={"status": {"error": "RingTimeoutError"}, "capabilities": caps_error})]
    partner_mode.status_attrs = {FRONT_ID: {"online": True}}
    client.get("/api/ring/devices")

    telemetry = client.get("/api/ring/telemetry").json()

    assert telemetry["device_health"]["online"] is True
    assert telemetry["fetch_errors"] == {"capabilities": caps_error}
    assert telemetry["measurement_status"]["device_health"]["battery_percentage"] == "capabilities_unknown"
