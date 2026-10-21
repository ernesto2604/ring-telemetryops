"""
Regression tests for RING-004 (RSSI bands), RING-005 (pre-check vs fallback), RING-006 (model
output validation), RING-007 (scenario labels are not evidence) and RING-009 (offline reads).
Bedrock is never called: `_call_llm` is replaced in every test that reaches it.
"""
import json

import pytest

from src.agent import concierge as concierge_module
from src.agent.concierge import SupportConcierge
from src.agent.knowledge_retriever import KnowledgeRetriever
from src.utils.partner_telemetry import partner_device_to_telemetry
from src.utils.ring_partner_client import normalize_device

retriever = KnowledgeRetriever()
WIFI_ARTICLE = next(a for a in retriever.kb_data if a["id"] == "KB-RING-001")


def no_bedrock(*_args, **_kwargs):
    raise AssertionError("Bedrock must not be called")


@pytest.fixture
def agent(monkeypatch):
    instance = SupportConcierge()
    monkeypatch.setattr(instance, "_call_llm", no_bedrock)
    return instance


# ---------------------------------------------------------------------------
# RING-004: KB-RING-001 RSSI bands. Interpretation adopted: each "a_to_b" band includes the bound
# nearer 0 dBm and excludes the far one; "lte -80" is Very poor. These are the bands the KB cites
# from a Ring Help Center article; the Ring Partner API itself reports no RSSI.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rssi, band", [
    (-30, "Great"), (-59, "Great"),
    (-60, "Okay"), (-65, "Okay"), (-69, "Okay"),
    (-70, "Poor"), (-75, "Poor"), (-79.5, "Poor"),
    (-80, "Very poor"), (-84, "Very poor"), (-95, "Very poor"),
])
def test_each_rssi_value_falls_in_exactly_one_band(rssi, band):
    hits = [s["interpretation"] for s in WIFI_ARTICLE["official_signals"]
            if retriever._evaluate_signal(s, {"rssi_dbm": rssi}) is not None]
    assert hits == [band]


@pytest.mark.parametrize("flat", [{}, {"rssi_dbm": None}])
def test_unmeasured_rssi_matches_no_band(flat):
    assert all(retriever._evaluate_signal(s, flat) is None for s in WIFI_ARTICLE["official_signals"])


