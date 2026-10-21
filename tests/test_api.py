from fastapi.testclient import TestClient

from src.api.server import app
from src.utils.telemetry_simulator import TelemetrySimulator


client = TestClient(app)


def test_read_only_api_endpoints_return_expected_shapes():
    scenarios = client.get("/api/scenarios")
    kb = client.get("/api/kb")
    samples = client.get("/api/samples")

    assert scenarios.status_code == 200
    assert scenarios.json()["scenarios"] == list(TelemetrySimulator.SUPPORTED_SCENARIOS)
    assert kb.status_code == 200
    assert kb.json()["articles"]
    assert samples.status_code == 200
    assert {sample["name"] for sample in samples.json()["samples"]} >= {
        "good_internet.mp4",
        "bad_internet.mp4",
    }


def test_telemetry_endpoint_supports_preset_and_custom_modes():
    preset = client.post(
        "/api/telemetry",
        json={"mode": "preset", "scenario": "wifi_degradation"},
    )
    custom = client.post(
        "/api/telemetry",
        json={"mode": "custom", "rssi": -88, "jitter_ms": 50},
    )

    assert preset.status_code == 200
    assert preset.json()["scenario"] == "wifi_degradation"
    assert custom.status_code == 200
    assert custom.json()["device_health"]["rssi"] == -88
    assert custom.json()["webrtc_stats"]["inbound_rtp"]["jitter"] == 0.05


