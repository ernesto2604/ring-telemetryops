import json
import logging

import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from src.utils.ring_partner_client import (
    RingForbiddenError,
    RingNotFoundError,
    RingPartnerClient,
    RingPartnerError,
    RingRateLimitError,
    RingServiceUnavailableError,
    RingTimeoutError,
    RingUnauthorizedError,
    build_device_health,
    normalize_device,
)

FAKE_TOKEN = "fake-partner-token-9876543210"
FRONT_ID = "ava1.ring.device.FRONT"
BACK_ID = "ava1.ring.device.BACK"

client = TestClient(server.app)


def device(device_id, name, status_attrs, capability_attrs, fetch_errors=None):
    resource = {"type": "devices", "id": device_id, "attributes": {"name": name}}
    status = {"attributes": status_attrs} if status_attrs is not None else None
    caps = {"attributes": capability_attrs} if capability_attrs is not None else None
    normalized = normalize_device(resource, status, caps)
    normalized["fetch_errors"] = fetch_errors or {}
    return normalized


class FakePartnerClient:
    """Stands in for RingPartnerClient: no network, records calls."""

    is_configured = True

    def __init__(self, devices=None, error=None):
        self.devices = devices if devices is not None else [
            device(FRONT_ID, "Front Door", {"online": True}, {}),
            device(BACK_ID, "Back Yard", {"online": False}, {}),
        ]
        self.error = error
        self.status_error = None
        self.calls = 0            # list_devices (discovery: 1 + 2N Ring requests)
        self.status_calls = []    # get_status (1 Ring request each)
        self.status_attrs = {}    # device_id -> raw status attributes for get_status

    async def list_devices(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.devices

    async def get_status(self, device_id, capabilities=None):
        self.status_calls.append((device_id, capabilities))
        if self.status_error:
            raise self.status_error
        if self.error:
            raise self.error
        if device_id in self.status_attrs:
            return {"device_id": device_id, **build_device_health(self.status_attrs[device_id], capabilities)}
        known = next(d for d in self.devices if d["device_id"] == device_id)
        return {"device_id": device_id, **known["device_health"]}


@pytest.fixture
def partner_mode(monkeypatch):
    """Switches the server module to Partner mode with a fake client; monkeypatch restores it."""
    fake = FakePartnerClient()
    monkeypatch.setenv("RING_PARTNER_ACCESS_TOKEN", FAKE_TOKEN)
    monkeypatch.delenv("RING_PARTNER_DEVICE_ID", raising=False)
    monkeypatch.setattr(server, "ring_source", "partner")
    monkeypatch.setattr(server, "ring_source_error", None)
    monkeypatch.setattr(server, "partner_client", fake)
    monkeypatch.setattr(server, "_partner_known_devices", {})
    return fake


@pytest.fixture
def no_llm(monkeypatch):
    """Any Bedrock call fails the test."""
    def fail(*_args, **_kwargs):
        raise AssertionError("Bedrock must not be called in these tests.")
    monkeypatch.setattr("src.agent.concierge.SupportConcierge._call_llm", fail)


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------

def test_default_source_is_simulator(monkeypatch):
    monkeypatch.delenv("RING_DATA_SOURCE", raising=False)
    monkeypatch.setenv("RING_USERNAME", "someone@example.com")  # must not trigger ring_doorbell
    monkeypatch.setenv("RING_PASSWORD", "not-a-real-password")

    source, ring_client, partner, error = server._load_ring_source()

    assert source == "simulator"
    assert ring_client.mode == "MOCK"
    assert ring_client.is_live is False  # simulator only: Ring credentials are never read
    assert partner is None and error is None


def test_removed_ring_doorbell_source_is_explicit_error(monkeypatch):
    monkeypatch.setenv("RING_DATA_SOURCE", "ring_doorbell")
    monkeypatch.setenv("RING_USERNAME", "someone@example.com")
    monkeypatch.setenv("RING_PASSWORD", "not-a-real-password")

    source, ring_client, partner, error = server._load_ring_source()

    assert source == "ring_doorbell" and partner is None
    assert error == "Unsupported RING_DATA_SOURCE value; expected one of: simulator, partner."
    assert ring_client.mode == "MOCK"  # the Twin routes keep working; nothing is presented as Ring data


def test_ring_client_refuses_live_mode():
    from src.utils.ring_client import RingClient

    with pytest.raises(ValueError):
        RingClient(mock=False)


def test_server_starts_in_mock_mode():
    response = client.get("/api/ring/status")

    assert response.status_code == 200
    assert response.json()["mode"] == "MOCK"
    assert response.json()["source"] == "simulator"


def test_token_alone_does_not_activate_partner(monkeypatch):
    monkeypatch.delenv("RING_DATA_SOURCE", raising=False)
    monkeypatch.setenv("RING_PARTNER_ACCESS_TOKEN", FAKE_TOKEN)

    source, _ring_client, partner, error = server._load_ring_source()

    assert source == "simulator"
    assert partner is None and error is None


def test_partner_source_with_token_builds_partner_client(monkeypatch):
    monkeypatch.setenv("RING_DATA_SOURCE", "partner")
    monkeypatch.setenv("RING_PARTNER_ACCESS_TOKEN", FAKE_TOKEN)

    source, ring_client, partner, error = server._load_ring_source()

    assert source == "partner"
    assert isinstance(partner, RingPartnerClient) and partner.is_configured
    assert ring_client.mode == "MOCK"  # simulator still backs the Digital Twin routes
    assert error is None


def test_partner_without_token_is_explicit_error(monkeypatch):
    monkeypatch.setenv("RING_DATA_SOURCE", "partner")
    monkeypatch.delenv("RING_PARTNER_ACCESS_TOKEN", raising=False)

    source, _ring_client, partner, error = server._load_ring_source()
    assert source == "partner" and partner is None
    assert "RING_PARTNER_ACCESS_TOKEN is not set" in error

    monkeypatch.setattr(server, "ring_source", source)
    monkeypatch.setattr(server, "partner_client", None)
    monkeypatch.setattr(server, "ring_source_error", error)

    status = client.get("/api/ring/status").json()
    assert status["mode"] == "ERROR" and status["devices"] == []
    for path in ("/api/ring/telemetry", "/api/ring/devices"):
        response = client.get(path)
        assert response.status_code == 503
        assert "RING_PARTNER_ACCESS_TOKEN is not set" in response.json()["detail"]
    analyze = client.post("/api/analyze", json={"source_mode": "ring", "camera_active": False})
    assert analyze.status_code == 503


def test_unknown_source_value_is_explicit_error(monkeypatch):
    monkeypatch.setenv("RING_DATA_SOURCE", "partnr")

    _source, _ring_client, partner, error = server._load_ring_source()

    assert partner is None
    assert "Unsupported RING_DATA_SOURCE" in error


# ---------------------------------------------------------------------------
# Partner endpoints use the fake client
# ---------------------------------------------------------------------------

def test_status_and_devices_use_partner_client(partner_mode):
    status = client.get("/api/ring/status").json()
    devices = client.get("/api/ring/devices")

    assert status["mode"] == "LIVE" and status["source"] == "partner"
    assert status["device_count"] == 2
    assert status["features"] == {"telemetry": True, "snapshot": False, "whep": True, "webhooks": False}
    assert devices.status_code == 200
    assert [d["device_id"] for d in devices.json()["devices"]] == [FRONT_ID, BACK_ID]
    assert partner_mode.calls == 2


def test_devices_endpoint_requires_partner_source():
    response = client.get("/api/ring/devices")

    assert response.status_code == 409


def test_telemetry_uses_official_adapter(partner_mode):
    default = client.get("/api/ring/telemetry").json()
    selected = client.get("/api/ring/telemetry", params={"device_id": BACK_ID}).json()

    assert default["_source"] == "ring_partner_api"
    assert default["device_id"] == FRONT_ID
    assert default["device_health"]["online"] is True
    assert default["device_health"]["rssi"] is None  # unknowns stay None
    assert default["webrtc_stats"]["inbound_rtp"]["jitter"] is None
    assert selected["device_id"] == BACK_ID
    assert selected["device_health"]["online"] is False


def test_telemetry_honours_configured_device_id(partner_mode, monkeypatch):
    monkeypatch.setenv("RING_PARTNER_DEVICE_ID", BACK_ID)

    assert client.get("/api/ring/telemetry").json()["device_id"] == BACK_ID


def test_unknown_device_id_is_404(partner_mode):
    response = client.get("/api/ring/telemetry", params={"device_id": "nope"})

    assert response.status_code == 404


def test_simulator_routes_still_work_in_partner_mode(partner_mode):
    preset = client.post("/api/telemetry", json={"mode": "preset", "scenario": "nominal"})

    assert preset.status_code == 200
    assert preset.json()["scenario"] == "nominal"


def test_whep_is_not_offered_with_partner_token(partner_mode):
    response = client.post("/api/ring/whep-session", content="v=0\r\n")

    assert response.status_code == 501
    assert FAKE_TOKEN not in response.text


# ---------------------------------------------------------------------------
# Errors: no silent fallback, safe translation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error, http_status, retry_after",
    [
        (RingRateLimitError("Ring API GET /v1/devices failed with HTTP 429", retry_after=2.4), 429, "3"),
        (RingServiceUnavailableError("HTTP 503", retry_after=5.0, detail="Server busy"), 503, "5"),
        (RingServiceUnavailableError("HTTP 503"), 503, None),
        (RingTimeoutError("Ring API GET /v1/devices timed out after 10.0s"), 504, None),
        (RingNotFoundError("HTTP 404", status=404), 404, None),
        (RingUnauthorizedError("HTTP 401", status=401), 502, None),
        (RingForbiddenError("HTTP 403", status=403), 502, None),
        (RingPartnerError("Ring API GET /v1/devices connection error (ClientConnectorError)"), 502, None),
    ],
)
def test_partner_errors_are_translated_without_fallback(partner_mode, error, http_status, retry_after):
    partner_mode.error = error

    for path in ("/api/ring/telemetry", "/api/ring/devices"):
        response = client.get(path)
        assert response.status_code == http_status
        assert response.headers.get("Retry-After") == retry_after
        detail = response.json()["detail"]
        assert detail["source"] == "ring_partner_api"
        assert detail["error"] == type(error).__name__
        assert "scenario" not in response.json()  # no simulated telemetry substituted

    status = client.get("/api/ring/status").json()
    assert status["mode"] == "ERROR"
    assert status["devices"] == []
    assert status["error"]["error"] == type(error).__name__


