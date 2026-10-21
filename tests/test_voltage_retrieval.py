"""
B4: knowledge retrieval for transformer voltage follows the project's general 12 V heuristic
(TRANSFORMER_MIN_VAC) and the specific mechanical in-home chime rule (16 V, only when the
telemetry reports chime_type_setting="mechanical"). Neither value is a Ring-published universal
requirement. No network or Bedrock calls.
"""
import json
import re
from pathlib import Path

import pytest

from src.agent import concierge
from src.agent.concierge import SupportConcierge
from src.agent.knowledge_retriever import KnowledgeRetriever
from src.utils.telemetry_simulator import TelemetrySimulator

retriever = KnowledgeRetriever()
KB = {a["id"]: a for a in retriever.kb_data}
POWER, CHIME = "KB-RING-009", "KB-RING-013"

VIDEO = {"status": "OPTIMAL", "issues": [], "fps": None, "bitrate_kbps": None,
         "sharpness_score": 200.0, "optical_banding_detected": False}


def wired(volts, rssi=-50, **device_health):
    return {"device_health": {"rssi": rssi, "battery_percentage": 100,
                              "transformer_voltage_vac": volts, **device_health},
            "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0.0,
                                             "frames_per_second": 30, "bitrate_kbps": 1200}}}


def ids(tel):
    return [a["id"] for a in retriever.retrieve_guidance(VIDEO, tel)]


def test_retriever_uses_the_backend_transformer_threshold():
    assert retriever.TRANSFORMER_MIN_VAC == concierge.TRANSFORMER_MIN_VAC == 12.0


# Generic wired device (no chime information): the power procedure only below 12 V,
# and the mechanical chime procedure never.
@pytest.mark.parametrize("volts, power_expected", [(11.5, True), (12.0, False), (13.0, False), (14.0, False)])
def test_generic_wired_voltage_retrieval(volts, power_expected):
    retrieved = ids(wired(volts))
    assert (POWER in retrieved) is power_expected
    assert CHIME not in retrieved


# Unchanged behaviour: a mechanical chime below 16 V still retrieves the chime procedure first.
@pytest.mark.parametrize("volts", [11.5, 12.0, 13.0, 13.5, 14.0])
def test_mechanical_chime_below_16v_retrieves_the_chime_procedure(volts):
    assert CHIME in ids(wired(volts, chime_type_setting="mechanical"))


def test_mechanical_chime_at_16v_does_not_retrieve_the_chime_procedure():
    assert CHIME not in ids(wired(16.0, chime_type_setting="mechanical"))


def test_digital_chime_is_not_judged_by_the_mechanical_rule():
    assert CHIME not in ids(wired(13.5, chime_type_setting="digital"))


def test_simulator_in_home_chime_scenario_retrieves_the_chime_procedure_first():
    assert ids(TelemetrySimulator().get_telemetry("in_home_chime"))[0] == CHIME


def test_battery_only_critical_battery_still_retrieves_the_power_procedure():
    tel = wired(None)
    tel["device_health"].update(power_mode="battery", battery_percentage=5)
    assert POWER in ids(tel)


@pytest.mark.parametrize("volts", [12.0, 13.0, 14.0])
def test_retrieval_does_not_contradict_a_network_diagnosis(volts):
    """Wired device at a voltage the diagnosis accepts, with weak Wi-Fi: no power advice."""
    agent = SupportConcierge()
    tel = wired(volts, rssi=-85)
    articles = retriever.retrieve_guidance(VIDEO, tel)
    report = agent._build_local_diagnostic(VIDEO, tel, articles, True)

    assert agent._precheck_anomaly(VIDEO, tel, True)["domain"] == "NETWORK"
    assert report["root_cause_domain"] == "NETWORK"
    assert {a["id"] for a in articles}.isdisjoint({POWER, CHIME})
    assert articles[0]["root_cause_domain"] == "NETWORK"


def test_retrieval_agrees_with_a_power_diagnosis_below_12v():
    agent = SupportConcierge()
    tel = wired(11.5)
    articles = retriever.retrieve_guidance(VIDEO, tel)
    report = agent._build_local_diagnostic(VIDEO, tel, articles, True)

    assert report["root_cause_domain"] == "HARDWARE_POWER"
    assert articles[0]["id"] == POWER
    # Plain-language power mitigation (generic until the KB article carries customer messages).
    from src.agent.concierge import _GENERIC_MITIGATIONS
    assert report["recommended_mitigation"] == _GENERIC_MITIGATIONS["HARDWARE_POWER"]


# Voltage texts: no 12 V or 16 V universal requirement presented as official without a
# verifiable source.

def article_text(article_id):
    return json.dumps(KB[article_id], ensure_ascii=False)


@pytest.mark.parametrize("article_id", [POWER, CHIME])
def test_voltage_articles_flag_their_provenance_as_pending(article_id):
    assert KB[article_id]["source_verification"].startswith("pending")


@pytest.mark.parametrize("article_id", [POWER, CHIME])
def test_voltage_articles_state_no_universal_16v_requirement(article_id):
    text = article_text(article_id).lower()
    assert not re.search(r"must (be|maintain)[^\"]*16", text)
    assert "ring video doorbells connected to low-voltage transformers (< 16 vac" not in text


def test_kb_never_presents_12v_as_a_ring_requirement():
    for article in retriever.kb_data:
        for fact in article.get("official_facts", []):
            assert not re.search(r"\b12 ?v", fact.lower()), (article["id"], fact)


def test_mechanical_chime_signal_is_marked_as_a_project_rule():
    signal = next(s for s in KB[CHIME]["official_signals"] if s["metric"] == "transformer_voltage_vac")
    assert signal["requires"] == {"chime_type_setting": "mechanical"}
    assert "project diagnostic rule" in signal["interpretation"].lower()


# Final round: no 16-24 V universal requirement and no unverified endpoint presented as confirmed.

APP_JS = (Path(__file__).resolve().parents[1] / "web" / "js" / "app.js").read_text(encoding="utf-8")


def test_ui_does_not_present_16_24v_as_a_universal_requirement():
    assert not re.search(r"16\s*[-–]\s*24\s*V", APP_JS)


def test_power_article_facts_do_not_attribute_16_24v_to_ring():
    for fact in KB[POWER]["official_facts"]:
        assert not re.search(r"16\s*[-–]\s*24", fact), fact


def test_power_requirement_figures_are_flagged_as_unverified():
    for entry in KB[POWER]["official_power_requirements"]:
        assert entry["verification"].startswith("pending"), entry


def test_chime_fact_presents_16v_as_a_project_rule():
    facts = [f for f in KB[CHIME]["official_facts"] if "16" in f]
    assert facts and all("project" in f.lower() for f in facts)


def test_configurations_endpoint_is_never_presented_as_confirmed():
    entries = [(a["id"], m) for a in retriever.kb_data for m in a.get("official_api_mapping", [])
               if m.get("endpoint", "").endswith("/configurations")]
    assert entries
    for article_id, mapping in entries:
        assert mapping["verification"].startswith("unverified"), article_id
