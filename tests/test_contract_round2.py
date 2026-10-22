"""
Second round of simulator findings (T4-T9, T11, T14, T15): what reaches the model, what the
contract rejects and how a rejected customer text degrades. Bedrock is always a test double.
"""
import json
import re

import numpy as np
import pytest

from src.agent import concierge as concierge_module
from src.agent.concierge import SAFETY_SENTENCE, SupportConcierge, customer_text_problems, findings_problems
from src.agent.knowledge_retriever import KnowledgeRetriever
from src.utils.telemetry_simulator import TelemetrySimulator
from src.video_pipeline.analyzer import VideoAnalyzer
from tests.test_bedrock_contract import KB, Script, reply
from tests.test_retrieval_cases import pink_frame
from tests.test_video_pipeline import as_canvas_jpeg, dirty_lens, sample_frame

ELECTRICAL = "electrical work"


def telemetry(name="nominal", **device_health):
    t = TelemetrySimulator().get_telemetry(name)
    t["device_health"].update(device_health)
    return t


def black_frame():
    return np.zeros_like(sample_frame())


def prompt_for(t, frame=None):
    """The case prompt the model would receive for this telemetry (and frame, when the camera is on)."""
    script = Script(reply())
    agent = SupportConcierge()
    agent._call_llm = script
    kwargs = {"frame_base64": as_canvas_jpeg(frame), "camera_active": True} if frame is not None \
        else {"camera_active": False}
    result = agent.process_stream(telemetry=t, **kwargs)
    return script.prompts[0], result


# ---------------------------------------------------------------------------
# T4 (C4): visual findings describe image quality, never people, objects or places
# ---------------------------------------------------------------------------

OBSERVED_SCENE_FINDINGS = [
    "a person in a yellow and gray jacket and black cap, holding a tablet, standing in front of a brick "
    "building with a dark door",
    "clear visibility of face, clothing, and surroundings",
    "person in vest, house siding, trees",
    "a person in outdoor daylight",
]


@pytest.mark.parametrize("finding", OBSERVED_SCENE_FINDINGS)
def test_scene_descriptions_are_rejected(finding):
    problems = findings_problems({"telemetry_findings": [], "visual_findings": [finding]})
    assert any("people, objects or places" in p for p in problems), problems


def test_image_quality_finding_passes():
    finding = "Strong pink/magenta cast across the whole frame; colors shifted, exposure normal."
    assert findings_problems({"telemetry_findings": [], "visual_findings": [finding]}) == []


def test_prompt_forbids_scene_descriptions():
    assert "Never describe people, faces, clothing, objects, license plates" in " ".join(
        concierge_module.SYSTEM_PROMPT.split())


@pytest.mark.parametrize("frame", [dirty_lens(sample_frame()), pink_frame(), black_frame()],
                         ids=["dirty_lens", "pink", "black"])
def test_local_fallback_visual_findings_respect_privacy(frame):
    agent = SupportConcierge()
    agent._call_llm = Script(RuntimeError("off"))
    result = agent.process_stream(telemetry=telemetry(), frame_base64=as_canvas_jpeg(frame), camera_active=True)
    assert result["_provider"] == "local_fallback"
    assert findings_problems(result["diagnostic_result"], result["video_metrics"]) == []


# ---------------------------------------------------------------------------
# T7 (C10): an in-range sharpness score is never called low
# ---------------------------------------------------------------------------

OBSERVED_SHARPNESS = "Sharpness score 183.26 is low but consistent with color channel misalignment from stuck IR filter"


def test_low_sharpness_claim_with_an_in_range_score_is_rejected():
    problems = findings_problems({"visual_findings": [OBSERVED_SHARPNESS]}, {"sharpness_score": 183.26})
    assert any("sharpness low" in p for p in problems), problems


def test_low_sharpness_claim_with_a_low_score_passes():
    assert findings_problems({"visual_findings": ["Sharpness score 13.2 is low (blur)."]},
                             {"sharpness_score": 13.2}) == []


def test_prompt_marks_an_in_range_sharpness_as_not_to_interpret():
    case, _ = prompt_for(telemetry(), pink_frame())
    assert re.search(r"Sharpness score: [\d.]+ \(in range, blur limit 40: quote it, do not interpret it\)", case)


