"""T38: in-memory funnel metrics. Bedrock is always a double; Ring is the local fake server."""
import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from src.api.funnel import Funnel, funnel
from tests.fake_ring_server import greco_no_valid_key
from tests.test_api import NOMINAL_TELEMETRY, recording_concierge
from tests.test_partner_snapshots_events import analyze as partner_analyze, partner, ring, sleeps  # noqa: F401
from tests.test_video_pipeline import as_canvas_jpeg, dirty_lens, sample_frame

api = TestClient(server.app)


@pytest.fixture(autouse=True)
def fresh_funnel():
    funnel.reset()
    yield
    funnel.reset()


def twin(frame):
    return api.post("/api/analyze", json={"source_mode": "twin", "camera_active": True,
                                          "frame_base64": as_canvas_jpeg(frame), "telemetry": NOMINAL_TELEMETRY})


def failing_concierge(monkeypatch):
    concierge = recording_concierge(monkeypatch)

    def unavailable(*_args):
        raise RuntimeError("Bedrock unavailable (test double)")

    monkeypatch.setattr(concierge, "_call_llm", unavailable)
    return concierge


def test_counters_follow_a_known_sequence(monkeypatch):
    concierge = recording_concierge(monkeypatch)
    monkeypatch.setattr(server, "SupportConcierge", lambda: concierge)

    assert twin(sample_frame()).json()["analysis_skipped"] is True          # nominal: frame kept
    assert twin(dirty_lens(sample_frame())).json()["_provider"] == "bedrock"  # anomaly, one Bedrock call

    fallback = failing_concierge(monkeypatch)
    monkeypatch.setattr(server, "SupportConcierge", lambda: fallback)
    assert twin(dirty_lens(sample_frame())).json()["_provider"] == "local_fallback"

    m = api.get("/api/metrics").json()
    assert m["analyses_total"] == 3
    assert m["precheck_nominal"] == 1 and m["precheck_anomaly"] == 2
    assert m["bedrock_calls"] == 2 and m["bedrock_retries"] == 0   # the failed request counts as a call
    assert m["local_fallbacks"] == 1 and m["insufficient_data"] == 0
    assert m["latency"]["precheck"]["samples"] == 1 and m["latency"]["bedrock"]["samples"] == 1
    assert m["latency"]["local_fallback"]["samples"] == 1
    assert m["latency"]["bedrock"]["median_ms"] >= 0 and m["persisted"] is False
    assert len(concierge.llm_calls) == 1


def test_contract_retry_counts_two_calls_and_one_retry(monkeypatch):
    from tests.test_api import DIRTY_LENS_LLM_REPLY

    concierge = recording_concierge(monkeypatch)
    replies = iter(["not json", DIRTY_LENS_LLM_REPLY])
    monkeypatch.setattr(concierge, "_call_llm", lambda *_args: next(replies))
    monkeypatch.setattr(server, "SupportConcierge", lambda: concierge)

    assert twin(dirty_lens(sample_frame())).json()["bedrock_attempts"] == 2
    m = api.get("/api/metrics").json()
    assert m["bedrock_calls"] == 2 and m["bedrock_retries"] == 1


def test_partner_snapshot_success_and_failure_are_counted_by_cause(partner):  # noqa: F811
    ring, _ = partner
    assert partner_analyze().status_code == 200
    ring.presigned_replies = [greco_no_valid_key()]
    assert partner_analyze().status_code == 200

    m = api.get("/api/metrics").json()
    assert m["snapshots_ok"] == 1
    assert m["snapshots_failed"] == {"no_recent_media": 0, "media_undecryptable": 1, "corrupt_recording": 0,
                                     "unreadable_image": 0, "other": 0}
    # Partner never shares Wi-Fi or power: both analyses end as INSUFFICIENT_DATA from the pre-check.
    assert m["analyses_total"] == 2 and m["insufficient_data"] == 2


def test_metrics_carry_no_device_ids_images_or_text(partner):  # noqa: F811
    partner_analyze()
    body = api.get("/api/metrics").text
    assert "ring.device" not in body.lower() and "base64" not in body and "jpeg" not in body

    def leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from leaves(item)
        else:
            yield value

    assert all(v is None or isinstance(v, (int, float, bool)) or v == "server_start"
               for v in leaves(api.get("/api/metrics").json()))


def test_latency_keeps_the_last_100_samples_with_median_and_p95():
    f = Funnel()
    for ms in range(1, 151):
        f.record_analysis({"_provider": "precheck", "analysis_skipped": True}, ms / 1000)
    stats = f.snapshot()["latency"]["precheck"]
    assert stats["samples"] == 100
    assert stats["median_ms"] == 100.5 and stats["p95_ms"] == 145.0
    assert f.snapshot()["latency"]["bedrock"] == {"samples": 0, "median_ms": None, "p95_ms": None}


def test_unknown_snapshot_status_counts_as_other():
    f = Funnel()
    f.record_snapshot("not_ready")
    f.record_snapshot("error")
    assert f.snapshot()["snapshots_failed"]["other"] == 2
