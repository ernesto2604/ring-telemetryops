import json

import pytest

from src.agent.concierge import SupportConcierge
from src.utils.partner_telemetry import partner_device_to_telemetry
from src.utils.ring_partner_client import normalize_device
from src.utils.telemetry_simulator import TelemetrySimulator

STANDBY_METRICS = {
    "file": "standby_low_power_mode",
    "resolution": "Camera in standby mode",
    "fps": 0.0,
    "bitrate_kbps": 0.0,
    "sharpness_score": 0.0,
    "status": "STANDBY",
    "issues": [],
}

LLM_DEGRADED_NETWORK = json.dumps({
    "diagnostic_status": "DEGRADED",
    "root_cause_domain": "NETWORK",
    "confidence_score": 0.9,
    "telemetry_findings": ["Weak RSSI."],
    "visual_findings": ["Camera in low-power standby mode. Optical sensor powered down by design."],
    "recommended_mitigation": "Move the router closer.",
})


def partner_telemetry(status_attrs, capability_attrs=None):
    resource = {"type": "devices", "id": "ava1.ring.device.X", "attributes": {"name": "Front Door"}}
    status = {"attributes": status_attrs} if status_attrs is not None else None
    caps = {"attributes": capability_attrs} if capability_attrs is not None else None
    return partner_device_to_telemetry(normalize_device(resource, status, caps))


@pytest.fixture
def concierge(monkeypatch):
    agent = SupportConcierge()
    calls = []

    def fake_llm(prompt, image_base64):
        calls.append(prompt)
        return agent._fake_llm_response

    agent._fake_llm_response = LLM_DEGRADED_NETWORK
    agent.llm_prompts = calls
    monkeypatch.setattr(agent, "_call_llm", fake_llm)
    return agent


def run(agent, telemetry):
    return agent.process_stream(telemetry=telemetry, camera_active=False)


# ---------------------------------------------------------------------------
# Offline / unknown online state
# ---------------------------------------------------------------------------

def test_offline_device_gets_offline_diagnosis_without_bedrock(concierge):
    result = run(concierge, partner_telemetry({"online": False}, {}))

    diag = result["diagnostic_result"]
    assert diag["diagnostic_status"] == "CRITICAL"
    assert diag["root_cause_domain"] == "UNKNOWN"
    assert "offline" in " ".join(diag["telemetry_findings"]).lower()
    assert result["analysis_skipped"] is False
    assert result["_provider"] == "precheck"
    assert concierge.llm_prompts == []


def test_offline_overrides_otherwise_healthy_metrics(concierge):
    telemetry = TelemetrySimulator().get_telemetry("nominal")
    telemetry["device_health"]["online"] = False

    assert concierge._precheck_anomaly(STANDBY_METRICS, telemetry, False)["reason"] == "Device reported offline"
    assert run(concierge, telemetry)["diagnostic_result"]["diagnostic_status"] == "CRITICAL"
    local = concierge._build_local_diagnostic(STANDBY_METRICS, telemetry, [], False)
    assert local["diagnostic_status"] == "CRITICAL"
    assert local["root_cause_domain"] == "UNKNOWN"


