"""
B5: a wired device that reports a mechanical in-home chime (chime_type_setting="mechanical")
below 16 V AC is DEGRADED / HARDWARE_POWER. The 16 V value is a product diagnostic rule for this
case, not a Ring specification; generic wired devices keep the 12 V heuristic only.
No network or Bedrock calls.
"""
import pytest

from src.agent import concierge
from src.agent.concierge import SupportConcierge
from src.utils.telemetry_simulator import TelemetrySimulator

VIDEO = {"status": "OPTIMAL", "issues": [], "fps": None, "bitrate_kbps": None,
         "sharpness_score": 200.0, "optical_banding_detected": False}


def wired(volts, **device_health):
    return {"device_health": {"rssi": -50, "battery_percentage": 100,
                              "transformer_voltage_vac": volts, **device_health},
            "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0.0,
                                             "frames_per_second": 30, "bitrate_kbps": 1200}}}


@pytest.fixture
def agent(monkeypatch):
    instance = SupportConcierge()

    def bedrock_unavailable(*_args, **_kwargs):
        raise RuntimeError("Bedrock disabled in tests")
    monkeypatch.setattr(instance, "_call_llm", bedrock_unavailable)
    return instance


def test_chime_threshold_is_specific_and_separate_from_the_general_one():
    assert concierge.MECHANICAL_CHIME_MIN_VAC == 16.0
    assert concierge.TRANSFORMER_MIN_VAC == 12.0


def test_in_home_chime_scenario_precheck(agent):
    tel = TelemetrySimulator().get_telemetry("in_home_chime")
    anomaly = agent._precheck_anomaly(VIDEO, tel, True)
    assert anomaly["domain"] == "HARDWARE_POWER"
    assert "mechanical chime" in anomaly["reason"].lower()


def test_in_home_chime_scenario_fallback(agent):
    tel = TelemetrySimulator().get_telemetry("in_home_chime")
    report = agent._build_local_diagnostic(VIDEO, tel, [], True)
    assert (report["diagnostic_status"], report["root_cause_domain"]) == ("DEGRADED", "HARDWARE_POWER")
    finding = report["telemetry_findings"][0]
    assert "13.5" in finding and "mechanical" in finding.lower()
    assert "ring spec" not in finding.lower()


def test_in_home_chime_scenario_end_to_end_without_bedrock(agent):
    tel = TelemetrySimulator().get_telemetry("in_home_chime")
    result = agent.process_stream(telemetry=tel, camera_active=False)
    assert result["_provider"] == "local_fallback"
    assert result.get("analysis_skipped") is not True
    report = result["diagnostic_result"]
    assert (report["diagnostic_status"], report["root_cause_domain"]) == ("DEGRADED", "HARDWARE_POWER")


@pytest.mark.parametrize("device_health", [{}, {"chime_type_setting": "digital"}, {"chime_type_setting": None}])
def test_wired_13_5v_without_a_mechanical_chime_is_not_flagged(agent, device_health):
    tel = wired(13.5, **device_health)
    assert agent._precheck_anomaly(VIDEO, tel, True) is None
    assert agent._build_local_diagnostic(VIDEO, tel, [], True)["diagnostic_status"] == "HEALTHY"


@pytest.mark.parametrize("volts, anomalous", [(15.9, True), (16.0, False), (18.0, False)])
def test_mechanical_chime_boundary(agent, volts, anomalous):
    tel = wired(volts, chime_type_setting="mechanical")
    assert (agent._precheck_anomaly(VIDEO, tel, True) is not None) is anomalous
    status = agent._build_local_diagnostic(VIDEO, tel, [], True)["diagnostic_status"]
    assert status == ("DEGRADED" if anomalous else "HEALTHY")


def test_general_undervoltage_still_wins_below_12v(agent):
    tel = wired(10.0, chime_type_setting="mechanical")
    assert agent._precheck_anomaly(VIDEO, tel, True)["reason"] == "Transformer undervoltage"
    report = agent._build_local_diagnostic(VIDEO, tel, [], True)
    assert report["root_cause_domain"] == "HARDWARE_POWER"
    assert report["telemetry_findings"][0].startswith("Transformer voltage (wired) is 10 V AC (Undervoltage: < 12 V AC")


def test_battery_only_device_ignores_the_chime_rule(agent):
    tel = wired(13.5, chime_type_setting="mechanical", power_mode="battery")
    assert agent._precheck_anomaly(VIDEO, tel, True) is None


def test_documented_api_demo_procedure_for_in_home_chime(monkeypatch):
    """README demo path: preset telemetry from /api/telemetry, then /api/analyze (no Bedrock)."""
    from fastapi.testclient import TestClient
    from src.api.server import app

    def bedrock_unavailable(*_args, **_kwargs):
        raise RuntimeError("Bedrock disabled in tests")
    monkeypatch.setattr(SupportConcierge, "_call_llm", bedrock_unavailable)

    client = TestClient(app)
    tel = client.post("/api/telemetry", json={"mode": "preset", "scenario": "in_home_chime"}).json()
    response = client.post("/api/analyze", json={
        "video_path": "good_internet.mp4", "telemetry": tel,
        "camera_active": False, "source_mode": "twin",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["_provider"] == "local_fallback"
    assert body["telemetry"]["_source"] == "client_supplied"
    report = body["diagnostic_result"]
    assert (report["diagnostic_status"], report["root_cause_domain"]) == ("DEGRADED", "HARDWARE_POWER")