def test_analyze_does_not_fall_back_to_simulator_on_partner_error(partner_mode, no_llm):
    partner_mode.error = RingServiceUnavailableError("HTTP 503", retry_after=1.0)

    response = client.post(
        "/api/analyze",
        json={"source_mode": "ring", "camera_active": False,
              "telemetry": {"scenario": "nominal", "device_health": {"rssi": -48}}},
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


def test_token_never_appears_in_responses_or_logs(partner_mode, caplog):
    caplog.set_level(logging.DEBUG)
    partner_mode.error = RingUnauthorizedError(
        f"Ring API GET /v1/devices failed with HTTP 401: echo {FAKE_TOKEN}", status=401)

    bodies = [
        client.get("/api/ring/status").text,
        client.get("/api/ring/devices").text,
        client.get("/api/ring/telemetry").text,
        client.post("/api/analyze", json={"source_mode": "ring"}).text,
    ]
    partner_mode.error = None
    bodies.append(client.get("/api/ring/status").text)
    bodies.append(client.get("/api/ring/telemetry").text)

    for body in bodies:
        assert FAKE_TOKEN not in body
    assert FAKE_TOKEN not in caplog.text
    assert "[redacted]" in bodies[1]


# ---------------------------------------------------------------------------
# Analysis with official telemetry
# ---------------------------------------------------------------------------

def test_analyze_offline_device_is_not_healthy(partner_mode, no_llm):
    response = client.post(
        "/api/analyze",
        json={"source_mode": "ring", "device_id": BACK_ID, "camera_active": False},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["telemetry"]["_source"] == "ring_partner_api"
    assert body["diagnostic_result"]["diagnostic_status"] == "CRITICAL"
    assert body["diagnostic_result"]["diagnostic_status"] != "HEALTHY"


def test_analyze_unknown_metrics_report_insufficient_data(partner_mode, no_llm):
    response = client.post(
        "/api/analyze",
        json={"source_mode": "ring", "device_id": FRONT_ID, "camera_active": False,
              # Caller-supplied telemetry must not replace official data in Partner mode.
              "telemetry": {"device_health": {"rssi": -48, "battery_percentage": 100},
                            "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0}}}},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["analysis_skipped"] is False
    assert body["diagnostic_result"]["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert body["telemetry"]["device_health"]["rssi"] is None


def test_analyze_unknown_online_state_is_not_healthy(partner_mode, no_llm):
    partner_mode.devices = [device(FRONT_ID, "Front Door", None, None,
                                   fetch_errors={"status": {"error": "RingTimeoutError"}})]

    body = client.post("/api/analyze", json={"source_mode": "ring", "camera_active": False}).json()

    assert body["telemetry"]["device_health"]["online"] is None
    assert body["telemetry"]["fetch_errors"] == {"status": {"error": "RingTimeoutError"}}
    assert body["diagnostic_result"]["diagnostic_status"] == "INSUFFICIENT_DATA"


def test_analyze_with_active_camera_and_no_frame_is_telemetry_only(partner_mode, no_llm):
    response = client.post(
        "/api/analyze",
        json={"source_mode": "ring", "device_id": FRONT_ID, "camera_active": True},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["video_analysis"]["available"] is False
    assert "telemetry only" in body["video_analysis"]["reason"]
    assert body["diagnostic_result"]["visual_findings"] == [body["video_analysis"]["reason"]]
    assert "standby" not in json.dumps(body["diagnostic_result"]).lower()


def test_simulator_analysis_is_unchanged_in_default_mode(monkeypatch):
    calls = {}

    class FakeConcierge:
        def process_stream(self, **kwargs):
            calls.update(kwargs)
            return {"diagnostic_result": {"diagnostic_status": "DEGRADED", "root_cause_domain": "NETWORK"}}

    monkeypatch.setattr("src.api.server.SupportConcierge", FakeConcierge)

    response = client.post(
        "/api/analyze",
        json={"source_mode": "twin", "video_path": "good_internet.mp4",
              "telemetry": {"scenario": "nominal"}, "camera_active": False},
    )

    assert response.status_code == 200
    assert calls["telemetry"] == {"scenario": "nominal", "_source": server.CLIENT_TELEMETRY_SOURCE}
    assert "video_analysis" not in response.json()


def test_partner_analysis_never_claims_standby_and_ignores_client_metrics(partner_mode, monkeypatch):
    partner_mode.devices = [device(FRONT_ID, "Front Door",
                                   {"online": True, "battery_status": {"percentage": 5}},
                                   {"battery_status": {"supported": True}})]
    prompts = []

    def fake_llm(self, prompt, image_base64):
        prompts.append(prompt)
        return "not json"  # forces the deterministic fallback; no Bedrock call

    monkeypatch.setattr("src.agent.concierge.SupportConcierge._call_llm", fake_llm)

    response = client.post(
        "/api/analyze",
        json={"source_mode": "ring", "camera_active": False,
              "telemetry": {"device_health": {"rssi": -95, "battery_percentage": 100},
                            "webrtc_stats": {"inbound_rtp": {"jitter": 0.5, "packet_loss_fraction": 0.4}}}},
    )

    body = response.json()
    assert response.status_code == 200
    assert body["telemetry"]["_source"] == "ring_partner_api"
    assert body["telemetry"]["device_health"]["battery_percentage"] == 5  # server data, not the client's 100
    assert body["telemetry"]["device_health"]["rssi"] is None             # not the client's -95
    assert body["diagnostic_result"]["root_cause_domain"] == "HARDWARE_POWER"
    assert body["video_analysis"]["available"] is False
    assert "standby" not in json.dumps(body["diagnostic_result"]).lower()
    assert "TELEMETRY-ONLY DIAGNOSIS" in prompts[0]
    assert "CAMERA STATE: STANDBY" not in prompts[0]
    assert "-95" not in prompts[0]


# ---------------------------------------------------------------------------
# Caller-supplied frames and telemetry (RING-001 / RING-002)
# ---------------------------------------------------------------------------

def client_frame():
    """A JPEG that would be analyzable if the server accepted it."""
    import base64

    import cv2
    import numpy as np

    image = np.random.default_rng(0).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    return base64.b64encode(cv2.imencode(".jpg", image)[1]).decode("ascii")


FAKE_PARTNER_TELEMETRY = {
    "_source": "ring_partner_api",
    "scenario": "live",
    "device_health": {"online": True, "rssi": -90, "battery_percentage": 100},
    "webrtc_stats": {"inbound_rtp": {"jitter": 0.5, "packet_loss_fraction": 0.4,
                                     "frames_per_second": 30, "bitrate_kbps": 900}},
}


@pytest.fixture
def low_battery_partner(partner_mode, monkeypatch):
    """Official device with a reported anomaly, so the agent reaches the (mocked) model."""
    partner_mode.devices = [device(FRONT_ID, "Front Door",
                                   {"online": True, "battery_status": {"percentage": 5}},
                                   {"battery_status": {"supported": True}})]
    llm_calls = []

    def fake_llm(self, prompt, image_base64):
        llm_calls.append({"prompt": prompt, "image": image_base64})
        return "not json"  # forces the deterministic fallback; no Bedrock call

    monkeypatch.setattr("src.agent.concierge.SupportConcierge._call_llm", fake_llm)
    return partner_mode, llm_calls


def assert_official_telemetry_only(body):
    assert body["telemetry"]["_source"] == "ring_partner_api"
    assert body["telemetry"]["device_health"]["battery_percentage"] == 5   # Ring value, not the caller's
    assert body["telemetry"]["device_health"]["rssi"] is None              # not provided by Ring
    assert body["telemetry"]["webrtc_stats"]["inbound_rtp"]["jitter"] is None
    assert body["video_analysis"]["available"] is False
    assert body["video_analysis"]["reason"] == server.PARTNER_NO_FRAME_REASON
    assert body["diagnostic_result"]["visual_findings"] == [server.PARTNER_NO_FRAME_REASON]
    assert body["video_metrics"]["status"] == "UNAVAILABLE"
    assert body["video_metrics"]["sharpness_score"] is None


def test_partner_ring_mode_does_not_analyze_client_frame(low_battery_partner):
    fake, llm_calls = low_battery_partner

    response = client.post(
        "/api/analyze",
        json={"source_mode": "ring", "frame_base64": client_frame(), "camera_active": True},
    )

    body = response.json()
    assert response.status_code == 200
    assert_official_telemetry_only(body)
    assert llm_calls and llm_calls[0]["image"] is None       # the caller image never reaches the model
    assert "CAMERA STATE: ACTIVE" not in llm_calls[0]["prompt"]
    assert "standby" not in json.dumps(body["diagnostic_result"]).lower()


def test_partner_auto_mode_with_frame_and_fake_telemetry_uses_official_api(low_battery_partner):
    fake, llm_calls = low_battery_partner

    response = client.post(
        "/api/analyze",
        json={"source_mode": "auto", "frame_base64": client_frame(), "camera_active": True,
              "telemetry": FAKE_PARTNER_TELEMETRY},
    )

    body = response.json()
    assert response.status_code == 200
    assert fake.calls == 1                                   # Ring was queried despite the frame
    assert_official_telemetry_only(body)
    assert body["diagnostic_result"]["root_cause_domain"] == "HARDWARE_POWER"
    assert llm_calls[0]["image"] is None
    assert "-90" not in llm_calls[0]["prompt"]


def test_partner_auto_mode_ring_error_is_not_replaced_by_client_data(partner_mode, no_llm):
    partner_mode.error = RingServiceUnavailableError("HTTP 503", retry_after=1.0)

    response = client.post(
        "/api/analyze",
        json={"source_mode": "auto", "frame_base64": client_frame(), "camera_active": True,
              "telemetry": FAKE_PARTNER_TELEMETRY},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["source"] == "ring_partner_api"
    assert "diagnostic_result" not in response.json()


@pytest.mark.parametrize("partner", [False, True])
def test_caller_cannot_label_telemetry_as_official(partner, request, monkeypatch):
    if partner:
        request.getfixturevalue("partner_mode")
    calls = {}

    class FakeConcierge:
        def process_stream(self, **kwargs):
            calls.update(kwargs)
            return {"diagnostic_result": {"diagnostic_status": "DEGRADED"}}

    monkeypatch.setattr("src.api.server.SupportConcierge", FakeConcierge)
    frame = client_frame()

    response = client.post(
        "/api/analyze",
        json={"source_mode": "twin", "frame_base64": frame, "camera_active": True,
              "telemetry": FAKE_PARTNER_TELEMETRY},
    )

    assert response.status_code == 200
    assert calls["telemetry"]["_source"] == server.CLIENT_TELEMETRY_SOURCE
    assert calls["telemetry"]["device_health"] == FAKE_PARTNER_TELEMETRY["device_health"]  # values kept
    # Twin keeps its own frame and camera state.
    assert calls["frame_base64"] == frame
    assert calls["camera_active"] is True
    assert calls["no_frame_reason"] is None
    assert FAKE_PARTNER_TELEMETRY["_source"] == "ring_partner_api"   # request object not mutated


def test_simulator_auto_mode_with_frame_still_analyzes_twin_frame(monkeypatch):
    """Outside Partner mode, "auto" keeps its meaning: in MOCK it is the Digital Twin."""
    calls = {}

    class FakeConcierge:
        def process_stream(self, **kwargs):
            calls.update(kwargs)
            return {"diagnostic_result": {"diagnostic_status": "DEGRADED"}}

    monkeypatch.setattr("src.api.server.SupportConcierge", FakeConcierge)
    frame = client_frame()

    response = client.post(
        "/api/analyze",
        json={"source_mode": "auto", "frame_base64": frame, "camera_active": True,
              "telemetry": {"scenario": "nominal"}},
    )

    assert response.status_code == 200
    assert calls["frame_base64"] == frame
    assert calls["no_frame_reason"] is None
    assert calls["telemetry"] == {"scenario": "nominal", "_source": server.CLIENT_TELEMETRY_SOURCE}