def test_unknown_online_state_is_neither_online_nor_offline(concierge):
    result = run(concierge, partner_telemetry({}, {}))

    diag = result["diagnostic_result"]
    assert diag["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert "offline" not in json.dumps(diag).lower()
    assert concierge.llm_prompts == []


def test_unknown_online_state_is_rendered_as_unavailable_in_prompt(concierge):
    telemetry = partner_telemetry({}, {})
    telemetry["device_health"]["battery_percentage"] = 5  # force an anomaly to reach the prompt

    run(concierge, telemetry)

    prompt = concierge.llm_prompts[0]
    assert "- Online         : unavailable (not_reported)" in prompt
    assert "- Source         : ring_partner_api" in prompt


# ---------------------------------------------------------------------------
# Unmeasured transport metrics
# ---------------------------------------------------------------------------

def test_unmeasured_jitter_loss_rtt_do_not_trigger_or_clear_anomalies(concierge):
    telemetry = {
        "device_health": {"rssi": -48, "battery_percentage": 80},
        "webrtc_stats": {"inbound_rtp": {"jitter": None, "packet_loss_fraction": None},
                         "candidate_pair": {"current_round_trip_time": None}},
    }

    assert concierge._precheck_anomaly(STANDBY_METRICS, telemetry, False) is None
    assert run(concierge, telemetry)["analysis_skipped"] is True  # RSSI + battery are real measurements


def test_unmeasured_transport_metrics_are_not_shown_as_zero(concierge):
    telemetry = partner_telemetry({"online": True}, {})
    telemetry["device_health"]["battery_percentage"] = 5

    run(concierge, telemetry)

    prompt = concierge.llm_prompts[0]
    assert "- Jitter         : unavailable (not_collected)" in prompt
    assert "- Packet loss    : unavailable (not_collected)" in prompt
    assert "- RTT            : unavailable (not_collected)" in prompt
    assert "- RSSI           : unavailable (not_provided_by_api)" in prompt
    assert "0.0 ms" not in prompt
    assert "0.0%" not in prompt


def test_measured_zero_is_kept_as_a_real_zero(concierge):
    telemetry = {
        "device_health": {"rssi": -48, "battery_percentage": 5},
        "webrtc_stats": {"inbound_rtp": {"jitter": 0.0, "packet_loss_fraction": 0.0},
                         "candidate_pair": {"current_round_trip_time": 0.0}},
    }

    run(concierge, telemetry)

    prompt = concierge.llm_prompts[0]
    assert "- Jitter         : 0.0 ms" in prompt
    assert "- Packet loss    : 0.0%" in prompt
    assert "- RTT            : 0.0 ms" in prompt


def test_standby_placeholder_video_metrics_are_not_stream_measurements(concierge):
    telemetry = {"device_health": {"rssi": -48, "battery_percentage": 80}, "webrtc_stats": {}}

    # Standby reports 0 FPS / 0 kbps by design; that is not a degraded stream.
    assert concierge._precheck_anomaly(STANDBY_METRICS, telemetry, False) is None


# ---------------------------------------------------------------------------
# Power setup
# ---------------------------------------------------------------------------

def test_unknown_transformer_voltage_does_not_imply_battery_install(concierge):
    telemetry = {"device_health": {"rssi": -84, "battery_percentage": 60}, "webrtc_stats": {}}

    run(concierge, telemetry)

    prompt = concierge.llm_prompts[0]
    assert "BATTERY ONLY" not in prompt
    assert "Power Setup    : UNKNOWN" in prompt
    assert "- Transformer    : unavailable (not measured)" in prompt


def test_explicit_battery_power_mode_is_still_honoured(concierge):
    telemetry = {"device_health": {"rssi": -84, "battery_percentage": 60, "power_mode": "battery"},
                 "webrtc_stats": {}}

    run(concierge, telemetry)

    assert "Power Setup    : BATTERY ONLY" in concierge.llm_prompts[0]


# ---------------------------------------------------------------------------
# Insufficient data vs. no anomaly
# ---------------------------------------------------------------------------

def test_online_device_without_essential_metrics_is_not_healthy(concierge):
    result = run(concierge, partner_telemetry({"online": True}, {}))

    diag = result["diagnostic_result"]
    assert result["analysis_skipped"] is False
    assert diag["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert diag["root_cause_domain"] == "UNKNOWN"
    assert set(result["missing_metrics"]) == {
        "rssi", "jitter", "packet_loss_fraction", "battery_percentage", "transformer_voltage_vac",
    }
    assert diag["visual_findings"] == ["Camera in low-power standby mode. Optical sensor powered down by design."]
    assert concierge.llm_prompts == []


def test_missing_power_metrics_alone_block_healthy(concierge):
    telemetry = {"device_health": {"rssi": -48},
                 "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0.0}}}

    result = run(concierge, telemetry)

    assert result["diagnostic_result"]["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert result["missing_metrics"] == ["battery_percentage", "transformer_voltage_vac"]


def test_local_fallback_reports_insufficient_data_instead_of_nominal(concierge):
    local = concierge._build_local_diagnostic(
        STANDBY_METRICS | {"sharpness_score": 50.0}, partner_telemetry({"online": True}, {}), [], False)

    assert local["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert "nominal" not in json.dumps(local).lower()


# ---------------------------------------------------------------------------
# Simulated telemetry with real metrics keeps its behaviour
# ---------------------------------------------------------------------------

def test_simulated_nominal_telemetry_still_skips_bedrock(concierge):
    result = run(concierge, TelemetrySimulator().get_telemetry("nominal"))

    assert result["analysis_skipped"] is True
    assert result["diagnostic_result"]["diagnostic_status"] == "HEALTHY"   # F2: explicit, not null
    assert concierge.llm_prompts == []


def test_simulated_wifi_degradation_still_uses_bedrock(concierge):
    result = run(concierge, TelemetrySimulator().get_telemetry("wifi_degradation"))

    assert result["_provider"] == "bedrock"
    assert result["diagnostic_result"]["root_cause_domain"] == "NETWORK"
    prompt = concierge.llm_prompts[0]
    assert "- RSSI           : -84 dBm" in prompt
    assert "- Jitter         : 85.0 ms" in prompt
    assert "unavailable (not measured)" in prompt  # transformer is absent in this scenario


def test_simulated_wifi_degradation_fallback_is_unchanged(concierge):
    concierge._fake_llm_response = "not json"

    result = run(concierge, TelemetrySimulator().get_telemetry("wifi_degradation"))

    diag = result["diagnostic_result"]
    assert result["_provider"] == "local_fallback"
    assert diag["diagnostic_status"] == "DEGRADED"
    assert diag["root_cause_domain"] == "NETWORK"
    # Every anomaly is listed (S1-7), each with its band from the severity table.
    findings = diag["telemetry_findings"]
    assert findings[0].startswith("Wi-Fi signal (RSSI) is -84 dBm (Poor: < -75 dBm, DEGRADED")
    assert any(f.startswith("Packet loss is 18%") for f in findings)
    assert any(f.startswith("Jitter is 85 ms") for f in findings)
    assert any(f.startswith("Frame rate is 15 FPS") for f in findings)


# ---------------------------------------------------------------------------
# Telemetry-only diagnosis (camera state unknown, no frame) vs. real standby
# ---------------------------------------------------------------------------

NO_FRAME = "No video frame available: image retrieval is not integrated. Telemetry only."


def test_no_frame_prompt_is_telemetry_only_not_standby(concierge):
    telemetry = {"device_health": {"rssi": -84, "battery_percentage": 60}, "webrtc_stats": {}}

    result = concierge.process_stream(telemetry=telemetry, camera_active=True, no_frame_reason=NO_FRAME)

    prompt = concierge.llm_prompts[0]
    assert "TELEMETRY-ONLY DIAGNOSIS" in prompt
    assert "STANDBY" not in prompt.split("=== APPLICABLE TROUBLESHOOTING PROCEDURES")[0]
    assert "- Not available: no video frame was analyzed." in prompt
    assert result["diagnostic_result"]["visual_findings"] == [NO_FRAME]
    assert result["video_analysis"] == {"available": False, "reason": NO_FRAME}
    assert result["video_metrics"]["status"] == "UNAVAILABLE"


def test_no_frame_local_fallback_is_not_standby(concierge):
    concierge._fake_llm_response = "not json"
    telemetry = {"device_health": {"rssi": -84, "battery_percentage": 60}, "webrtc_stats": {}}

    result = concierge.process_stream(telemetry=telemetry, camera_active=True, no_frame_reason=NO_FRAME)

    diag = result["diagnostic_result"]
    assert result["_provider"] == "local_fallback"
    assert diag["root_cause_domain"] == "NETWORK"
    assert diag["visual_findings"] == [NO_FRAME]
    assert "standby" not in json.dumps(diag).lower()


def test_no_frame_insufficient_and_skipped_results_say_no_video(concierge):
    insufficient = concierge.process_stream(
        telemetry=partner_telemetry({"online": True}, {}), camera_active=True, no_frame_reason=NO_FRAME)
    skipped = concierge.process_stream(
        telemetry=TelemetrySimulator().get_telemetry("nominal"), camera_active=True, no_frame_reason=NO_FRAME)

    assert insufficient["diagnostic_result"]["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert insufficient["diagnostic_result"]["visual_findings"] == [NO_FRAME]
    assert insufficient["video_analysis"]["available"] is False
    assert skipped["analysis_skipped"] is True
    assert "no video frame was analyzed" in skipped["skip_reason"]
    assert concierge.llm_prompts == []


def test_real_standby_still_described_as_standby(concierge):
    telemetry = {"device_health": {"rssi": -84, "battery_percentage": 60}, "webrtc_stats": {}}

    result = concierge.process_stream(telemetry=telemetry, camera_active=False)

    assert "CAMERA STATE: STANDBY" in concierge.llm_prompts[0]
    assert result["diagnostic_result"]["visual_findings"] == [
        "Camera in low-power standby mode. Optical sensor powered down by design."
    ]
    assert "video_analysis" not in result


def test_standby_fallback_does_not_report_optical_fault_from_placeholder_metrics(concierge):
    concierge._fake_llm_response = "not json"
    telemetry = {"device_health": {"rssi": -48, "battery_percentage": 80},
                 "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0.0,
                                                  "frames_per_second": 12}}}

    diag = concierge.process_stream(telemetry=telemetry, camera_active=False)["diagnostic_result"]

    # Standby sharpness 0.0 is a placeholder; the real anomaly is the low WebRTC FPS.
    assert diag["root_cause_domain"] == "NETWORK"