def wifi_telemetry(rssi):
    return {"device_health": {"rssi": rssi, "battery_percentage": 100},
            "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0.0}}}


OPTIMAL_VIDEO = {"status": "OPTIMAL", "issues": [], "fps": 30, "bitrate_kbps": 1200, "sharpness_score": 200}


def test_weak_signal_gets_the_wifi_procedure_and_excellent_signal_does_not():
    weak = [a["id"] for a in retriever.retrieve_guidance(OPTIMAL_VIDEO, wifi_telemetry(-84))]
    excellent = [a["id"] for a in retriever.retrieve_guidance(OPTIMAL_VIDEO, wifi_telemetry(-50))]
    assert "KB-RING-001" in weak
    assert "KB-RING-001" not in excellent


# ---------------------------------------------------------------------------
# RING-007: scenario labels never act as evidence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rssi", [-50, -84])
def test_identical_metrics_with_different_labels_retrieve_the_same_articles(rssi):
    results = {
        label: [a["id"] for a in retriever.retrieve_guidance(OPTIMAL_VIDEO, {**wifi_telemetry(rssi), "scenario": label})]
        for label in ("nominal", "power_issue", "motion_delayed", "audio_delay_jitter",
                      "live_view_timeout", "cold_battery_drain", "partner_status", "live")
    }
    assert len({tuple(ids) for ids in results.values()}) == 1, results


def test_label_alone_does_not_retrieve_a_power_or_motion_procedure():
    ids = [a["id"] for a in retriever.retrieve_guidance(OPTIMAL_VIDEO, {**wifi_telemetry(-50), "scenario": "power_issue"})]
    assert "KB-RING-009" not in ids and "KB-RING-010" not in ids


def test_twin_scenario_metrics_still_retrieve_their_procedure():
    from src.utils.telemetry_simulator import TelemetrySimulator

    power = TelemetrySimulator().get_telemetry("power_issue")
    ids = [a["id"] for a in retriever.retrieve_guidance(OPTIMAL_VIDEO, power)]
    assert "KB-RING-009" in ids   # driven by the simulated battery/transformer values


# ---------------------------------------------------------------------------
# RING-005: the pre-check and the local fallback name the same domain
# ---------------------------------------------------------------------------

def video(status="OPTIMAL", issues=(), banding=False):
    return {"status": status, "issues": list(issues), "fps": None, "bitrate_kbps": None,
            "sharpness_score": 200.0, "optical_banding_detected": banding}


def telemetry(rssi=-50, battery=100, fps=30, bitrate=1200, loss=0.0, jitter=0.002, transformer=None):
    return {"device_health": {"rssi": rssi, "battery_percentage": battery, "transformer_voltage_vac": transformer},
            "webrtc_stats": {"inbound_rtp": {"jitter": jitter, "packet_loss_fraction": loss,
                                             "frames_per_second": fps, "bitrate_kbps": bitrate}}}


@pytest.mark.parametrize("video_metrics, tel, domain", [
    # Low FPS (transport) with a clean frame: the analyzer also lists the FPS issue.
    (video("DEGRADED", ["Deficient frame rate (10.0 fps < 20 fps)"]), telemetry(fps=10), "NETWORK"),
    # Low bitrate together with a blur issue.
    (video("DEGRADED", ["Deficient sharpness / optical blur", "Critically low bitrate (100.0 kbps)"]),
     telemetry(bitrate=100), "NETWORK"),
    (video("DEGRADED", ["Deficient sharpness / optical blur"]), telemetry(), "OPTICAL_PHYSICAL"),
    (video("CRITICAL", ["Completely black screen"]), telemetry(), "OPTICAL_PHYSICAL"),
    (video(banding=True), telemetry(), "OPTICAL_PHYSICAL"),
    (video("DEGRADED", ["Deficient sharpness / optical blur"]), telemetry(rssi=-90), "NETWORK"),
    (video("DEGRADED", ["Deficient sharpness / optical blur"]), telemetry(battery=5), "HARDWARE_POWER"),
    (video(), telemetry(transformer=10.0), "HARDWARE_POWER"),
])
def test_precheck_and_fallback_agree_on_domain(agent, video_metrics, tel, domain):
    assert agent._precheck_anomaly(video_metrics, tel, True)["domain"] == domain
    assert agent._build_local_diagnostic(video_metrics, tel, [], True)["root_cause_domain"] == domain


# ---------------------------------------------------------------------------
# RING-017: shared thresholds, boundaries of each rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tel, anomalous", [
    (telemetry(battery=9), True), (telemetry(battery=10), False),
    (telemetry(transformer=11.9), True), (telemetry(transformer=12.0), False),
    (telemetry(rssi=-61), True), (telemetry(rssi=-60), False),   # Weak Wi-Fi band starts below -60 dBm (D3)
    (telemetry(loss=0.081), True), (telemetry(loss=0.08), False),
    (telemetry(jitter=0.0401), True), (telemetry(jitter=0.040), False),
    (telemetry(fps=19.9), True), (telemetry(fps=20), False),
    (telemetry(bitrate=149), True), (telemetry(bitrate=150), False),
])
def test_threshold_boundaries(agent, tel, anomalous):
    assert (agent._precheck_anomaly(video(), tel, True) is not None) is anomalous


# ---------------------------------------------------------------------------
# RING-006: model output must match the diagnostic contract
# ---------------------------------------------------------------------------

VALID = {"diagnostic_status": "DEGRADED", "root_cause_domain": "NETWORK", "confidence_score": 0.7,
         "telemetry_findings": ["RSSI is -84 dBm."], "visual_findings": [],
         "recommended_mitigation": "Move the router closer."}


def test_valid_model_output_is_accepted(agent):
    assert "_parse_error" not in agent._parse_response(json.dumps(VALID))


@pytest.mark.parametrize("change", [
    {"root_cause_domain": "MADE_UP"},
    {"root_cause_domain": None},
    {"diagnostic_status": "INSUFFICIENT_DATA"},
    {"diagnostic_status": "healthy"},
    {"confidence_score": 7},
    {"confidence_score": -0.1},
    {"confidence_score": "0.9"},
    {"confidence_score": True},
    {"telemetry_findings": "RSSI -91 dBm"},
    {"visual_findings": [1, 2]},
    {"recommended_mitigation": ""},
    {"recommended_mitigation": ["x"]},
])
def test_malformed_model_output_is_rejected(agent, change):
    assert "_parse_error" in agent._parse_response(json.dumps({**VALID, **change}))


@pytest.mark.parametrize("raw", ["[]", "null", "42", json.dumps({k: v for k, v in VALID.items() if k != "visual_findings"})])
def test_non_object_or_incomplete_output_is_rejected(agent, raw):
    assert "_parse_error" in agent._parse_response(raw)


def test_invalid_model_output_falls_back_to_local_rules(monkeypatch):
    instance = SupportConcierge()
    monkeypatch.setattr(instance, "_call_llm",
                        lambda *_a: json.dumps({**VALID, "root_cause_domain": "MADE_UP", "confidence_score": 0.99}))

    result = instance.process_stream(telemetry=telemetry(rssi=-90), camera_active=False)

    assert result["_provider"] == "local_fallback"
    assert result["_model"] is None
    assert result["diagnostic_result"]["root_cause_domain"] == "NETWORK"
    assert result["diagnostic_result"]["confidence_score"] != 0.99


