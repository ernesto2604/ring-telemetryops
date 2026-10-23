"""T40: signed Ring webhooks. Invented HMAC key; Ring is the local fake server; Bedrock is a double."""
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import src.api.server as server
from src.api.funnel import funnel
from tests.fake_ring_server import DEVICE_ID, blurry_frame, history_event, jpeg
from tests.test_partner_snapshots_events import (  # noqa: F401
    SNAPSHOT_POST, events_partner, new_start, partner, poll, ring, sleeps,
)
from tools.send_test_webhook import build_payload, sign

KEY = "test-hmac-key-invented-for-pytest"
api = TestClient(server.app)


@pytest.fixture
def hooks(events_partner, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RING_PARTNER_HMAC_KEY", KEY)
    monkeypatch.delenv("RING_WEBHOOK_SKIP_SUBTYPES", raising=False)
    monkeypatch.delenv("RING_WEBHOOK_MAX_BODY_BYTES", raising=False)
    monkeypatch.setattr(server, "_webhook_request_ids", server.deque(maxlen=1000))
    monkeypatch.setattr(server, "_webhook_reports", [])
    monkeypatch.setattr(server, "_webhook_state", {"received": {}, "devices": {}})
    funnel.reset()
    yield events_partner
    funnel.reset()


def send(payload=None, *, body=None, key=KEY, signature=None, headers=None):
    body = body if body is not None else json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if signature is not None:
        hdrs["X-Signature"] = signature
    elif key is not None:
        hdrs["X-Signature"] = sign(body, key)
    return api.post("/webhooks/ring", content=body, headers=hdrs)


def motion(timestamp=None, sub_type="human", kind="motion_detected"):
    return build_payload(kind, DEVICE_ID, timestamp or new_start(), sub_type)


def snapshot_posts(ring):  # noqa: F811
    return ring.paths("POST").count(SNAPSHOT_POST)


def test_valid_signature_is_accepted_and_analyzed(hooks):
    ring, _ = hooks
    response = send(motion())
    assert response.status_code == 200 and response.json() == {"status": "accepted"}
    post = next(r for r in ring.requests if r["path"] == SNAPSHOT_POST)
    assert post["json"]["type"] == "at_timestamp" and post["json"]["timestamp"] == new_start()
    assert api.get("/api/metrics").json()["events_seen"] == 1


@pytest.mark.parametrize("signature", ["sha256=" + "0" * 64, "sha256=", "md5=abc", "deadbeef",
                                       "sha256=" + "f" * 63])
def test_invalid_signature_is_401_and_nothing_is_processed(hooks, signature):
    ring, _ = hooks
    response = send(motion(), signature=signature)
    assert response.status_code == 401 and response.json() == {"detail": "Unauthorized"}
    assert snapshot_posts(ring) == 0 and server._webhook_request_ids == server.deque()


def test_wrong_key_is_401(hooks):
    assert send(motion(), key="another-invented-key").status_code == 401


def test_missing_signature_is_401(hooks):
    ring, _ = hooks
    assert send(motion(), key=None).status_code == 401
    assert snapshot_posts(ring) == 0


def test_missing_key_is_503_and_never_accepts_unsigned_messages(hooks, monkeypatch):
    monkeypatch.setenv("RING_PARTNER_HMAC_KEY", "  ")
    for kwargs in ({"key": None}, {"key": KEY}):
        response = send(motion(), **kwargs)
        assert response.status_code == 503 and "RING_PARTNER_HMAC_KEY" in response.json()["detail"]
    assert snapshot_posts(hooks[0]) == 0


def test_reserialized_body_no_longer_matches_the_signature(hooks):
    payload = motion()
    original = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    reserialized = json.dumps(json.loads(original), indent=2).encode("utf-8")
    assert send(body=reserialized, signature=sign(original, KEY)).status_code == 401
    assert send(body=original, signature=sign(original, KEY)).status_code == 200


def test_duplicate_request_id_is_200_without_reprocessing(hooks):
    ring, _ = hooks
    payload = motion()
    assert send(payload).json() == {"status": "accepted"}
    assert send(payload).json() == {"status": "duplicate"}
    assert snapshot_posts(ring) == 1


def test_unknown_type_is_200_and_ignored(hooks):
    payload = build_payload("subscription_activated", DEVICE_ID, new_start())
    response = send(payload)
    assert response.status_code == 200 and response.json() == {"status": "ignored"}
    assert snapshot_posts(hooks[0]) == 0


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b""])
def test_malformed_json_with_valid_signature_is_400(hooks, body):
    assert send(body=body).status_code == 400


def test_body_over_the_limit_is_413(hooks, monkeypatch):
    monkeypatch.setenv("RING_WEBHOOK_MAX_BODY_BYTES", "2048")
    payload = motion()
    payload["meta"]["padding"] = "x" * 4096
    assert send(payload).status_code == 413
    assert send(motion()).status_code == 200


def test_default_body_limit_is_64_kb(hooks):
    payload = motion()
    payload["meta"]["padding"] = "x" * (64 * 1024)
    assert send(payload).status_code == 413


