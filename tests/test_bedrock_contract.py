"""
Block D / F: the Bedrock reasoning contract, the local fallback mock-up and the CLI output.
Bedrock is always a test double here (tests/conftest.py blocks real boto3 clients); no network.
"""
import json
import re
import sys

import pytest
from fastapi.testclient import TestClient

from src.agent import concierge as concierge_module
from src.agent.concierge import (
    CUSTOMER_TEXT_MAX_WORDS, SYSTEM_PROMPT, SupportConcierge, customer_text_problems, findings_problems,
)
from src.agent.knowledge_retriever import KnowledgeRetriever
from src.api.server import app
from src.config import severity
from src.utils.telemetry_simulator import TelemetrySimulator

KB = {a["id"]: a for a in KnowledgeRetriever().kb_data}


def reply(status="DEGRADED", domain="NETWORK", mitigation=None, findings=None):
    return json.dumps({
        "diagnostic_status": status, "root_cause_domain": domain, "confidence_score": 0.85,
        "telemetry_findings": findings or ["Wi-Fi signal is -84 dBm, in the Poor band."],
        "visual_findings": ["Camera in low-power standby mode. Optical sensor powered down by design."],
        "recommended_mitigation": mitigation or (
            "Your doorbell's Wi-Fi signal is weak, so video may freeze. Move your router closer, then check "
            "the signal in the Ring app."),
    })


def rssi_only(rssi=-84):
    """Web UI case N8: standby, no frame, only the RSSI outside the GREEN band."""
    telemetry = TelemetrySimulator().get_telemetry("nominal")
    telemetry["device_health"]["rssi"] = rssi
    return telemetry