# ---------------------------------------------------------------------------
# RING-009: an offline status read is reported as a single read, with its time
# ---------------------------------------------------------------------------

def partner_telemetry(status_attrs, capabilities=None):
    resource = {"type": "devices", "id": "ava1.ring.device.FRONT", "attributes": {"name": "Front"}}
    device = normalize_device(resource, {"attributes": status_attrs}, {"attributes": capabilities or {}})
    return partner_device_to_telemetry(device)


def test_partner_offline_read_cites_the_read_time_and_is_not_overconfident(agent):
    tel = partner_telemetry({"online": False})

    diag = agent.process_stream(telemetry=tel, camera_active=False, no_frame_reason="no frame")["diagnostic_result"]

    assert diag["diagnostic_status"] == "CRITICAL"
    assert diag["confidence_score"] < 0.9
    assert f"Ring Partner API reported the device offline in the status read taken at {tel['captured_at']}." \
        == diag["telemetry_findings"][0]
    assert "does not confirm a lasting outage" in diag["telemetry_findings"][1]


def test_offline_from_non_ring_telemetry_is_not_attributed_to_ring(agent):
    diag = agent.process_stream(telemetry={"_source": "client_supplied", "device_health": {"online": False}},
                                camera_active=False)["diagnostic_result"]

    assert diag["telemetry_findings"][0] == "The telemetry reported the device offline in the last status read."


def test_failed_status_read_is_not_offline(agent):
    resource = {"type": "devices", "id": "ava1.ring.device.FRONT", "attributes": {}}
    device = normalize_device(resource, None, None)
    device["device_health"]["measurement_status"]["online"] = "fetch_failed"
    tel = partner_device_to_telemetry(device)

    diag = agent.process_stream(telemetry=tel, camera_active=False, no_frame_reason="no frame")["diagnostic_result"]

    assert diag["diagnostic_status"] == "INSUFFICIENT_DATA"
    assert "offline" not in json.dumps(diag).lower()


@pytest.mark.parametrize("status_attrs, expected", [
    ({"online": True, "reported_at": "2026-10-19T10:00:00Z", "battery_status": {"percentage": 5}},
     "- Last check-in  : 2026-10-19T10:00:00Z"),              # recent or old: the reported time is shown
    ({"online": True, "reported_at": "2019-01-01T00:00:00Z", "battery_status": {"percentage": 5}},
     "- Last check-in  : 2019-01-01T00:00:00Z"),
    ({"online": True, "battery_status": {"percentage": 5}},
     "- Last check-in  : unavailable (not_reported)"),        # absent: never invented
    ({"online": True, "reported_at": "1970-01-01T00:00:00Z", "battery_status": {"percentage": 5}},
     "- Last check-in  : unavailable (not_reported)"),        # documented epoch placeholder
])
def test_prompt_shows_ring_check_in_time_as_reported(monkeypatch, status_attrs, expected):
    prompts = []
    instance = SupportConcierge()
    monkeypatch.setattr(instance, "_call_llm", lambda prompt, _img: prompts.append(prompt) or "not json")
    tel = partner_telemetry(status_attrs, {"battery_status": {"supported": True}})

    instance.process_stream(telemetry=tel, camera_active=False, no_frame_reason="no frame")

    assert expected in prompts[0]
    assert f"- Status read at : {tel['captured_at']}" in prompts[0]


def test_thresholds_are_defined_once():
    from src.config import severity
    assert concierge_module.TRANSFORMER_MIN_VAC == severity.limit("transformer", "Undervoltage") == 12.0
    assert not hasattr(concierge_module, "RSSI_DEGRADED_DBM")   # no second copy in the concierge


# ---------------------------------------------------------------------------
# RING-013: the prompt states where each telemetry block comes from
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source, label", [
    ("ring_partner_api", "Ring Partner API device status; it reports no RSSI, power wiring or WebRTC stats"),
    ("mock", "Digital Twin simulator: SIMULATED values, not from a Ring device"),
    ("client_supplied", "sent by the caller; not verified by Ring"),
    ("anything_else", "anything_else (unverified; not Ring data)"),
    (None, "not stated (unverified; not Ring data)"),
])
def test_prompt_labels_the_telemetry_source(monkeypatch, source, label):
    prompts = []
    instance = SupportConcierge()
    monkeypatch.setattr(instance, "_call_llm", lambda prompt, _img: prompts.append(prompt) or "not json")
    tel = telemetry(battery=5)
    if source is not None:
        tel["_source"] = source

    instance.process_stream(telemetry=tel, camera_active=False)

    assert label in prompts[0]
    assert "(Ring Partner API) ===" not in prompts[0]
    assert "OFFICIAL RING SOPS" not in prompts[0]


def test_partner_telemetry_scenario_is_a_status_read_not_live():
    assert partner_telemetry({"online": True})["scenario"] == "partner_status"