# ---------------------------------------------------------------------------
# T8 (C12): threshold claims must be numerically true; levels are precomputed
# ---------------------------------------------------------------------------

def test_false_threshold_claim_is_rejected():
    finding = "Bitrate is 103.52 kbps (OpenCV) and 180 kbps (WebRTC), both below the 150 kbps threshold."
    problems = findings_problems({"telemetry_findings": [finding], "visual_findings": []})
    assert any("180 kbps is not below the 150 kbps threshold" in p for p in problems), problems


def test_true_threshold_claim_passes():
    finding = "Bitrate is 103.52 kbps (OpenCV), below the 150 kbps threshold; RSSI -84 dBm is below the -75 dBm threshold."
    assert findings_problems({"telemetry_findings": [finding], "visual_findings": []}) == []


def test_prompt_gives_precomputed_levels_per_metric():
    case, _ = prompt_for(telemetry("wifi_degradation"))
    assert "=== METRIC LEVELS (precomputed from the severity table; use them, do not compare numbers yourself)" in case
    assert "- Wi-Fi signal (RSSI): -84 dBm = DEGRADED (Poor, < -75 dBm); GREEN when at or above -60 dBm" in case
    assert "Never compare numbers yourself" in concierge_module.SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# T9 (C3, N42): the improvement target is the GREEN boundary of each metric
# ---------------------------------------------------------------------------

def test_prompt_carries_the_green_boundary_of_each_anomaly():
    case, _ = prompt_for(telemetry("wifi_degradation"))
    anomalies = case.split("=== PRE-CHECK ANOMALIES")[1].split("=== APPLICABLE")[0]
    assert "To return to GREEN it must be at or above -60 dBm." in anomalies
    assert "To leave this band" not in case


# ---------------------------------------------------------------------------
# T5 (C5, C8, C13): no KB electrical figures, electrical work, unseen causes or field names reach the model
# ---------------------------------------------------------------------------

PROMPT_CASES = {
    "battery_20": (telemetry(power_mode="battery", battery_percentage=20), None),
    "battery_9": (telemetry(power_mode="battery", battery_percentage=9), None),
    "dirty_lens": (telemetry(), dirty_lens(sample_frame())),
    "wifi_degraded": (telemetry("wifi_degradation"), None),
    "transformer_11v": (telemetry(power_mode="wired", transformer_voltage_vac=11.0), None),
    "chime": (telemetry("in_home_chime"), None),
    "power_cut_black": (telemetry(power_mode="wired", transformer_voltage_vac=0.0), black_frame()),
}
KB_FIGURES = ("5V/2A", "16V", "30VA", "30 VA", "16 VAC", "48V", "15.4W")
KB_ELECTRICAL_ACTIONS = ("Install an upgraded doorbell transformer", "Pro Power Kit", "jumper",
                         "Check transformer voltage", "terminal wiring", "Power cycle")
UNSEEN_CAUSES = ("condensation", "shipping film", "scratches", "reflective surfaces")
INTERNAL_FIELDS = ("power_mode", "transformer_voltage_vac", "battery_percentage", "battery_saver_low_voltage",
                   "packet_loss_fraction", "chime_type_setting", "camera_active", "sharpness_score",
                   "bitrate_kbps", "mean_brightness", "recommended_actions", "customer_messages",
                   "official_signals", "issue_category", "official_power_requirements")


@pytest.mark.parametrize("name", PROMPT_CASES)
def test_prompt_is_sanitized(name):
    t, frame = PROMPT_CASES[name]
    case, result = prompt_for(t, frame)
    assert result["retrieved_articles"], "the case must reach the KB section"
    full = concierge_module.SYSTEM_PROMPT + case
    for text in KB_FIGURES + KB_ELECTRICAL_ACTIONS + UNSEEN_CAUSES:
        assert text.lower() not in case.lower(), (name, text)
    for field in INTERNAL_FIELDS:
        assert not re.search(rf"\b{field}\b", full), (name, field)


