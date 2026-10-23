"""Tanda 8b: device settings gated by capabilities, Ring snapshots, Event History, per-domain verdict.
Every Ring call goes to the local fake server in tests/fake_ring_server.py (127.0.0.1) or a test double."""
import pytest

from src.utils.ring_partner_client import build_device_context

# ---------------------------------------------------------------------------
# T26: only the settings the device supports
# ---------------------------------------------------------------------------

LIVE_CONFIG = {"e2e_encryption": {"enabled": False},
               "image_enhancements": {"color_night_vision": "on", "hdr": "off", "ir_led_night_vision": "off",
                                      "auto_zoom_track": "off", "privacy_zones": []}}


def caps(*names):
    return {"image_enhancements": {"configurations": list(names)}}


def test_settings_missing_from_capabilities_are_null_not_false():
    context = build_device_context(None, caps("color_night_vision", "privacy_zones", "snapshot"), LIVE_CONFIG, None)
    enh = context["image_enhancements"]
    assert enh == {"color_night_vision": True, "hdr": None, "ir_led_night_vision": None,
                   "auto_zoom_track": None, "privacy_zones": 0}
    assert context["unsupported_settings"] == ["hdr", "ir_led_night_vision", "auto_zoom_track"]
    assert context["snapshot_supported"] is True


@pytest.mark.parametrize("setting", ["color_night_vision", "hdr", "ir_led_night_vision", "auto_zoom_track",
                                     "privacy_zones"])
def test_each_setting_follows_its_capability(setting):
    supported = build_device_context(None, caps(setting), LIVE_CONFIG, None)
    unsupported = build_device_context(None, caps("snapshot"), LIVE_CONFIG, None)
    assert supported["image_enhancements"][setting] is not None and setting not in supported["unsupported_settings"]
    assert unsupported["image_enhancements"][setting] is None and setting in unsupported["unsupported_settings"]


def test_without_capability_list_values_pass_through_and_no_snapshot():
    context = build_device_context(None, None, LIVE_CONFIG, None)
    assert context["image_enhancements"]["hdr"] is False
    assert context["unsupported_settings"] == [] and context["snapshot_supported"] is False


@pytest.mark.parametrize("snapshot, e2e, available", [
    (True, False, True), (True, True, False), (False, False, False),
])
def test_status_feature_snapshot_reflects_support_and_readability(monkeypatch, snapshot, e2e, available):
    from fastapi.testclient import TestClient

    import src.api.server as server
    from tests.test_partner_api_server import FakePartnerClient

    fake = FakePartnerClient()
    config = {"e2e_encryption": {"enabled": e2e}}
    fake.devices[0]["device_context"] = build_device_context(
        None, caps("snapshot") if snapshot else caps(), config, None)
    monkeypatch.setattr(server, "ring_source", "partner")
    monkeypatch.setattr(server, "ring_source_error", None)
    monkeypatch.setattr(server, "partner_client", fake)
    monkeypatch.setattr(server, "_partner_known_devices", {})

    assert TestClient(server.app).get("/api/ring/status").json()["features"]["snapshot"] is available


# ---------------------------------------------------------------------------
# T27: Ring snapshot (two steps) and Analyze now
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import base64  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.api.server as server  # noqa: E402
from src.agent.concierge import RING_SNAPSHOT_OK_FINDING, SupportConcierge  # noqa: E402
from src.utils.ring_partner_client import SNAPSHOT_WINDOW_MS, RingPartnerClient  # noqa: E402
from tests.fake_ring_server import (  # noqa: E402
    DEVICE_ID, PRESIGNED_PATH, FakeRing, blurry_frame, jpeg, pink_frame, sharp_frame,
)

TOKEN = "fake-snapshot-token-4455667788"
SNAPSHOT_POST = f"/v1/devices/{DEVICE_ID}/media/image/download"
api = TestClient(server.app)