def test_receiver_is_404_outside_partner_mode(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_HMAC_KEY", KEY)
    monkeypatch.setattr(server, "ring_source", "simulator")
    assert send(motion()).status_code == 404


def test_device_events_update_state_without_image_analysis(hooks):
    ring, llm_calls = hooks
    for kind in ("device_offline", "device_online", "device_added", "device_removed"):
        assert send(build_payload(kind, DEVICE_ID, new_start())).json() == {"status": "accepted"}
    assert server._webhook_state["devices"] == {DEVICE_ID: "device_removed"}
    assert server._webhook_state["received"]["device_offline"] == 1
    assert snapshot_posts(ring) == 0 and llm_calls == []
    assert api.get("/api/metrics").json()["events_seen"] == 0


def test_motion_with_a_healthy_snapshot_is_silent(hooks):
    _, llm_calls = hooks
    assert send(motion()).status_code == 200
    assert poll().json()["reports"] == [] and llm_calls == []
    assert api.get("/api/metrics").json()["analyses_total"] == 1


@pytest.mark.parametrize("kind, trigger", [("motion_detected", "motion"), ("button_press", "ding")])
def test_blurry_snapshot_reports_through_the_events_endpoint(hooks, kind, trigger):
    ring, llm_calls = hooks
    ring.image = jpeg(blurry_frame())
    assert send(motion(kind=kind)).status_code == 200

    (report,) = poll().json()["reports"]
    assert report["trigger"] == trigger and report["event"]["origin"] == "webhook"
    assert report["diagnostic_result"]["diagnostic_status"] in ("DEGRADED", "CRITICAL")
    assert report["report_id"] and llm_calls
    assert poll().json()["reports"] == []        # handed over once


def test_skip_subtypes_filter_motion_only(hooks, monkeypatch):
    ring, _ = hooks
    monkeypatch.setenv("RING_WEBHOOK_SKIP_SUBTYPES", "vehicle, other_motion")
    assert send(motion(sub_type="vehicle")).json() == {"status": "skipped"}
    assert send(motion(new_start(90_000), sub_type="human")).json() == {"status": "accepted"}
    assert snapshot_posts(ring) == 1


def test_same_event_from_webhook_and_event_history_is_analyzed_once(hooks):
    ring, _ = hooks
    ring.image = jpeg(blurry_frame())
    assert send(motion(new_start())).status_code == 200
    ring.event_pages = {"": [history_event("ev-w", "motion.human", new_start() + 3_000)]}

    body = poll().json()

    assert len(body["reports"]) == 1 and body["reports"][0]["event"]["origin"] == "webhook"
    assert [e["id"] for e in body["events"]] == ["ev-w"]
    assert snapshot_posts(ring) == 1
    assert api.get("/api/metrics").json()["events_seen"] == 1


def test_event_history_first_then_webhook_is_not_analyzed_again(hooks):
    ring, _ = hooks
    ring.event_pages = {"": [history_event("ev-h", "ding", new_start())]}
    poll()
    assert send(motion(new_start() - 8_000, kind="button_press")).json() == {"status": "already_analyzed"}
    assert send(motion(new_start() + 30_000, kind="button_press")).json() == {"status": "accepted"}
    assert snapshot_posts(ring) == 2


def test_signature_check_uses_constant_time_compare(monkeypatch):
    calls = []
    real = server.hmac.compare_digest
    monkeypatch.setattr(server.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    body = b'{"a": 1}'
    assert server._webhook_signature_ok(body, sign(body, KEY), KEY) and calls


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_200_does_not_wait_for_a_slow_analysis(hooks, monkeypatch):
    done = threading.Event()

    async def slow_analysis(_device_id, _event):
        await asyncio.sleep(3)
        done.set()
        return None

    monkeypatch.setattr(server, "_analyze_partner_event", slow_analysis)
    port = free_port()
    uv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not uv.started and time.monotonic() < deadline:
            time.sleep(0.05)
        body = json.dumps(motion()).encode("utf-8")
        started = time.monotonic()
        response = httpx.post(f"http://127.0.0.1:{port}/webhooks/ring", content=body,
                              headers={"X-Signature": sign(body, KEY)}, timeout=10)
        elapsed = time.monotonic() - started

        assert response.status_code == 200 and elapsed < 2 and not done.is_set()
        assert done.wait(10)                    # processed afterwards
    finally:
        uv.should_exit = True
        thread.join(10)


def test_send_test_webhook_signs_the_exact_bytes_it_sends(monkeypatch, capsys):
    from tools import send_test_webhook as tool

    sent = {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url):
            assert url.endswith("/api/ring/events")
            return httpx.Response(200, json={"events": [{"start": 1_761_000_005_000}, {"start": 1_761_000_001_000}]})

        def post(self, url, content, headers):
            sent.update(url=url, content=content, headers=headers)
            return httpx.Response(200, json={"status": "accepted"})

    monkeypatch.setenv("RING_PARTNER_HMAC_KEY", KEY)
    monkeypatch.setattr(tool.httpx, "Client", FakeClient)

    assert tool.main(["--type", "button_press", "--device-id", DEVICE_ID]) == 0

    assert sent["url"] == "http://127.0.0.1:8000/webhooks/ring"
    assert server._webhook_signature_ok(sent["content"], sent["headers"]["X-Signature"], KEY)
    payload = json.loads(sent["content"])
    assert payload["meta"]["version"] == "1.1" and payload["data"]["type"] == "button_press"
    assert payload["data"]["attributes"] == {"source": DEVICE_ID, "source_type": "device",
                                             "timestamp": 1_761_000_005_000}
    out = capsys.readouterr().out
    assert KEY not in out and out.splitlines()[0] == "HTTP 200" and len(out.splitlines()) == 2