def test_kb_actions_are_kept_in_their_safe_form():
    case, _ = prompt_for(*PROMPT_CASES["battery_9"])
    assert "Action: For battery models, remove battery and recharge fully before reinserting." in case
    case, _ = prompt_for(*PROMPT_CASES["dirty_lens"])
    assert "Action: Gently clean the camera lens with a soft, clean microfiber cloth." in case


# ---------------------------------------------------------------------------
# T14: a transformer power cut explains the black frame
# ---------------------------------------------------------------------------

def test_power_cut_with_a_black_frame_retrieves_only_the_power_article():
    t = telemetry(power_mode="wired", transformer_voltage_vac=0.0)
    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(black_frame()), t)
    assert [a["id"] for a in KnowledgeRetriever().retrieve_guidance(metrics, t)] == ["KB-RING-009"]


def test_black_frame_without_a_power_cut_keeps_the_optical_articles():
    t = telemetry()
    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(black_frame()), t)
    assert "KB-RING-005" in [a["id"] for a in KnowledgeRetriever().retrieve_guidance(metrics, t)]


def test_prompt_explains_the_black_frame_as_a_power_symptom():
    case, _ = prompt_for(*PROMPT_CASES["power_cut_black"])
    assert "The black frame is explained by the loss of power (transformer power cut)" in case
    assert "not a separate optical problem" in case


# ---------------------------------------------------------------------------
# T6 (C9): no power, wiring or transformer actions for the customer; safe checks only
# ---------------------------------------------------------------------------

OBSERVED_POWER_CYCLE = "Power cycle the device by turning off power for 30 seconds, then turn it back on."
OBSERVED_WIRING_CHECK = ("Check that the transformer is plugged in and the wires are connected securely. "
                         "If the problem persists, contact a qualified electrician or Ring Support.")
VALID_BATTERY = ("Your doorbell's battery isn't charging. Make sure your charging cable is firmly connected at "
                 "both ends, then check the battery level in the Ring app. " + SAFETY_SENTENCE)
VALID_TRANSFORMER = KB["KB-RING-009"]["customer_messages"]["transformer"]


@pytest.mark.parametrize("text", [OBSERVED_POWER_CYCLE, OBSERVED_WIRING_CHECK,
                                  "Turn off the power at the breaker, then restart it.",
                                  "Unplug the doorbell for a minute.",
                                  "Check the wiring connections behind the doorbell."])
def test_power_and_wiring_actions_are_rejected(text):
    assert any(ELECTRICAL in p for p in customer_text_problems(text)), text


def test_valid_battery_check_passes():
    assert customer_text_problems(VALID_BATTERY) == []


def test_charging_check_needs_the_safety_sentence():
    without = VALID_BATTERY.replace(SAFETY_SENTENCE, "")
    assert any(ELECTRICAL in p for p in customer_text_problems(without))


def test_charging_check_is_not_allowed_for_transformer_cases():
    assert any(ELECTRICAL in p for p in customer_text_problems(VALID_BATTERY, charging_check_allowed=False))


@pytest.mark.parametrize("text", [
    VALID_TRANSFORMER,
    "Your doorbell isn't getting enough power from its wiring. "
    "Please contact a qualified electrician or Ring Support to check the doorbell's power supply.",
    "Restart your doorbell following the steps in the Ring app. " + SAFETY_SENTENCE,
])
def test_valid_referrals_pass_in_transformer_cases(text):
    assert customer_text_problems(text, charging_check_allowed=False) == []


def test_prompt_states_the_safe_check_policy():
    prompt = " ".join(concierge_module.SYSTEM_PROMPT.split())
    assert "restart your doorbell following the steps in the Ring app" in prompt
    assert "never ask the customer to check or touch the wiring" in prompt
    assert SAFETY_SENTENCE in prompt


def test_wiring_check_on_a_transformer_case_falls_back_to_the_kb_message():
    script = Script(reply("CRITICAL", "HARDWARE_POWER", mitigation=OBSERVED_WIRING_CHECK),
                    reply("CRITICAL", "HARDWARE_POWER", mitigation=OBSERVED_WIRING_CHECK))
    agent = SupportConcierge()
    agent._call_llm = script
    result = agent.process_stream(telemetry=telemetry(power_mode="wired", transformer_voltage_vac=0.0),
                                  camera_active=False)
    assert ELECTRICAL in script.prompts[1]
    assert result["diagnostic_result"]["recommended_mitigation"] == VALID_TRANSFORMER