@pytest.fixture
def ring(monkeypatch):
    fake = FakeRing().start()
    monkeypatch.setenv("RING_PARTNER_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("RING_PARTNER_BASE_URL", fake.url)
    monkeypatch.delenv("RING_PARTNER_DEVICE_ID", raising=False)
    yield fake
    fake.stop()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def partner(ring, sleeps, monkeypatch):
    """Server in Partner mode with the real client against the fake Ring; Bedrock is a double."""
    async def no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(server, "ring_source", "partner")
    monkeypatch.setattr(server, "ring_source_error", None)
    monkeypatch.setattr(server, "partner_client", RingPartnerClient(sleep=no_sleep))
    monkeypatch.setattr(server, "_partner_known_devices", {})
    monkeypatch.setattr(server, "_latest_snapshot", None)
    llm_calls = []

    def fake_llm(self, prompt, image_base64):
        llm_calls.append({"prompt": prompt, "image": image_base64})
        return "not json"   # deterministic fallback afterwards; no Bedrock

    monkeypatch.setattr(SupportConcierge, "_call_llm", fake_llm)

    def no_disk(*_args, **_kwargs):
        raise AssertionError("Ring frames must never be written to disk")

    monkeypatch.setattr(cv2, "imwrite", no_disk)
    return ring, llm_calls


def analyze():
    return api.post("/api/analyze", json={"source_mode": "ring", "camera_active": False})


def test_two_step_download_sends_no_token_to_the_presigned_url(ring):
    result = asyncio.run(RingPartnerClient().download_snapshot(DEVICE_ID))

    assert result["status"] == "ok" and result["content_type"] == "image/jpeg"
    assert result["media_timestamp"] == 1761210000000 and result["media_origin"] == "recording"
    post, presigned = ring.requests
    assert post["method"] == "POST" and post["path"] == SNAPSHOT_POST
    assert post["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert post["headers"]["User-Agent"].startswith("RingTelemetryOps/") and post["headers"]["Hardware_Id"]
    body = post["json"]
    assert body["type"] == "latest_in_range" and body["image_options"] == {"format": "jpeg"}
    assert body["end_timestamp"] - body["start_timestamp"] == SNAPSHOT_WINDOW_MS
    assert "resolution" not in json.dumps(body) and "components" not in body
    assert presigned["path"] == PRESIGNED_PATH
    assert "Authorization" not in presigned["headers"]
    assert TOKEN not in json.dumps(presigned["headers"])


def test_at_timestamp_and_multi_camera_component(ring):
    asyncio.run(RingPartnerClient().download_snapshot(DEVICE_ID, at_timestamp_ms=1761210000123,
                                                      component_id="cam-left"))
    body = ring.requests[0]["json"]
    assert body["type"] == "at_timestamp" and body["timestamp"] == 1761210000123
    assert body["components"] == [{"component_id": "cam-left"}]


def test_sharp_snapshot_is_image_ok_without_bedrock(partner, capsys, caplog):
    ring, llm_calls = partner
    caplog.set_level(logging.DEBUG)

    response = analyze()

    body = response.json()
    assert response.status_code == 200
    assert llm_calls == []                                    # healthy frame: never sent to Bedrock
    assert body["diagnostic_result"]["visual_findings"] == [RING_SNAPSHOT_OK_FINDING]
    assert body["video_metrics"]["resolution"] == "720x348"   # watermark band cropped (1280x620 -> 720 px)
    assert body["snapshot"]["media_origin"] == "recording"
    assert "image" not in body["snapshot"]
    snap = api.get("/api/ring/snapshot")
    assert snap.status_code == 200 and snap.content == ring.image
    assert snap.headers["cache-control"] == "no-store"
    assert snap.headers["x-media-timestamp"] == "1761210000000" and snap.headers["x-media-origin"] == "recording"
    image_b64 = base64.b64encode(ring.image).decode()[:60]
    assert image_b64 not in capsys.readouterr().out and image_b64 not in caplog.text   # never logged


@pytest.mark.parametrize("frame, issue", [(blurry_frame, "sharpness"), (pink_frame, "chromatic")])
def test_image_anomaly_reaches_bedrock_with_the_cropped_frame(partner, frame, issue):
    ring, llm_calls = partner
    ring.image = jpeg(frame())

    body = analyze().json()

    assert any(issue in i.lower() for i in body["video_metrics"]["issues"])
    assert llm_calls and llm_calls[0]["image"]               # the frame goes to Bedrock only now
    sent = cv2.imdecode(np.frombuffer(base64.b64decode(llm_calls[0]["image"]), np.uint8), cv2.IMREAD_COLOR)
    assert sent.shape[:2] == (348, 720)                      # cropped and resized, not the raw Ring image


def test_healthy_frame_is_not_sent_when_another_metric_triggers_bedrock(partner):
    ring, llm_calls = partner
    ring.status = {"online": True, "battery_status": {"percentage": 5}}
    ring.capabilities = {**ring.capabilities, "battery_status": {"supported": True}}

    analyze()

    assert llm_calls and all(call["image"] is None for call in llm_calls)
    assert "RING SNAPSHOT CHECKED LOCALLY" in llm_calls[0]["prompt"]


@pytest.mark.parametrize("replies, expected_reason, posts", [
    ([(416, None, "MEDIA_NOT_FOUND")], server.SNAPSHOT_REASONS["no_recent_media"], 1),
    ([(422, None, "CORRUPT_RECORDING")], server.SNAPSHOT_REASONS["corrupt_recording"], 1),
    ([(425, {"Retry-After": "20"}, "RECORDING_NOT_READY")], server.SNAPSHOT_REASONS["not_ready"], 1),
    ([(503, None, "SERVER_BUSY")], None, 2),
])
def test_snapshot_without_image_explains_why(partner, replies, expected_reason, posts):
    ring, llm_calls = partner
    ring.snapshot_replies = replies

    body = analyze().json()

    finding = body["diagnostic_result"]["visual_findings"]
    if expected_reason is None:
        assert "device_offline_503" in finding[0]
    else:
        assert finding == [expected_reason]
    assert ring.paths("POST").count(SNAPSHOT_POST) == posts and PRESIGNED_PATH not in ring.paths()
    assert llm_calls == [] and "snapshot" not in body
    assert api.get("/api/ring/snapshot").status_code == 404


@pytest.mark.parametrize("first", [(425, None, "RECORDING_NOT_READY"), (503, None, "SERVER_BUSY"),
                                   (500, None, None)])
def test_transient_snapshot_reply_is_retried_then_succeeds(partner, sleeps, first):
    ring, _ = partner
    ring.snapshot_replies = [first, None]

    body = analyze().json()

    assert body["diagnostic_result"]["visual_findings"] == [RING_SNAPSHOT_OK_FINDING]
    assert ring.paths("POST").count(SNAPSHOT_POST) == 2 and sleeps == [1.0]


def test_not_ready_waits_stay_within_the_budget(ring):
    ring.snapshot_replies = [(425, None, "RECORDING_NOT_READY")]
    waits = []

    async def record(seconds):
        waits.append(seconds)

    result = asyncio.run(RingPartnerClient(sleep=record).download_snapshot(DEVICE_ID))
    assert result["status"] == "not_ready" and sum(waits) <= 15.0 and waits == [1.0, 2.0, 4.0, 8.0]


@pytest.mark.parametrize("image", [b"not an image at all", b"\xff\xd8" + b"\x00" * 64,
                                   b"\xff\xd8" + b"\x00" * (10 * 1024 * 1024 + 1)],
                         ids=["not-an-image", "jpeg-signature-only", "over-10-mb"])
def test_corrupt_or_oversized_bytes_are_unreadable_not_errors(partner, image):
    ring, llm_calls = partner
    ring.image = image

    response = analyze()

    assert response.status_code == 200
    assert response.json()["diagnostic_result"]["visual_findings"] == [server.SNAPSHOT_REASONS["unreadable"]]
    assert llm_calls == []


@pytest.mark.parametrize("configurations, capabilities, reason", [
    ({"e2e_encryption": {"enabled": True}}, None, server.E2E_ENCRYPTED_REASON),
    (None, {"image_enhancements": {"configurations": ["color_night_vision"]}}, server.PARTNER_NO_FRAME_REASON),
])
def test_no_snapshot_request_when_encrypted_or_unsupported(partner, configurations, capabilities, reason):
    ring, _ = partner
    if configurations:
        ring.configurations = configurations
    if capabilities:
        ring.capabilities = capabilities

    body = analyze().json()

    assert body["diagnostic_result"]["visual_findings"] == [reason]
    assert SNAPSHOT_POST not in ring.paths()


@pytest.mark.parametrize("status, reason", [(401, "token_rejected"), (403, "token_rejected"),
                                            (406, "blocked_406")])
def test_credential_errors_on_snapshot_keep_their_reason(partner, status, reason):
    ring, _ = partner
    ring.snapshot_replies = [(status, None, None)]

    response = analyze()

    assert response.status_code == 502 and response.json()["detail"]["reason"] == reason
    assert TOKEN not in response.text


def test_simulator_analysis_never_requests_a_snapshot(ring):
    frame = base64.b64encode(jpeg(sharp_frame())).decode()
    response = api.post("/api/analyze", json={"source_mode": "twin", "camera_active": True, "frame_base64": frame,
                                              "telemetry": {"scenario": "nominal"}})
    assert response.status_code == 200 and ring.requests == []


# ---------------------------------------------------------------------------
# T28: Event History (polling instead of webhooks)
# ---------------------------------------------------------------------------

from src.utils.ring_partner_client import RingRateLimitError, RingUnauthorizedError  # noqa: E402
from tests.fake_ring_server import history_event  # noqa: E402

EVENTS_PATH = f"/v1/history/devices/{DEVICE_ID}/events"


def test_events_follow_at_most_two_pages_and_split_subtypes(ring):
    ring.event_pages = {
        "": [history_event("e3", "motion.human", 3000), history_event("e2", "ding", 2000, reviewed=True)],
        "p2": [history_event("e1", "on_demand", 1000)],
        "p3": [history_event("e0", "motion", 500)],
    }
    ring.event_links = {"": "p2", "p2": "p3"}

    events = asyncio.run(RingPartnerClient().list_events(DEVICE_ID))

    assert [e["id"] for e in events] == ["e3", "e2", "e1"]          # page 3 not fetched
    assert events[0] == {"id": "e3", "event_type": "motion", "sub_type": "human", "start": 3000, "end": 23000,
                         "is_third_party_reviewed": False}
    assert events[1]["sub_type"] is None and events[1]["is_third_party_reviewed"] is True
    first, second = [r for r in ring.requests if r["path"] == EVENTS_PATH]
    assert first["query"]["event_types"] == "ding,motion,on_demand"
    assert second["query"] == {"page[key]": "p2"}
    assert all(r["headers"]["Hardware_Id"] for r in ring.requests)


def test_empty_history_is_normal(ring):
    assert asyncio.run(RingPartnerClient().list_events(DEVICE_ID)) == []


@pytest.mark.parametrize("status, error", [(401, RingUnauthorizedError), (429, RingRateLimitError)])
def test_event_history_errors_are_typed(ring, status, error):
    ring.events_error = (status, {"Retry-After": "30"})

    async def call():
        with pytest.raises(error):
            await RingPartnerClient().list_events(DEVICE_ID)

    asyncio.run(call())


@pytest.fixture
def events_partner(partner, monkeypatch):
    monkeypatch.setattr(server, "_partner_events", [])
    monkeypatch.setattr(server, "_seen_event_ids", set())
    monkeypatch.setattr(server, "_events_poll", {"last_query": None, "paused_until": 0.0})
    monkeypatch.setattr(server, "_SERVER_STARTED_MS", 1_761_000_000_000)
    return partner


def new_start(offset_ms=60_000):
    return server._SERVER_STARTED_MS + offset_ms


def poll():
    server._events_poll["last_query"] = None   # next poll cycle
    return api.get("/api/ring/events")


def test_new_event_with_healthy_frame_is_silent(events_partner):
    ring, llm_calls = events_partner
    ring.event_pages = {"": [history_event("ev-1", "motion.human", new_start())]}

    body = poll().json()

    assert body["reports"] == [] and llm_calls == []
    assert body["events"][0]["id"] == "ev-1" and body["events"][0]["sub_type"] == "human"
    post = next(r for r in ring.requests if r["path"] == SNAPSHOT_POST)
    assert post["json"]["type"] == "at_timestamp" and post["json"]["timestamp"] == new_start()


@pytest.mark.parametrize("event_type, trigger", [("ding", "ding"), ("motion.vehicle", "motion"),
                                                 ("on_demand", "live")])
def test_new_event_with_blurry_frame_returns_a_report(events_partner, event_type, trigger):
    ring, llm_calls = events_partner
    ring.image = jpeg(blurry_frame())
    ring.event_pages = {"": [history_event("ev-2", event_type, new_start())]}

    body = poll().json()

    (report,) = body["reports"]
    assert report["trigger"] == trigger and report["event"]["id"] == "ev-2"
    assert report["diagnostic_result"]["diagnostic_status"] in ("DEGRADED", "CRITICAL")
    assert llm_calls and llm_calls[0]["image"]


def test_events_before_server_start_are_listed_but_not_analyzed(events_partner):
    ring, _ = events_partner
    ring.event_pages = {"": [history_event("old-1", "ding", server._SERVER_STARTED_MS - 1)]}

    body = poll().json()

    assert [e["id"] for e in body["events"]] == ["old-1"] and body["reports"] == []
    assert SNAPSHOT_POST not in ring.paths()


def test_repeated_event_is_analyzed_once(events_partner):
    ring, _ = events_partner
    ring.image = jpeg(blurry_frame())
    ring.event_pages = {"": [history_event("ev-3", "ding", new_start())]}

    assert len(poll().json()["reports"]) == 1
    second = poll().json()

    assert second["reports"] == [] and [e["id"] for e in second["events"]] == ["ev-3"]
    assert ring.paths("POST").count(SNAPSHOT_POST) == 1


def test_one_ring_query_per_poll_interval(events_partner):
    ring, _ = events_partner
    poll()
    api.get("/api/ring/events")   # same poll cycle: served from memory
    assert ring.paths().count(EVENTS_PATH) == 1


def test_429_pauses_event_queries_for_retry_after(events_partner):
    ring, _ = events_partner
    ring.events_error = (429, {"Retry-After": "30"})

    first = poll()
    second = poll()

    assert first.status_code == 429 and first.headers["Retry-After"] == "30"
    assert second.status_code == 429 and second.json()["detail"]["reason"] == "rate_limited"
    assert ring.paths().count(EVENTS_PATH) == 1   # no Ring query while paused


def test_401_on_events_keeps_the_token_rejected_reason(events_partner):
    ring, _ = events_partner
    ring.events_error = (401, None)

    response = poll()

    assert response.status_code == 502 and response.json()["detail"]["reason"] == "token_rejected"
    assert TOKEN not in response.text


def test_events_require_partner_mode():
    assert api.get("/api/ring/events").status_code == 409


# ---------------------------------------------------------------------------
# T29: per-domain verdict in Partner reports
# ---------------------------------------------------------------------------

def domains_by_name(body):
    return {d["domain"]: d for d in body["checked_domains"]}


def test_healthy_image_is_a_partial_check_with_customer_mitigation(partner):
    body = analyze().json()

    domains = domains_by_name(body)
    assert domains["image"]["status"] == "ok"
    assert domains["network"] == {"domain": "network", "status": "not_available",
                                  "detail": "Ring does not share Wi-Fi or power data with partner apps"}
    assert domains["power"]["status"] == "not_available"
    diag = body["diagnostic_result"]
    assert diag["diagnostic_status"] == "INSUFFICIENT_DATA"            # unchanged
    assert diag["recommended_mitigation"] == server.PARTNER_IMAGE_OK_MITIGATION
    assert "Collect the missing metrics" not in json.dumps(body)


def test_image_anomaly_is_an_image_issue_and_keeps_its_mitigation(partner):
    ring, _ = partner
    ring.image = jpeg(blurry_frame())

    body = analyze().json()

    image = domains_by_name(body)["image"]
    assert image["status"] == "issue" and "sharpness" in image["detail"].lower()
    assert body["diagnostic_result"]["diagnostic_status"] != "INSUFFICIENT_DATA"
    assert body["diagnostic_result"]["recommended_mitigation"] not in (
        server.PARTNER_IMAGE_OK_MITIGATION, server.PARTNER_NO_IMAGE_MITIGATION)


def test_without_image_nothing_is_checked(partner):
    ring, _ = partner
    ring.snapshot_replies = [(416, None, "MEDIA_NOT_FOUND")]

    body = analyze().json()

    image = domains_by_name(body)["image"]
    assert image == {"domain": "image", "status": "not_available",
                     "detail": server.SNAPSHOT_REASONS["no_recent_media"]}
    assert body["diagnostic_result"]["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert body["diagnostic_result"]["recommended_mitigation"] == server.PARTNER_NO_IMAGE_MITIGATION


def test_simulator_reports_have_no_checked_domains():
    frame = base64.b64encode(jpeg(sharp_frame())).decode()
    body = api.post("/api/analyze", json={"source_mode": "twin", "camera_active": True, "frame_base64": frame,
                                          "telemetry": {"scenario": "nominal"}}).json()
    assert "checked_domains" not in body
    assert body["diagnostic_result"]["recommended_mitigation"] != server.PARTNER_NO_IMAGE_MITIGATION


# ---------------------------------------------------------------------------
# T35 Analyze now starts from the newest event's recording
# ---------------------------------------------------------------------------

EVENT_STARTS = [1_761_209_000_000, 1_761_208_000_000, 1_761_207_000_000, 1_761_206_000_000]


@pytest.fixture
def with_events(partner):
    ring, llm_calls = partner
    ring.event_pages[""] = [history_event(f"evt-{i}", "motion", start) for i, start in enumerate(EVENT_STARTS)]
    return ring


def snapshot_bodies(ring):
    return [r["json"] for r in ring.requests if r["method"] == "POST" and r["path"] == SNAPSHOT_POST]


def test_analyze_now_uses_the_newest_event_recording(with_events):
    body = analyze().json()

    assert body["diagnostic_result"]["visual_findings"] == [RING_SNAPSHOT_OK_FINDING]
    assert [b["type"] for b in snapshot_bodies(with_events)] == ["at_timestamp"]
    assert snapshot_bodies(with_events)[0]["timestamp"] == EVENT_STARTS[0]


def test_analyze_now_moves_to_the_next_event_after_a_422(with_events):
    with_events.snapshot_replies = [(422, None, "CORRUPT_RECORDING"), None]

    body = analyze().json()

    assert body["diagnostic_result"]["visual_findings"] == [RING_SNAPSHOT_OK_FINDING]
    assert [b.get("timestamp") for b in snapshot_bodies(with_events)] == EVENT_STARTS[:2]


def test_analyze_now_falls_back_to_latest_in_range_after_three_events(with_events):
    with_events.snapshot_replies = [(422, None, "CORRUPT_RECORDING"), (416, None, "MEDIA_NOT_FOUND"),
                                    (422, None, "CORRUPT_RECORDING"), None]

    body = analyze().json()

    assert body["diagnostic_result"]["visual_findings"] == [RING_SNAPSHOT_OK_FINDING]
    bodies = snapshot_bodies(with_events)
    assert [b["type"] for b in bodies] == ["at_timestamp"] * 3 + ["latest_in_range"]
    assert [b["timestamp"] for b in bodies[:3]] == EVENT_STARTS[:3]


def test_analyze_now_without_events_uses_only_latest_in_range(partner):
    ring, _ = partner

    analyze()

    assert [b["type"] for b in snapshot_bodies(ring)] == ["latest_in_range"]
    assert f"/v1/history/devices/{DEVICE_ID}/events" in ring.paths("GET")


def test_analyze_now_reuses_the_polled_events_without_a_new_query(partner, monkeypatch):
    ring, _ = partner
    monkeypatch.setattr(server, "_partner_events", [{"id": "e", "event_type": "ding", "sub_type": None,
                                                     "start": EVENT_STARTS[1], "end": None,
                                                     "is_third_party_reviewed": False}])
    monkeypatch.setattr(server, "_events_poll", {"last_query": None, "paused_until": 0.0, "device_id": DEVICE_ID})

    analyze()

    assert snapshot_bodies(ring)[0]["timestamp"] == EVENT_STARTS[1]
    assert not any("/history/" in p for p in ring.paths())


@pytest.mark.parametrize("status, http_status", [(401, 502), (429, 200)], ids=["token-rejected", "rate-limited"])
def test_analyze_now_does_not_try_other_events_after_a_credential_or_ring_error(with_events, status, http_status):
    with_events.snapshot_replies = [(status, {"Retry-After": "120"}, None), None]

    response = analyze()

    assert response.status_code == http_status
    assert len(snapshot_bodies(with_events)) == 1
    if status == 401:
        assert response.json()["detail"]["reason"] == "token_rejected"