class Script:
    """Bedrock double that answers with the given replies in order and records every prompt."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt, _image=None):
        self.prompts.append(prompt)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(script, telemetry=None, **kwargs):
    agent = SupportConcierge()
    agent._call_llm = script
    return agent.process_stream(telemetry=telemetry or rssi_only(), camera_active=kwargs.pop("camera_active", False),
                                **kwargs)


# ---------------------------------------------------------------------------
# D1 (N8): HEALTHY despite a pre-check anomaly gets one corrective retry
# ---------------------------------------------------------------------------

def test_healthy_then_degraded_uses_the_corrected_bedrock_answer():
    script = Script(reply("HEALTHY", "NONE"), reply("DEGRADED"))
    result = run(script)

    assert result["_provider"] == "bedrock"
    assert result["bedrock_attempts"] == 2
    assert result["diagnostic_result"]["diagnostic_status"] == "DEGRADED"
    correction = script.prompts[1]
    assert "YOUR PREVIOUS ANSWER WAS REJECTED" in correction
    assert "HEALTHY although the pre-check found: Poor Wi-Fi signal" in correction


def test_healthy_twice_falls_back_to_the_local_mock_up():
    script = Script(reply("HEALTHY", "NONE"), reply("HEALTHY", "NONE"))
    result = run(script)

    assert result["_provider"] == "local_fallback"
    assert len(script.prompts) == 2
    assert result["diagnostic_result"]["diagnostic_status"] == "DEGRADED"
    assert result["bedrock_attempts"] == 2      # both requests are counted (billing)


@pytest.mark.parametrize("second, provider", [("not json", "local_fallback"), (reply(), "bedrock")])
def test_invalid_answer_gets_one_retry(second, provider):
    script = Script("not json", second)
    assert run(script)["_provider"] == provider
    assert len(script.prompts) == 2


def test_a_call_error_is_not_retried():
    script = Script(RuntimeError("AccessDeniedException"))
    result = run(script)
    assert result["_provider"] == "local_fallback"
    assert len(script.prompts) == 1


def test_bedrock_answer_with_jargon_is_corrected_or_replaced():
    jargon = "Your RSSI is -84 dBm; improve jitter and packet loss on the WebRTC stream."
    script = Script(reply(mitigation=jargon), reply(mitigation=jargon))
    result = run(script)
    assert result["_provider"] == "local_fallback"
    assert "technical terms" in script.prompts[1]
    assert customer_text_problems(result["diagnostic_result"]["recommended_mitigation"]) == []


def test_findings_with_internal_field_names_are_rejected():
    script = Script(reply(findings=["battery_saver_low_voltage is false"]), reply())
    result = run(script)
    assert result["_provider"] == "bedrock" and result["bedrock_attempts"] == 2
    assert "internal field names: battery_saver_low_voltage" in script.prompts[1]


# ---------------------------------------------------------------------------
# D2 / D3: the prompt carries the level table, its provenance and the safety rules
# ---------------------------------------------------------------------------

PROMPT = " ".join(SYSTEM_PROMPT.split())


def test_prompt_contains_the_level_table_and_its_provenance():
    assert severity.PROVENANCE in PROMPT
    for line in severity.table_for_prompt().splitlines():
        assert " ".join(line.split()) in PROMPT
    assert "quote ONLY the boundary of its next better level" in PROMPT


def test_prompt_never_generalizes_the_mechanical_chime_rule():
    assert "never a requirement of the doorbell itself (for example not of the Video Doorbell 4)" in PROMPT
    assert "call them \"reference values (unverified)\"" in PROMPT


def test_prompt_forbids_electrical_work_and_explains_the_backup_battery():
    assert "no switching or resetting circuit breakers, no multimeter" in PROMPT
    assert "contact a qualified electrician or Ring Support" in PROMPT
    assert "Never call this inconsistent and never suggest stale telemetry" in PROMPT


def test_bedrock_request_leaves_room_for_one_finding_per_anomaly():
    import inspect
    assert '"max_tokens": 1024' in inspect.getsource(SupportConcierge._call_bedrock)


def test_prompt_states_the_customer_text_rules():
    assert f"at most {CUSTOMER_TEXT_MAX_WORDS - 10} words" in PROMPT
    assert "what is happening, why it matters, what to do" in PROMPT
    assert "\"in the Ring app\"" in PROMPT
    assert "For a blurry lens do not mention condensation, protective film, obstructions or sun glare" in PROMPT


def test_case_lists_precheck_anomalies_with_level_and_next_target():
    script = Script(reply())
    run(script)
    case = script.prompts[0]
    assert "=== PRE-CHECK ANOMALIES" in case
    assert "- DEGRADED: Wi-Fi signal (RSSI) -84 dBm: Poor (DEGRADED, < -75 dBm). To leave this band it must be " \
           "at or above -75 dBm." in case
    assert "diagnostic_status can NOT be HEALTHY" in case


def test_case_without_kb_match_asks_for_a_conservative_generated_mitigation(monkeypatch):
    monkeypatch.setattr(KnowledgeRetriever, "retrieve_guidance", lambda *_a, **_k: [])
    script = Script(reply())
    result = run(script)
    assert "NONE: no procedure matched. Give a conservative, generic mitigation" in script.prompts[0]
    assert result["mitigation_source"] == "generated_no_kb_match"


def test_bedrock_mitigation_with_kb_articles_is_labelled_knowledge_base():
    assert run(Script(reply()))["mitigation_source"] == "knowledge_base"


# ---------------------------------------------------------------------------
# D4: the customer-text contract itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, problem", [
    ("word " * (CUSTOMER_TEXT_MAX_WORDS + 1), "words"),
    ("Your RSSI is low.", "technical terms"),
    ("Check KB-RING-009 in the Ring app.", "article ID"),
    ("Your transformer_voltage_vac is low.", "internal field name"),
    ("Toggle the transformer breaker.", "electrical work"),
    ("Measure the voltage with a multimeter.", "electrical work"),
    ("Recharge it with a 5V/2A charger.", "electrical specification"),
    ("Check that the protective shipping film was removed.", "does not show"),
    ("Your transformer needs 16 VAC.", "electrical specification"),
])
def test_customer_text_problems(text, problem):
    assert any(problem in p for p in customer_text_problems(text))


def test_plain_customer_text_passes():
    assert customer_text_problems(KB["KB-RING-001"]["customer_messages"]["default"]) == []
    assert findings_problems({"telemetry_findings": ["Battery saver flag is set."], "visual_findings": []}) == []


@pytest.mark.parametrize("article_id", sorted(a for a, art in KB.items() if art.get("customer_messages")))
def test_every_kb_customer_message_fits_the_contract(article_id):
    for text in KB[article_id]["customer_messages"].values():
        assert customer_text_problems(text) == [], (article_id, text)


# ---------------------------------------------------------------------------
# D5: one table of user-facing domain labels
# ---------------------------------------------------------------------------

def test_domain_labels():
    assert severity.DOMAIN_LABELS["NETWORK"] == "Network Issue (Wi-Fi / Upstream)"
    assert severity.DOMAIN_LABELS["HARDWARE_POWER"] == "Power Supply Issue"
    assert severity.DOMAIN_LABELS["OPTICAL_PHYSICAL"] == "Camera Image Issue"
    assert set(concierge_module.LLM_DOMAINS) <= set(severity.DOMAIN_LABELS)


# ---------------------------------------------------------------------------
# D9: the local fallback mock-up, per simulator scenario
# ---------------------------------------------------------------------------

def scenario(name, **device_health):
    telemetry = TelemetrySimulator().get_telemetry(name)
    telemetry["device_health"].update(device_health)
    return telemetry


FALLBACK_CASES = [
    # (telemetry, status, domain, KB article, message key, findings that must be cited)
    (scenario("wifi_degradation"), "DEGRADED", "NETWORK", "KB-RING-001", "default",
     ["Wi-Fi signal (RSSI) is -84 dBm", "Packet loss is 18%", "Jitter is 85 ms", "Frame rate is 15 FPS"]),
    (scenario("nominal", power_mode="battery", battery_percentage=8, battery_saver_low_voltage=True),
     "CRITICAL", "HARDWARE_POWER", "KB-RING-009", "battery", ["Battery is 8%"]),
    (scenario("nominal", power_mode="wired", transformer_voltage_vac=11.0),
     "DEGRADED", "HARDWARE_POWER", "KB-RING-009", "transformer", ["Transformer voltage (wired) is 11 V AC"]),
    (scenario("nominal", power_mode="wired", transformer_voltage_vac=0.0),
     "CRITICAL", "HARDWARE_POWER", "KB-RING-009", "transformer", ["Transformer voltage (wired) is 0 V AC"]),
    (scenario("in_home_chime"), "DEGRADED", "HARDWARE_POWER", "KB-RING-013", "mechanical_chime",
     ["Transformer voltage for a mechanical in-home chime is 13.5 V AC"]),
]


@pytest.mark.parametrize("telemetry, status, domain, article, key, cited", FALLBACK_CASES)
def test_fallback_per_scenario(telemetry, status, domain, article, key, cited):
    result = run(Script(RuntimeError("Bedrock disabled")), telemetry)
    report = result["diagnostic_result"]

    assert result["_provider"] == "local_fallback"
    concierge_module._validate_llm_diagnostic(report)        # same schema as a Bedrock answer
    assert (report["diagnostic_status"], report["root_cause_domain"]) == (status, domain)
    assert report["recommended_mitigation"] == KB[article]["customer_messages"][key]
    assert result["mitigation_source"] == "knowledge_base"
    assert report["confidence_score"] == concierge_module.FALLBACK_CONFIDENCE
    for text in cited:
        assert any(f.startswith(text) for f in report["telemetry_findings"]), (text, report["telemetry_findings"])
    assert customer_text_problems(report["recommended_mitigation"]) == []
    assert findings_problems(report) == []
    assert "90%" not in report["recommended_mitigation"]       # never the solar article's advice


def test_fallback_cites_the_low_file_bitrate_of_the_wifi_cli_case(tmp_path, monkeypatch):
    """S1-7: the CLI wifi case also lists the analyzer's stream issues (bitrate 103 kbps < 150)."""
    from pathlib import Path
    sample = Path(__file__).resolve().parents[1] / "data" / "samples" / "bad_internet.mp4"
    agent = SupportConcierge()      # loads the KB from the repository root
    agent._call_llm = Script(RuntimeError("off"))
    monkeypatch.chdir(tmp_path)   # the analyzer writes its evidence frame under ./data/frames
    report = agent.process_stream(video_path=str(sample), telemetry=scenario("wifi_degradation"))["diagnostic_result"]
    visual = " ".join(report["visual_findings"])
    assert "Critically low bitrate (103.52 kbps < 150 kbps)" in visual
    assert "Low resolution" in visual and "Deficient frame rate" in visual