def test_analyze_endpoint_returns_diagnostic_without_calling_llm(monkeypatch):
    calls = {}

    class FakeConcierge:
        def process_stream(self, **kwargs):
            calls["process_stream"] = kwargs
            return {
                "diagnostic_result": {
                    "diagnostic_status": "DEGRADED",
                    "root_cause_domain": "NETWORK",
                },
                "_provider": "bedrock",
            }

    monkeypatch.setattr("src.api.server.SupportConcierge", FakeConcierge)

    response = client.post(
        "/api/analyze",
        json={
            "video_path": "good_internet.mp4",
            "telemetry": {"scenario": "nominal"},
            "keyframe_second": 2.5,
            "camera_active": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["diagnostic_result"]["root_cause_domain"] == "NETWORK"
    assert calls["process_stream"]["video_path"].endswith("good_internet.mp4")
    assert calls["process_stream"]["keyframe_second"] == 2.5
    assert calls["process_stream"]["camera_active"] is False


def test_analyze_endpoint_uses_local_fallback_when_bedrock_is_unavailable(monkeypatch):
    class FailingConcierge:
        def __init__(self):
            from src.agent.concierge import SupportConcierge

            self.concierge = SupportConcierge()
            self.concierge._call_llm = lambda *_args: (_ for _ in ()).throw(
                RuntimeError("Bedrock credentials unavailable")
            )

        def process_stream(self, **kwargs):
            return self.concierge.process_stream(**kwargs)

    monkeypatch.setattr("src.api.server.SupportConcierge", FailingConcierge)

    response = client.post(
        "/api/analyze",
        json={
            "video_path": "good_internet.mp4",
            "telemetry": {
                "scenario": "wifi_degradation",
                "device_health": {"rssi": -84, "battery_percentage": 78},
                "webrtc_stats": {
                    "inbound_rtp": {
                        "jitter": 0.085,
                        "packet_loss_fraction": 0.18,
                        "frames_per_second": 15,
                        "bitrate_kbps": 180,
                    }
                },
            },
            "camera_active": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["_provider"] == "local_fallback"
    assert response.json()["_model"] is None
    assert response.json()["diagnostic_result"]["root_cause_domain"] == "NETWORK"
    assert response.json()["diagnostic_result"]["visual_findings"] == [
        "Camera in low-power standby mode. Optical sensor powered down by design."
    ]


def test_nominal_analysis_skips_bedrock_after_precheck(monkeypatch):
    from src.agent.concierge import SupportConcierge

    concierge = SupportConcierge()

    def fail_if_called(*_args):
        raise AssertionError("Bedrock must not be called for nominal telemetry.")

    monkeypatch.setattr(concierge, "_call_llm", fail_if_called)
    result = concierge.process_stream(
        video_path="data/samples/good_internet.mp4",
        telemetry={
            "device_health": {
                "rssi": -48,
                "battery_percentage": 100,
                "transformer_voltage_vac": 18,
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "jitter": 0.002,
                    "packet_loss_fraction": 0,
                    "frames_per_second": 30,
                    "bitrate_kbps": 1200,
                }
            },
        },
        camera_active=False,
    )

    assert result["analysis_skipped"] is True
    assert result["diagnostic_result"] is None
    assert result["_provider"] == "precheck"


def test_precheck_does_not_treat_optimal_low_sharpness_as_anomaly():
    from src.agent.concierge import SupportConcierge

    concierge = SupportConcierge()
    anomaly = concierge._precheck_anomaly(
        {
            "status": "OPTIMAL",
            "issues": [],
            "sharpness_score": 15.88,
            "optical_banding_detected": False,
            "fps": 30,
            "bitrate_kbps": 1200,
        },
        {
            "device_health": {
                "rssi": -48,
                "battery_percentage": 100,
                "transformer_voltage_vac": 18,
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "jitter": 0.002,
                    "packet_loss_fraction": 0,
                    "frames_per_second": 30,
                    "bitrate_kbps": 1200,
                }
            },
        },
        camera_active=True,
    )

    assert anomaly is None


def test_ring_analysis_without_partner_source_is_explicit_error(monkeypatch):
    """The unofficial live adapter was removed: "ring" in simulator mode never falls back to the Twin."""
    class FailConcierge:
        def process_stream(self, **_kwargs):
            raise AssertionError("must not analyze Twin data as Ring data")

    monkeypatch.setattr("src.api.server.SupportConcierge", FailConcierge)

    response = client.post(
        "/api/analyze",
        json={"source_mode": "ring", "camera_active": True, "video_path": "good_internet.mp4",
              "telemetry": {"scenario": "nominal"}},
    )

    assert response.status_code == 503
    assert "RING_DATA_SOURCE=simulator" in response.json()["detail"]
    assert client.get("/api/ring/telemetry").status_code == 503


def test_simulated_whep_answer_is_labelled_simulated():
    response = client.post("/api/ring/whep-session")

    body = response.json()
    assert response.status_code == 200
    assert body["simulated"] is True and body["status"] == "simulated"
    assert "amazonvision" not in response.text   # no real Ring endpoint is implied


# ---------------------------------------------------------------------------
# Simulator Dirty Lens: analyzer issues drive the concierge (no Bedrock calls)
# ---------------------------------------------------------------------------

NOMINAL_TELEMETRY = {
    "device_health": {"rssi": -48, "battery_percentage": 100, "transformer_voltage_vac": 18},
    "webrtc_stats": {
        "inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0, "frames_per_second": 30, "bitrate_kbps": 1200}
    },
}

DIRTY_LENS_LLM_REPLY = (
    '{"diagnostic_status": "DEGRADED", "root_cause_domain": "OPTICAL_PHYSICAL", "confidence_score": 0.9,'
    ' "telemetry_findings": [], "visual_findings": ["Blurred image."], "recommended_mitigation": "Clean the lens."}'
)


def recording_concierge(monkeypatch):
    """Real SupportConcierge whose Bedrock call is replaced by a recorder."""
    from src.agent.concierge import SupportConcierge

    concierge = SupportConcierge()
    concierge.llm_calls = []

    def fake_llm(prompt, image_base64):
        concierge.llm_calls.append(prompt)
        return DIRTY_LENS_LLM_REPLY

    monkeypatch.setattr(concierge, "_call_llm", fake_llm)
    return concierge


def test_precheck_keeps_optical_anomaly_found_by_analyzer():
    from src.agent.concierge import SupportConcierge
    from tests.test_video_pipeline import as_canvas_jpeg, dirty_lens, sample_frame
    from src.video_pipeline.analyzer import VideoAnalyzer

    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(dirty_lens(sample_frame())), NOMINAL_TELEMETRY)

    anomaly = SupportConcierge()._precheck_anomaly(metrics, NOMINAL_TELEMETRY, camera_active=True)

    assert (anomaly["domain"], anomaly["reason"], anomaly["level"]) == (
        "OPTICAL_PHYSICAL", "Visual quality issue detected", "DEGRADED")


def test_local_diagnostic_uses_analyzer_issues_not_its_own_threshold():
    from src.agent.concierge import SupportConcierge

    concierge = SupportConcierge()
    base = {"fps": 30, "bitrate_kbps": 1200, "optical_banding_detected": False}

    # Low raw score but the analyzer reported no issue: not reclassified.
    optimal = concierge._build_local_diagnostic(
        base | {"status": "OPTIMAL", "issues": [], "sharpness_score": 5.0}, NOMINAL_TELEMETRY, [], True)
    # High raw score but the analyzer reported blur: the analyzer wins.
    degraded = concierge._build_local_diagnostic(
        base | {"status": "DEGRADED", "issues": ["Deficient sharpness / optical blur"], "sharpness_score": 500.0},
        NOMINAL_TELEMETRY, [], True)

    assert optimal["root_cause_domain"] == "NONE"
    assert degraded["root_cause_domain"] == "OPTICAL_PHYSICAL"
    assert degraded["diagnostic_status"] == "DEGRADED"


def test_dirty_lens_frame_reaches_diagnosis_through_api(monkeypatch):
    from tests.test_video_pipeline import as_canvas_jpeg, dirty_lens, sample_frame

    concierge = recording_concierge(monkeypatch)
    monkeypatch.setattr("src.api.server.SupportConcierge", lambda: concierge)

    response = client.post("/api/analyze", json={
        "source_mode": "twin",
        "camera_active": True,
        "frame_base64": as_canvas_jpeg(dirty_lens(sample_frame())),
        "telemetry": NOMINAL_TELEMETRY,
    })

    body = response.json()
    assert response.status_code == 200
    assert not body.get("analysis_skipped")
    assert body["video_metrics"]["status"] == "DEGRADED"
    assert any("Deficient sharpness" in i for i in body["video_metrics"]["issues"])
    assert body["diagnostic_result"]["root_cause_domain"] == "OPTICAL_PHYSICAL"
    assert len(concierge.llm_calls) == 1  # mocked; no real Bedrock request


def test_clean_frame_skips_bedrock_through_api(monkeypatch):
    from tests.test_video_pipeline import as_canvas_jpeg, sample_frame

    concierge = recording_concierge(monkeypatch)
    monkeypatch.setattr("src.api.server.SupportConcierge", lambda: concierge)

    response = client.post("/api/analyze", json={
        "source_mode": "twin",
        "camera_active": True,
        "frame_base64": as_canvas_jpeg(sample_frame()),
        "telemetry": NOMINAL_TELEMETRY,
    })

    assert response.status_code == 200
    assert response.json()["analysis_skipped"] is True
    assert response.json()["video_metrics"]["issues"] == []
    assert concierge.llm_calls == []


def test_frame_with_telemetry_lacking_fps_and_bitrate_is_not_a_500(monkeypatch):
    """RING-003: a blurred frame with no WebRTC stats reaches the RAG with fps/bitrate None."""
    import base64

    import cv2
    import numpy as np

    sharp = np.random.default_rng(0).integers(0, 255, (720, 1280, 3), dtype=np.uint8)
    blurred = cv2.GaussianBlur(sharp, (0, 0), 12)
    frame = base64.b64encode(cv2.imencode(".jpg", blurred)[1]).decode("ascii")
    monkeypatch.setattr(
        "src.agent.concierge.SupportConcierge._call_llm",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("Bedrock disabled in tests")),
    )

    response = client.post(
        "/api/analyze",
        json={
            "source_mode": "twin",
            "frame_base64": frame,
            "camera_active": True,
            "telemetry": {"device_health": {"rssi": -50, "battery_percentage": 100}},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["video_metrics"]["fps"] is None
    assert body["video_metrics"]["bitrate_kbps"] is None
    assert body["_provider"] == "local_fallback"
    assert body["diagnostic_result"]["root_cause_domain"] == "OPTICAL_PHYSICAL"
    assert body["retrieved_articles"]


# ---------------------------------------------------------------------------
# RING-012: video paths are confined to samples/ and uploads/; errors are generic
# ---------------------------------------------------------------------------

import pytest


@pytest.mark.parametrize("video_path", ["good_internet.mp4", "data/samples/good_internet.mp4"])
def test_video_path_resolves_inside_samples(video_path):
    from src.api import server

    resolved = server._resolve_video_path(video_path)

    assert resolved.parent == server.SAMPLES_DIR.resolve()
    assert resolved.name == "good_internet.mp4"


@pytest.mark.parametrize("video_path", [
    __file__,                                   # absolute path to an existing file outside data/
    "../../requirements.txt",                   # traversal to an existing project file
    r"..\..\requirements.txt",
    "src/api/server.py",                        # project file relative to the root
    "..", "", "missing_clip.mp4",
])
def test_video_path_outside_allowed_dirs_is_not_found(video_path):
    from src.api import server

    with pytest.raises(FileNotFoundError) as exc:
        server._resolve_video_path(video_path)
    assert video_path not in str(exc.value) or not video_path


def test_analyze_with_path_outside_data_dirs_is_404_without_path_echo():
    response = client.post(
        "/api/analyze",
        json={"source_mode": "twin", "video_path": __file__, "camera_active": True,
              "telemetry": {"scenario": "nominal"}},
    )

    assert response.status_code == 404
    assert __file__ not in response.text


def test_analyze_internal_errors_are_generic(monkeypatch):
    class BrokenConcierge:
        def process_stream(self, **_kwargs):
            raise RuntimeError(r"secret detail C:\private\path token=abc")

    monkeypatch.setattr("src.api.server.SupportConcierge", BrokenConcierge)

    response = client.post(
        "/api/analyze",
        json={"source_mode": "twin", "video_path": "good_internet.mp4", "camera_active": False},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Internal error while running the diagnosis."
    assert "private" not in response.text and "token" not in response.text


def test_undecodable_frame_is_422_not_500(monkeypatch):
    monkeypatch.setattr(
        "src.agent.concierge.SupportConcierge._call_llm",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Bedrock must not be called")),
    )

    response = client.post(
        "/api/analyze",
        json={"source_mode": "twin", "frame_base64": "bm90LWFuLWltYWdl", "camera_active": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "The video frame or file could not be decoded."


def test_oversized_frame_is_rejected_before_analysis(monkeypatch):
    from src.api import server

    class FailConcierge:
        def process_stream(self, **_kwargs):
            raise AssertionError("must not analyze an oversized frame")

    monkeypatch.setattr("src.api.server.SupportConcierge", FailConcierge)

    response = client.post(
        "/api/analyze",
        json={"source_mode": "twin", "frame_base64": "A" * (server.MAX_FRAME_BASE64_CHARS + 1),
              "camera_active": True},
    )

    assert response.status_code == 422


def test_samples_are_listed_once_even_on_case_insensitive_filesystems():
    names = [sample["name"] for sample in client.get("/api/samples").json()["samples"]]
    assert len(names) == len(set(names))