# ---------------------------------------------------------------------------
# T11 (N36): the flicker message names the Anti-Flicker setting "in the Ring app"
# ---------------------------------------------------------------------------

def test_flicker_message_points_to_the_ring_app_setting():
    text = KB["KB-RING-014"]["customer_messages"]["default"]
    assert "Anti-Flicker setting in the Ring app" in text
    assert customer_text_problems(text) == []


# ---------------------------------------------------------------------------
# T15: a customer text rejected twice keeps the rest of Bedrock's answer
# ---------------------------------------------------------------------------

BAD_TEXT = "Your RSSI is -84 dBm; improve jitter on the WebRTC stream."
BEDROCK_FINDING = "Wi-Fi signal is -84 dBm, in the Poor band (Bedrock)."


def run_wifi(script):
    agent = SupportConcierge()
    agent._call_llm = script
    t = telemetry()
    t["device_health"]["rssi"] = -84
    return agent.process_stream(telemetry=t, camera_active=False)


def test_mitigation_rejected_twice_keeps_bedrock_with_the_kb_message():
    script = Script(reply(mitigation=BAD_TEXT, findings=[BEDROCK_FINDING]),
                    reply(mitigation=BAD_TEXT, findings=[BEDROCK_FINDING]))
    result = run_wifi(script)
    assert result["_provider"] == "bedrock" and result["bedrock_attempts"] == 2
    assert result["mitigation_source"] == "knowledge_base_deterministic"
    assert result["diagnostic_result"]["recommended_mitigation"] == KB["KB-RING-001"]["customer_messages"]["default"]
    assert result["diagnostic_result"]["telemetry_findings"] == [BEDROCK_FINDING]
    assert "_mitigation_rejected" not in result["diagnostic_result"]


def test_finding_rejected_twice_uses_the_local_fallback():
    bad = ["battery_saver_low_voltage is false"]
    result = run_wifi(Script(reply(findings=bad), reply(findings=bad)))
    assert result["_provider"] == "local_fallback"


def test_first_invalid_second_valid_uses_the_second_answer_as_is():
    good = "Your doorbell's Wi-Fi is weak, so video may freeze. Move your router closer, then check the Ring app."
    result = run_wifi(Script(reply(mitigation=BAD_TEXT), reply(mitigation=good)))
    assert result["_provider"] == "bedrock" and result["bedrock_attempts"] == 2
    assert result["mitigation_source"] == "knowledge_base"
    assert result["diagnostic_result"]["recommended_mitigation"] == good


def test_ui_labels_the_deterministic_mitigation_source():
    app_js = open("web/js/app.js", encoding="utf-8").read()
    assert "knowledge_base_deterministic: 'Deterministic KB message (Bedrock text rejected twice)'" in app_js


# ---------------------------------------------------------------------------
# Tanda 3 live verification: findings from real Bedrock answers
# ---------------------------------------------------------------------------

def test_live_v8_mounting_advice_about_obstruction_or_reflection_is_rejected():
    observed = ("Your doorbell's picture is blurry. Gently clean the camera lens with a soft, dry cloth, then check "
                "the live view in the Ring app. If the image remains unclear, adjust the mounting angle to ensure "
                "the lens is not obstructed or pointed at a reflective surface.")
    assert any("does not show" in p for p in customer_text_problems(observed))


def test_dirty_lens_prompt_drops_the_sunlight_mounting_action():
    case, _ = prompt_for(*PROMPT_CASES["dirty_lens"])
    assert "direct sunlight" not in case


@pytest.mark.parametrize("finding", [
    "Capture shows a sharp, well-exposed outdoor frame with good capacity for detail.",
    "Doorbell camera frame has a strong magenta cast; no doorway-level banding.",
])
def test_scene_words_inside_other_words_are_not_rejected(finding):
    assert findings_problems({"telemetry_findings": [], "visual_findings": [finding]}) == []