def test_nominal_scenario_never_reaches_the_fallback():
    script = Script()
    result = run(script, scenario("nominal"))
    assert result["_provider"] == "precheck" and script.prompts == []
    assert result["diagnostic_result"]["diagnostic_status"] == "HEALTHY"


# ---------------------------------------------------------------------------
# D8 / F: provider in the API and the CLI; CLI step counters on every path
# ---------------------------------------------------------------------------

def test_api_analyze_reports_the_provider(monkeypatch):
    monkeypatch.setattr(SupportConcierge, "_call_llm", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("off")))
    client = TestClient(app)
    for preset, provider in (("nominal", "precheck"), ("wifi_degradation", "local_fallback")):
        tel = client.post("/api/telemetry", json={"mode": "preset", "scenario": preset}).json()
        body = client.post("/api/analyze", json={"video_path": "good_internet.mp4", "telemetry": tel,
                                                  "camera_active": False, "source_mode": "twin"}).json()
        assert body["_provider"] == provider
        assert body["diagnostic_result"]["diagnostic_status"] in ("HEALTHY", "DEGRADED")


def cli(monkeypatch, capsys, scenario_name, video="data/samples/good_internet.mp4"):
    monkeypatch.setattr(SupportConcierge, "_call_llm", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("off")))
    monkeypatch.setattr(sys, "argv", ["concierge", "--video", video, "--scenario", scenario_name])
    concierge_module.main()
    out = capsys.readouterr().out
    report = json.loads(out.split("AGENT DIAGNOSTIC REPORT (JSON)")[1].split("=" * 65)[1].split("\nProblem type")[0])
    return out, report


