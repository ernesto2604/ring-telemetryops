"""
C1: knowledge retrieval per simulator case (baseline of the scenario-2 report). Frames are the
Digital Twin's base video with the canvas effects emulated in Python, as the browser sends them.
"""
import numpy as np
import pytest

from src.agent.knowledge_retriever import KnowledgeRetriever
from src.utils.telemetry_simulator import TelemetrySimulator
from src.video_pipeline.analyzer import VideoAnalyzer
from tests.test_video_pipeline import as_canvas_jpeg, dirty_lens, sample_frame

retriever = KnowledgeRetriever()
STANDBY = {"status": "STANDBY", "issues": [], "fps": 0.0, "bitrate_kbps": 0.0, "sharpness_score": 0.0}


def nominal(**device_health):
    telemetry = TelemetrySimulator().get_telemetry("nominal")
    telemetry["device_health"].update(device_health)
    return telemetry


def pink_frame():
    # Mirrors the pinkTint pixel shader in web/js/canvas-engine.js.
    f = sample_frame().astype(np.float32)
    f[:, :, 2] = np.minimum(255, f[:, :, 2] * 1.45 + 25)
    f[:, :, 1] = np.minimum(255, f[:, :, 1] * 0.55)
    f[:, :, 0] = np.minimum(255, f[:, :, 0] * 1.30 + 40)
    return f.astype(np.uint8)


def flicker_frame():
    # Periodic dark horizontal bands, like the canvas flicker overlay.
    rows = np.where((np.arange(1080) // 18) % 2 == 0, 0.55, 1.0)
    return (sample_frame().astype(np.float32) * rows[:, None, None]).clip(0, 255).astype(np.uint8)


def frame_ids(frame):
    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(frame), nominal())
    return [a["id"] for a in retriever.retrieve_guidance(metrics, nominal())]


def ids(video, telemetry):
    return [a["id"] for a in retriever.retrieve_guidance(video, telemetry)]


def test_pink_tint_retrieves_the_ir_filter_and_video_quality_articles():
    """N41: the pink tint used to retrieve 0 articles."""
    retrieved = frame_ids(pink_frame())
    assert retrieved, "pink tint retrieved no article"
    assert retrieved[0] == "KB-RING-007"
    assert "KB-RING-005" in retrieved


def test_dirty_lens_ranks_the_blurry_video_article_first():
    assert frame_ids(dirty_lens(sample_frame()))[0] == "KB-RING-005"


def test_flicker_ranks_the_banding_article_first():
    assert frame_ids(flicker_frame())[0] == "KB-RING-014"


def test_weak_wifi_in_standby_retrieves_the_wifi_article():
    assert ids(STANDBY, nominal(rssi=-84))[0] == "KB-RING-001"


def test_wifi_degradation_scenario_retrieves_the_wifi_article_first():
    assert ids(STANDBY, TelemetrySimulator().get_telemetry("wifi_degradation"))[0] == "KB-RING-001"


@pytest.mark.parametrize("device_health", [
    {"power_mode": "battery", "battery_percentage": 8, "battery_saver_low_voltage": True},
    {"power_mode": "wired", "transformer_voltage_vac": 11.0},
    {"power_mode": "wired", "transformer_voltage_vac": 0.0},
])
def test_power_cases_retrieve_the_power_article_first(device_health):
    assert ids(STANDBY, nominal(**device_health))[0] == "KB-RING-009"


def test_critical_battery_without_solar_does_not_retrieve_the_solar_article():
    """S1-B3b: KB-RING-011 only applies to devices with a solar panel."""
    retrieved = ids(STANDBY, nominal(power_mode="battery", battery_percentage=8, battery_saver_low_voltage=True))
    assert "KB-RING-011" not in retrieved


def test_critical_battery_with_a_solar_panel_still_retrieves_the_solar_article():
    retrieved = ids(STANDBY, nominal(power_mode="battery", power_source="Solar", battery_percentage=8,
                                     battery_saver_low_voltage=True))
    assert "KB-RING-011" in retrieved
    assert "KB-RING-009" in retrieved


def test_mechanical_chime_retrieves_the_chime_article():
    assert ids(STANDBY, TelemetrySimulator().get_telemetry("in_home_chime"))[0] == "KB-RING-013"


# ---------------------------------------------------------------------------
# C4 / N5: dropped frames are a simulated field; the Twin sliders do not simulate them
# ---------------------------------------------------------------------------

def test_slider_telemetry_does_not_invent_dropped_frames(monkeypatch):
    from fastapi.testclient import TestClient
    from src.agent.concierge import SupportConcierge
    from src.api.server import app

    tel = TestClient(app).post("/api/telemetry", json={"mode": "custom", "rssi": -84}).json()
    rtp = tel["webrtc_stats"]["inbound_rtp"]
    assert rtp["frames_dropped"] is None and rtp["frames_received"] is None
    assert tel["measurement_status"]["webrtc_stats"]["frames_dropped"] == "not simulated by the Digital Twin sliders"

    prompts = []
    agent = SupportConcierge()
    monkeypatch.setattr(agent, "_call_llm", lambda prompt, *_a, **_k: prompts.append(prompt) or "not json")
    agent.process_stream(telemetry=tel, camera_active=False)
    assert ("- Dropped frames : unavailable (not simulated by the Digital Twin sliders) / "
            "unavailable (not simulated by the Digital Twin sliders) received") in prompts[0]


def test_wifi_degradation_scenario_reports_its_simulated_dropped_frames():
    """The '210/450 frames dropped' Bedrock cites comes from this simulated scenario (README, N5)."""
    rtp = TelemetrySimulator().get_telemetry("wifi_degradation")["webrtc_stats"]["inbound_rtp"]
    assert (rtp["frames_dropped"], rtp["frames_received"]) == (210, 450)
