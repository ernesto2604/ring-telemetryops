"""
Final round: the KB, the Bedrock prompt, the README and the UI never present unverified content
as official Ring documentation. Keys stay the same (`official_*`) for /api/kb compatibility;
the verification state is carried by `evidence_level`, `source_verification` and `verification`.
"""
import json
import re
from pathlib import Path

import pytest

from src.agent.concierge import SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[1]
RAW = json.loads((ROOT / "data" / "knowledge_base" / "ring_video_troubleshooting.json").read_text(encoding="utf-8"))
KB = {a["id"]: a for a in RAW["issues"]}
MAPPINGS = [(a["id"], m) for a in RAW["issues"] for m in a.get("official_api_mapping", [])]


def test_policy_defines_the_unverified_level():
    assert "unverified" in RAW["evidence_policy"]


@pytest.mark.parametrize("article_id", sorted(KB))
def test_no_article_is_labelled_official_without_a_verified_source(article_id):
    article = KB[article_id]
    assert article["evidence_level"] == "unverified"
    assert article["declared_evidence_level"] in RAW["evidence_policy"]


@pytest.mark.parametrize("article_id", ["KB-RING-009", "KB-RING-013"])
def test_pending_source_verification_matches_the_evidence_level(article_id):
    assert KB[article_id]["source_verification"].startswith("pending")
    assert KB[article_id]["evidence_level"] != "official"


def test_every_api_mapping_is_marked_unverified():
    assert MAPPINGS
    for article_id, mapping in MAPPINGS:
        assert mapping["verification"].startswith("unverified"), (article_id, mapping["endpoint"])


def mapping(article_id, endpoint_part):
    return next(m for a, m in MAPPINGS if a == article_id and endpoint_part in m["endpoint"])


def test_motion_events_endpoint_is_not_presented_as_queryable():
    note = mapping("KB-RING-010", "/events")["verification"].lower()
    assert "motion" in note and "not confirmed" in note and "not implemented" in note


def test_whep_endpoint_is_marked_not_implemented():
    assert "not implemented" in mapping("KB-RING-015", "/whep/")["verification"].lower()


@pytest.mark.parametrize("article_id", ["KB-RING-011", "KB-RING-012"])
def test_device_attribute_mappings_are_unverified(article_id):
    note = mapping(article_id, "GET /v1/devices/{device_id}")["verification"].lower()
    assert "attribute" in note


def test_prompt_separates_verified_facts_pending_claims_and_heuristics():
    text = " ".join(SYSTEM_PROMPT.split())
    assert "never cite them as official Ring documentation" in text
    assert "project diagnostic heuristics, not Ring requirements" in text
    assert "Only telemetry labelled ring_partner_api comes from Ring" in text


@pytest.mark.parametrize("path", ["web/index.html", "web/js/app.js"])
def test_ui_does_not_call_the_report_official(path):
    assert not re.search(r"official report", (ROOT / path).read_text(encoding="utf-8"), re.I)


def test_readme_documents_the_chime_rule_and_the_threshold_copies():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "chime_type_setting" in readme
    assert "defined once" not in readme
    assert "test_threshold_parity.py" in readme


def test_kb_text_names_no_unverified_scope_and_no_official_category():
    text = json.dumps(RAW["issues"], ensure_ascii=False)
    assert "ring:device:" not in text
    assert "Official Ring" not in text


def test_whep_symptom_and_fact_state_that_whep_is_unverified():
    article = KB["KB-RING-015"]
    assert not any("/v1/" in s for s in article["symptoms"])
    whep_fact = next(f for f in article["official_facts"] if "WHEP (WebRTC HTTP Egress Protocol)" in f)
    assert "unverified" in whep_fact and "not implemented" in whep_fact


# Ring's release notes document these APIs; the KB says so without claiming the exact paths.
@pytest.mark.parametrize("article_id, endpoint_part, api", [
    ("KB-RING-010", "/events", "Event History API"),
    ("KB-RING-010", "/configurations", "Device Configuration API"),
    ("KB-RING-015", "/whep/", "WHEP"),
])
def test_documented_ring_apis_are_acknowledged_but_paths_stay_unverified(article_id, endpoint_part, api):
    note = mapping(article_id, endpoint_part)["verification"]
    assert note.startswith("unverified")
    assert api in note and "release notes" in note
    assert "not implemented" in note


def test_friction_log_entry_2_matches_the_code():
    log = (ROOT / "FRICTION_LOG.md").read_text(encoding="utf-8")
    entry = log.split("### Entry 2")[1].split("### Entry")[0]
    assert "720" in entry and "75" in entry and "claude-haiku-4-5" in entry
    assert "1280" not in entry and "3.5 Sonnet" not in entry