@pytest.mark.parametrize("name, provider, status", [
    ("nominal", "precheck", "HEALTHY"),
    ("wifi_degradation", "local_fallback", "DEGRADED"),
    ("in_home_chime", "local_fallback", "DEGRADED"),
])
def test_cli_prints_an_explicit_report_with_its_provider(monkeypatch, capsys, name, provider, status):
    out, report = cli(monkeypatch, capsys, name)
    assert report["_provider"] == provider
    assert report["diagnostic_status"] == status
    for step in ("[1/4]", "[2/4]", "[3/4]", "[4/4]"):
        assert step in out, step
    assert "Problem type:" in out


@pytest.mark.parametrize("kwargs, step2, step34", [
    ({"camera_active": False}, "[2/4] Camera in standby: no frame to extract", "[4/4] No anomaly: reasoning engine not invoked"),
    ({"camera_active": False, "no_frame_reason": "no frame"}, "[2/4] No video frame available",
     "[4/4] No anomaly: reasoning engine not invoked"),
])
def test_every_path_prints_all_four_steps(capsys, kwargs, step2, step34):
    agent = SupportConcierge()
    agent.process_stream(telemetry=scenario("nominal"), **kwargs)
    out = capsys.readouterr().out
    assert step2 in out and step34 in out
    assert "[3/4] Pre-check nominal: skipping SOP retrieval" in out


def test_live_frame_path_prints_all_four_steps(capsys):
    from tests.test_video_pipeline import as_canvas_jpeg, sample_frame
    agent = SupportConcierge()
    agent._call_llm = Script(RuntimeError("off"))
    agent.process_stream(telemetry=rssi_only(), frame_base64=as_canvas_jpeg(sample_frame()), camera_active=True)
    out = capsys.readouterr().out
    assert re.search(r"\[1/4\].*\n(.*\n)*\[2/4\] Live keyframe.*\n(.*\n)*\[3/4\] Pre-check: DEGRADED.*\n(.*\n)*\[4/4\]", out)
