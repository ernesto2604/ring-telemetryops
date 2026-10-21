"""
A3 / D5: the CRITICAL / DEGRADED label of a report comes from the severity table (worst metric
level), not from the model. The model's findings and mitigation are kept. Bedrock is replaced by
a test double; no network.

Rule 4: the Ring Partner API data path keeps its behaviour (only the battery and online values
Ring reports reach the classifier, and the low-battery band applies to battery-only installs).
"""
import json

import pytest

from src.agent.concierge import SupportConcierge
from src.utils.partner_telemetry import partner_device_to_telemetry


def wired(volts):
    return {"_source": "mock",
            "device_health": {"power_mode": "wired", "rssi": -50, "battery_percentage": 100,
                              "transformer_voltage_vac": volts},
            "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0.0,
                                             "frames_per_second": 30, "bitrate_kbps": 1200}}}


def model_reply(status):
    return json.dumps({
        "diagnostic_status": status, "root_cause_domain": "HARDWARE_POWER", "confidence_score": 0.95,
        "telemetry_findings": ["Transformer voltage is below the project threshold."],
        "visual_findings": ["Camera in low-power standby mode. Optical sensor powered down by design."],
        "recommended_mitigation": "Your doorbell is not getting enough power. Please contact a qualified "
                                  "electrician or Ring Support to check its power supply.",
    })


@pytest.mark.parametrize("volts, model_status, expected", [
    (11.0, "CRITICAL", "DEGRADED"),   # observed: Bedrock labelled 11 V CRITICAL after 3 pulses
    (0.0, "DEGRADED", "CRITICAL"),
    (11.0, "DEGRADED", "DEGRADED"),   # agreement: nothing to replace
])
def test_model_severity_is_replaced_by_the_deterministic_level(monkeypatch, caplog, volts, model_status, expected):
    agent = SupportConcierge()
    monkeypatch.setattr(agent, "_call_llm", lambda *_a, **_k: model_reply(model_status))

    with caplog.at_level("WARNING"):
        result = agent.process_stream(telemetry=wired(volts), camera_active=False)

    report = result["diagnostic_result"]
    assert result["_provider"] == "bedrock"
    assert report["diagnostic_status"] == expected
    assert result["severity_level"] == expected
    # The model's reasoning is kept.
    assert report["telemetry_findings"] == ["Transformer voltage is below the project threshold."]
    assert report["confidence_score"] == 0.95
    if model_status != expected:
        assert result["severity_override"] == {"model": model_status, "deterministic": expected}
        assert any("Replacing model severity" in r.getMessage() for r in caplog.records)
    else:
        assert result["severity_override"] is None


# ---------------------------------------------------------------------------
# Rule 4: Ring Partner API telemetry is diagnosed as before
# ---------------------------------------------------------------------------

def partner(status):
    return partner_device_to_telemetry({"device_id": "dev-1", "name": "Front Door",
                                        "device_health": status, "capabilities": {"battery_status": {"supported": True}}})


@pytest.fixture
def agent(monkeypatch):
    instance = SupportConcierge()
    instance.llm_calls = []
    monkeypatch.setattr(instance, "_call_llm", lambda *a, **_k: instance.llm_calls.append(a) or "not json")
    return instance


@pytest.mark.parametrize("battery", [25, 29, 50, 100])
def test_partner_battery_without_power_mode_is_not_a_low_battery_anomaly(agent, battery):
    """The low band (< 30 %) needs a battery-only install; Ring reports no power setup, so as before
    the only verdict without network data is INSUFFICIENT_DATA, and Bedrock is not called."""
    result = agent.process_stream(telemetry=partner({"online": True, "battery_percentage": battery}),
                                  camera_active=False, no_frame_reason="no frame")
    assert result["diagnostic_result"]["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert agent.llm_calls == []


def test_partner_critical_battery_is_still_critical(agent):
    result = agent.process_stream(telemetry=partner({"online": True, "battery_percentage": 5}),
                                  camera_active=False, no_frame_reason="no frame")
    assert result["_provider"] == "local_fallback"   # the double returned "not json" twice at most
    assert result["diagnostic_result"]["diagnostic_status"] == "CRITICAL"
    assert result["diagnostic_result"]["root_cause_domain"] == "HARDWARE_POWER"


def test_partner_offline_read_is_still_a_precheck_verdict(agent):
    result = agent.process_stream(telemetry=partner({"online": False}), camera_active=False,
                                  no_frame_reason="no frame")
    assert result["_provider"] == "precheck"
    assert result["diagnostic_result"]["diagnostic_status"] == "CRITICAL"
    assert result["diagnostic_result"]["root_cause_domain"] == "UNKNOWN"
    assert agent.llm_calls == []
