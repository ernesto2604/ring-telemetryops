"""
SupportConcierge — Ring TelemetryOps Concierge Agent
=====================================================
Core orchestrator for the autonomous Ring diagnostic agent.
Correlates computer vision frame metrics, transport metrics and device health telemetry
(Ring Partner API status, Digital Twin simulation or caller-supplied data, labelled by `_source`)
with multimodal LLM inference.

Supported provider:
  - Amazon Bedrock Runtime (Anthropic Claude; model from BEDROCK_MODEL_ID)

Agent Output (Strict JSON schema):
  {
    "diagnostic_status"    : "HEALTHY" | "DEGRADED" | "CRITICAL",
    "root_cause_domain"    : "NETWORK" | "OPTICAL_PHYSICAL" | "HARDWARE_POWER" | "ENVIRONMENT_POWER" | "AUDIO_NETWORK" | "ALGORITHM_CONFIGURATION" | "NONE",
    "confidence_score"     : 0.0 – 1.0,
    "telemetry_findings"   : ["anomaly 1", "anomaly 2", ...],
    "visual_findings"      : ["visual observation 1", ...],
    "recommended_mitigation": "actionable resolution step based on the retrieved troubleshooting procedures"
  }
"""

import os
import re
import sys
import json
import logging
import argparse
from pathlib import Path
from typing import Dict, Any, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from src.config import severity
from src.video_pipeline.analyzer import VideoAnalyzer, STREAM_ISSUE_PREFIXES, MIN_SHARPNESS_SCORE
from src.agent.knowledge_retriever import KnowledgeRetriever
from src.utils.telemetry_simulator import TelemetrySimulator
from src.utils.ring_client import RingClient

load_dotenv(ROOT / ".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default Models by Provider
# ---------------------------------------------------------------------------
BEDROCK_DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Customer-facing text contract (D4): recommended_mitigation is the Ring app notification
# ---------------------------------------------------------------------------
CUSTOMER_TEXT_MAX_WORDS = 60
# Terms a customer should not have to know; matched as whole words, case-insensitive.
CUSTOMER_JARGON = (
    "rssi", "dbm", "jitter", "packet loss", "kbps", "mbps", "fps", "bitrate", "webrtc", "whep", "sdp",
    "codec", "laplacian", "ir-cut", "solenoid", "telemetry", "pre-check", "rtt", "vac",
)
CUSTOMER_JARGON_SHOWN = ("RSSI", "dBm", "jitter", "packet loss", "kbps", "FPS", "bitrate", "WebRTC",
                         "solenoid", "IR-cut", "telemetry")
# Electrical work a customer must never be told to do (D3).
ELECTRICAL_DIY = re.compile(
    r"\b(breakers?|multimeter|voltmeter|rewir\w*|replace (?:the |your )?transformer|"
    r"(?:measure|test|check) (?:the |its )?(?:voltage|wiring))\b", re.I)
# Electrical specifications (e.g. "5V/2A charger", "16 VAC", "30 VA"): manufacturer requirements the
# project cannot verify, so they never reach the customer text (D2).
_ELECTRICAL_SPEC = re.compile(r"\b\d+(?:\.\d+)?\s?(?:v|va|vac|a|mah|w)\b", re.I)
# Visual causes the analyzer never detects; suggesting them is speculation the image does not support (N30).
_UNSEEN_CAUSES = re.compile(r"\b(protective (?:shipping )?film|condensation|fog(?:ging)?|sun glare|glare)\b", re.I)
_INTERNAL_FIELD = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_KB_ID = re.compile(r"\bKB-RING-\d+\b", re.I)

# Safe-check policy (C9): the customer may restart the doorbell from the Ring app and, for battery
# problems, check the charging cable or plug adapter; never touch power, wires or the transformer.
SAFETY_SENTENCE = ("Don't open the doorbell or touch any wires; if the problem continues, "
                   "contact a qualified electrician or Ring Support.")
# A clause that forbids something, or refers to an electrician, is not an instruction to the customer.
_NEGATED_CLAUSE = re.compile(r"\b(?:don't|do not|never)\b[^.;]*", re.I)
_REFERRAL_CLAUSE = re.compile(r"\bcontact (?:an? )?(?:qualified )?electrician\b[^.;]*", re.I)
_POWER_WORK = re.compile(
    r"\b(?:turn|switch|shut)(?:ing)? off (?:the |your |its )?(?:power|electricity|mains)\b|"
    r"\b(?:turn|switch|shut)(?:ing)? (?:the |your |its )?(?:power|electricity) off\b|"
    r"\bcut(?:ting)? (?:the |your |its )?(?:power|electricity)\b|\bpower[- ]cycl\w*|"
    r"\bunplug\w*|\bfuses?\b|"
    r"\b(?:check|inspect|verify|make sure|ensure|confirm|test|tighten|reconnect|connect|touch|replace|install|"
    r"upgrade|reset|open|plug|fix|secure|reseat)\b[^.;]{0,60}?\b(?:wires?|wiring|terminals?|connections?|"
    r"transformer|cabling)\b", re.I)
_PLUG_CHECK = re.compile(r"\bplug(?:ged)? (?:it )?in\b|\b(?:firmly|securely|properly) connected\b|"
                         r"\bconnected (?:firmly|securely|properly)\b", re.I)
_CHARGING_OBJECT = re.compile(r"\b(?:charging|charger|usb) cable\b|\b(?:plug|plug-in|charging|power) adapter\b", re.I)

# Visual findings describe image quality only, never who or what is in the scene (C4 privacy).
_SCENE_CONTENT = re.compile(
    r"\b(?:persons?|people|man|men|woman|women|child(?:ren)?|faces?|facial|clothing|clothes|jackets?|vests?|"
    r"caps?|hats?|tablets?|buildings?|bricks?|doors?|vegetation|trees?|bushes|siding|vehicles?|cars?|"
    r"licen[cs]e plates?)\b", re.I)
# "Sharpness ... low" (or the reverse order) within one clause (C10).
_IN_CLAUSE = r"(?:[^.;]|(?<=\d)\.(?=\d))*?"   # text up to the clause end; decimal points allowed
_LOW_SHARPNESS = re.compile(
    r"\bsharpness\b" + _IN_CLAUSE + r"\b(?:low|poor|deficient|insufficient|reduced)\b|"
    r"\b(?:low|poor|deficient|insufficient|reduced)\b" + _IN_CLAUSE + r"\bsharpness\b", re.I)
# "<value> <unit> ... below|above the <limit> <unit> threshold" (C12); clauses split on ";" and on
# sentence dots (not decimal points).
_UNITS = r"(dBm|kbps|ms|fps|%|V)(?![\w])"
_VALUE = re.compile(r"(-?\d+(?:\.\d+)?)\s?" + _UNITS, re.I)
_THRESHOLD_CLAIM = re.compile(
    r"\b(below|under|above|over|exceeding|exceeds)\s+the\s+(-?\d+(?:\.\d+)?)\s?" + _UNITS + _IN_CLAUSE + r"\bthreshold",
    re.I)
_CLAUSE_SPLIT = re.compile(r"(?<!\d)\.(?!\d)|;")


def _electrical_work(text: str, charging_check_allowed: bool) -> bool:
    """True when the text asks the customer to act on power, wiring or the transformer."""
    instructions = _REFERRAL_CLAUSE.sub("", _NEGATED_CLAUSE.sub("", text.replace("’", "'")))
    if ELECTRICAL_DIY.search(text) or _POWER_WORK.search(instructions):
        return True
    safety = SAFETY_SENTENCE.lower() in " ".join(text.replace("’", "'").split()).lower()
    for clause in _CLAUSE_SPLIT.split(instructions):
        if _PLUG_CHECK.search(clause) and not (charging_check_allowed and safety and _CHARGING_OBJECT.search(clause)):
            return True
    return False


def comparison_problems(text: str) -> list[str]:
    """Threshold claims that are numerically false, e.g. '180 kbps ... below the 150 kbps threshold'."""
    problems = []
    for clause in _CLAUSE_SPLIT.split(text):
        start = 0
        for claim in _THRESHOLD_CLAIM.finditer(clause):
            word, limit, unit = claim.group(1).lower(), float(claim.group(2)), claim.group(3).lower()
            below = word in ("below", "under")
            for value in _VALUE.finditer(clause, start, claim.start()):
                v = float(value.group(1))
                if value.group(2).lower() == unit and not (v < limit if below else v > limit):
                    problems.append(f"{value.group(0)} is not {word} the {claim.group(2)} {claim.group(3)} threshold")
            start = claim.end()
    return problems


def customer_text_problems(text: str, charging_check_allowed: bool = True) -> list[str]:
    """
    Reasons why a mitigation is not fit for the customer notification (empty when it is).
    `charging_check_allowed` is False for transformer / wiring cases, where only a Ring app restart
    and a referral to an electrician or Ring Support are allowed.
    """
    problems = []
    words = len(text.split())
    if words > CUSTOMER_TEXT_MAX_WORDS:
        problems.append(f"recommended_mitigation has {words} words (at most {CUSTOMER_TEXT_MAX_WORDS})")
    jargon = [t for t in CUSTOMER_JARGON if re.search(rf"(?<![\w-]){re.escape(t)}(?![\w-])", text, re.I)]
    if jargon:
        problems.append("recommended_mitigation uses technical terms: " + ", ".join(jargon))
    if _KB_ID.search(text):
        problems.append("recommended_mitigation cites a knowledge base article ID")
    if _INTERNAL_FIELD.search(text):
        problems.append("recommended_mitigation contains an internal field name")
    if _UNSEEN_CAUSES.search(text):
        problems.append("recommended_mitigation suggests causes the image does not show (film, condensation, glare)")
    if _ELECTRICAL_SPEC.search(text):
        problems.append("recommended_mitigation quotes an electrical specification (volts, amps, VA)")
    if _electrical_work(text, charging_check_allowed):
        problems.append("recommended_mitigation asks the customer to do electrical work "
                        "(power, wires, connections or the transformer)")
    return problems


def findings_problems(report: Dict[str, Any], video_metrics: Optional[Dict[str, Any]] = None) -> list[str]:
    """
    Technician findings may be technical, but never internal field names, scene content (people,
    objects, places), a measured in-range sharpness called low, or a numerically false threshold claim.
    """
    findings = [item for field in ("telemetry_findings", "visual_findings") for item in report.get(field, [])]
    problems = []
    names = sorted({m for item in findings for m in _INTERNAL_FIELD.findall(item)})
    if names:
        problems.append(f"findings contain internal field names: {', '.join(names)}")
    scene = sorted({m.lower() for item in report.get("visual_findings", []) for m in _SCENE_CONTENT.findall(item)})
    if scene:
        problems.append("visual_findings describe people, objects or places instead of image quality: "
                        + ", ".join(scene))
    sharpness = (video_metrics or {}).get("sharpness_score")
    if (isinstance(sharpness, (int, float)) and sharpness >= MIN_SHARPNESS_SCORE
            and any(_LOW_SHARPNESS.search(item) for item in findings)):
        problems.append(f"findings call the sharpness low although the measured score {sharpness} is in range "
                        f"(>= {MIN_SHARPNESS_SCORE:g}); quote it without interpreting it")
    wrong = [p for item in findings for p in comparison_problems(item)]
    if wrong:
        problems.append("findings compare numbers wrongly: " + "; ".join(wrong))
    return problems


SYSTEM_PROMPT = f"""You are a Principal Telemetry & Support Systems Engineer at Amazon Ring,
specialized in cross-modal diagnostics of smart video doorbells, computer vision stream analysis,
device health telemetry, and network transport telemetry (WebRTC/RTCP).
The case states the source of every telemetry block: only data marked as Ring Partner API comes
from Ring. Digital Twin data is simulated, and client-supplied data is unverified.

Your objective is to SIMULTANEOUSLY evaluate:
1. The attached video keyframe (visual clarity, exposure, sharpness, artifacts, color tints, reflections, or black frames).
2. Quantitative video stream metrics (sharpness score via Laplacian variance, effective FPS, bitrate, mean brightness, issues).
3. Device Health telemetry (RSSI, battery level, transformer voltage, LED status, battery saver flag).
4. Transport metrics in the WebRTC W3C getStats schema (jitter, packet loss, RTT, dropped frames, frame rate).
5. The anomalies found by the deterministic pre-check, each with its severity level, and the precomputed level of every measured metric.
6. Troubleshooting procedures retrieved from the project knowledge base (Ring Help Center-informed; not Ring SOPs).

SEVERITY LEVELS ({severity.PROVENANCE}; the pre-check already applied them):
{severity.table_for_prompt()}
The application sets diagnostic_status from the worst pre-check level; your job is the root cause, the findings and the mitigation.

CRITICAL DIAGNOSTIC RULES:
- PRE-CHECK ANOMALIES: When the case lists at least one pre-check anomaly, diagnostic_status can NOT be HEALTHY. A Weak, Poor or Critical Wi-Fi signal (RSSI) on its own IS an anomaly even when packet loss and jitter are fine: it risks dropouts and late alerts.
- UNAVAILABLE METRICS: A value shown as "unavailable" was NOT measured. Never treat it as zero, nominal, or degraded, and never cite it as evidence. Network metrics can only be called excellent or nominal when they were actually measured. Never return HEALTHY when power or network health is unavailable.
- BATTERY-ONLY INSTALLATION: Only if the power setup is "BATTERY ONLY", the device operates on battery by customer design. An unconnected transformer is NOT a failure. If the battery is GREEN in the severity levels and the network metrics are GREEN, the power subsystem is HEALTHY.
- UNKNOWN POWER SETUP: If the power setup is not given and the transformer voltage is unavailable, the power setup is UNKNOWN. Do NOT assume a battery-only or a wired installation.
- BACKUP BATTERY: A wired doorbell with a charged internal battery and a transformer at 0 V is consistent: the battery is a backup that keeps the device running. Never call this inconsistent and never suggest stale telemetry for that reason.
- STANDBY / LOW-POWER MODE: If the camera is inactive (camera state STANDBY), the optical sensor is shut down by low-power design. There is NO video frame or active stream. In "visual_findings" you MUST output EXCLUSIVELY: ["Camera in low-power standby mode. Optical sensor powered down by design."]. NEVER invent or hallucinate visual scenes, people, walls, bricks, or optical features during Standby. Root cause and remediation must be 100% telemetry-driven (Wi-Fi, WebRTC, battery).
- TELEMETRY-ONLY (NO FRAME): If the case says no video frame is available, the camera state is NOT known to be standby. Do not describe or infer any visual or optical condition; in "visual_findings" output only the stated reason. Root cause and remediation must be telemetry-driven.
- OPTICAL VS NETWORK DISAMBIGUATION: If video is blurry, exhibits pink/purple color tint, IR glare/halo, or 50Hz/60Hz flickering horizontal bands, BUT every network metric is GREEN in the severity levels → assign OPTICAL_PHYSICAL root cause.
- NETWORK DEGRADATION: If video shows macroblocking, pixelation, stuttering, OR any network metric (RSSI, packet loss, jitter, frame rate, bitrate) is DEGRADED or CRITICAL → assign NETWORK root cause.
- POWER SUBSYSTEM: If the battery or the transformer is DEGRADED or CRITICAL in the severity levels, OR the battery saver flag is true → assign HARDWARE_POWER or ENVIRONMENT_POWER.
- MECHANICAL IN-HOME CHIME: The mechanical chime level applies only if the in-home chime type is "mechanical" on a wired setup → assign DEGRADED with HARDWARE_POWER. It is a project diagnostic rule for mechanical chimes, never a requirement of the doorbell itself (for example not of the Video Doorbell 4).
- AUDIO DEGRADATION: If two-way audio distortion or choppy audio correlates with high jitter → assign AUDIO_NETWORK.
- FIELD OF VIEW / CONFIGURATION: If issue involves mounting height, false motion triggers, or PIR angle → assign ALGORITHM_CONFIGURATION.
- NOMINAL: If all telemetry and optical metrics are GREEN → assign HEALTHY with root_cause_domain="NONE".
- THRESHOLDS YOU QUOTE: When you say how far a metric must improve, quote ONLY its GREEN boundary given in the case (for example, a Poor RSSI must reach "at or above -60 dBm" to be GREEN; "-75 dBm" only leaves the Poor band and is still Weak). Never invent other limits.
- NUMBERS: Never compare numbers yourself. Use the level the case gives for each metric under METRIC LEVELS, and never write that a value is below or above a threshold unless that is true.
- SHARPNESS: A sharpness score marked "in range" is fine: quote it if useful, but never call it low, poor or a cause of the problem.
- POWER CUT WITH A BLACK FRAME: When the case says the black frame is explained by a transformer power cut, the black image is a symptom of the power loss, not a separate optical problem: give a power root cause and no optical mitigation.
- MANUFACTURER REQUIREMENTS: Do not state Ring requirements (voltages, VA ratings, device models, accessories) unless they appear in a retrieved knowledge base article, and then call them "reference values (unverified)".
- ELECTRICAL SAFETY: Never tell the customer to do electrical work: no switching or resetting circuit breakers, no multimeter or voltage measurements, no rewiring, no replacing the transformer. To restart, say: restart your doorbell following the steps in the Ring app (never turn off, cut or unplug the power, and no timed power cycles). For anything about the doorbell's wiring or power supply, tell them to contact a qualified electrician or Ring Support; never ask the customer to check or touch the wiring, the wires, the terminals, the connections, the fuses or the transformer (a wired transformer is part of the home's electrical installation, it is not plugged in). For transformer, wiring or in-home chime power problems the only actions are a Ring app restart and that referral. Only for battery or charging problems may you ask them to make sure the charging cable or plug adapter is firmly connected at both ends, and then add: "{SAFETY_SENTENCE}"
- VISUAL EVIDENCE: Recommend only what the image and metrics support. For a blurry lens do not mention condensation, protective film, obstructions or sun glare unless the image clearly shows them; do not list them as possibilities either.
- STANDBY IS NORMAL: Camera standby is the doorbell's normal idle state. Never tell the customer the device entered standby because of a problem.
- NO KNOWLEDGE BASE MATCH: If no troubleshooting procedure was retrieved, give a conservative, generic mitigation (check the device in the Ring app, contact Ring Support) and do not invent a specific procedure.
- EVIDENCE: Only telemetry labelled ring_partner_api comes from Ring. The numeric rules above are project diagnostic heuristics, not Ring requirements. The troubleshooting procedures are this project's knowledge base: content with evidence "unverified" or a pending verification is not confirmed, so never cite them as official Ring documentation and never state that a Ring API endpoint or capability exists because a procedure mentions it.

WHO READS EACH FIELD:
- "recommended_mitigation" is the notification the customer reads in the Ring app. Write plain, everyday English, at most {CUSTOMER_TEXT_MAX_WORDS - 10} words, in this order: what is happening, why it matters, what to do. Use no technical terms (no {', '.join(CUSTOMER_JARGON_SHOWN)}), no knowledge base article IDs, no internal field names, no electrical specifications (volts, amps, VA, charger ratings), and numbers only when they help the customer. Settings that live in the Ring app are written as "in the Ring app". When a retrieved article has a customer message, start from the message that matches the problem and adapt it to the evidence.
- "telemetry_findings" and "visual_findings" are for a support technician: one short factual sentence per anomaly (technical terms allowed), but no internal field names (write "battery saver flag", not the field name).
- "visual_findings" describe image quality only: exposure, sharpness, color, artifacts, banding, blur, black frames. Never describe people, faces, clothing, objects, license plates, vehicles, buildings, doors, walls or vegetation, even when they are visible.

RESPOND ONLY with a valid, parseable JSON object in English, without markdown wrapping, without explanations outside JSON:
{{
  "diagnostic_status": "HEALTHY|DEGRADED|CRITICAL",
  "root_cause_domain": "NETWORK|OPTICAL_PHYSICAL|HARDWARE_POWER|ENVIRONMENT_POWER|AUDIO_NETWORK|ALGORITHM_CONFIGURATION|NONE",
  "confidence_score": 0.0,
  "telemetry_findings": ["One short technical sentence per telemetry anomaly"],
  "visual_findings": ["One short sentence per visual observation"],
  "recommended_mitigation": "Plain-language message for the customer: what is happening, why it matters, what to do"
}}"""

STANDBY_VISUAL_FINDING = "Camera in low-power standby mode. Optical sensor powered down by design."

# Severity levels come from the single table in src/config/severity_thresholds.json (project
# heuristics, not Ring specifications); the pre-check, the local fallback and the prompt all read it.
TRANSFORMER_MIN_VAC = severity.limit("transformer", "Undervoltage")
MECHANICAL_CHIME_MIN_VAC = severity.limit("mechanical_chime", "Too low for the mechanical chime")


_SOURCE_DESCRIPTIONS = {
    "ring_partner_api": "ring_partner_api (Ring Partner API device status; it reports no RSSI, power wiring or WebRTC stats)",
    "mock": "mock (Digital Twin simulator: SIMULATED values, not from a Ring device)",
    "client_supplied": "client_supplied (sent by the caller; not verified by Ring)",
}


def _source_description(source: Optional[str]) -> str:
    """Prompt label for a telemetry `_source`; unknown labels are never presented as Ring data."""
    if source in _SOURCE_DESCRIPTIONS:
        return _SOURCE_DESCRIPTIONS[source]
    return f"{source or 'not stated'} (unverified; not Ring data)"


def _scaled(value: Any, factor: float) -> Optional[float]:
    """Scales a measured value; an unmeasured (None) value stays None, never 0."""
    return None if value is None else value * factor


def _measurement_reason(telemetry: Dict[str, Any], section: str, field: str) -> Optional[str]:
    """Why a value is missing, from `measurement_status` (per-field dict or per-section string)."""
    status = (telemetry.get("measurement_status") or {}).get(section)
    if isinstance(status, dict):
        return status.get(field)
    return status if isinstance(status, str) else None


def _telemetry_metrics(
    telemetry: Dict[str, Any],
    video_metrics: Dict[str, Any],
    camera_active: bool,
) -> Dict[str, Any]:
    """Reads the diagnostic metrics; anything not measured is None (a real 0 stays 0)."""
    dh = telemetry.get("device_health") or {}
    ws = telemetry.get("webrtc_stats") or {}
    rtp = ws.get("inbound_rtp") or {}
    cp = ws.get("candidate_pair") or {}
    fps = rtp.get("frames_per_second")
    bitrate = rtp.get("bitrate_kbps")
    # Standby video metrics are placeholders (0 FPS / 0 kbps), not measurements.
    if camera_active:
        fps = video_metrics.get("fps") if fps is None else fps
        bitrate = video_metrics.get("bitrate_kbps") if bitrate is None else bitrate
    return {
        "online": dh.get("online"),
        "power_mode": dh.get("power_mode"),
        "rssi": dh.get("rssi"),
        "battery": dh.get("battery_percentage"),
        "transformer": dh.get("transformer_voltage_vac"),
        "chime_type": dh.get("chime_type_setting"),
        "jitter_ms": _scaled(rtp.get("jitter"), 1000.0),
        "packet_loss_pct": _scaled(rtp.get("packet_loss_fraction"), 100.0),
        "rtt_ms": _scaled(cp.get("current_round_trip_time"), 1000.0),
        "fps": fps,
        "bitrate": bitrate,
    }


def _optical_issues(video_metrics: Dict[str, Any]) -> list[str]:
    """Analyzer issues about the image itself (frame rate / bitrate issues are judged as stream metrics)."""
    return [i for i in video_metrics.get("issues", []) if not i.startswith(STREAM_ISSUE_PREFIXES)]


def _assess(m: Dict[str, Any], video_metrics: Dict[str, Any], camera_active: bool) -> Dict[str, Any]:
    """
    Deterministic severity: every non-GREEN metric of the severity table, plus the analyzer's
    optical verdict when a frame was analyzed. The overall level is the worst one, and the
    dominant anomaly is the first with that level (table order: power, network, stream, then optics).
    Unmeasured metrics never count.
    """
    found = severity.anomalies(severity.classify_telemetry(m))
    for item in found:
        item["finding"] = _finding(item)
    if camera_active:
        if video_metrics.get("status") == "CRITICAL":
            found.append({"metric": "video", "level": severity.CRITICAL, "domain": "OPTICAL_PHYSICAL",
                          "reason": "No usable optical signal",
                          "finding": "The analyzed frame has no usable optical signal."})
        elif _optical_issues(video_metrics):
            found.append({"metric": "video", "level": severity.DEGRADED, "domain": "OPTICAL_PHYSICAL",
                          "reason": "Visual quality issue detected",
                          "finding": "The video analyzer detected visual quality issues."})
        elif video_metrics.get("optical_banding_detected", False):
            found.append({"metric": "video", "level": severity.DEGRADED, "domain": "OPTICAL_PHYSICAL",
                          "reason": "Degraded optical metrics",
                          "finding": "OpenCV detected degraded optical clarity or banding."})
    level = severity.worst_level([item["level"] for item in found])
    dominant = next((item for item in found if item["level"] == level), None)
    return {"level": level, "anomalies": found, "dominant": dominant}


def _finding(item: Dict[str, Any]) -> str:
    """Technician-facing finding for one table anomaly (no internal field names)."""
    rule = severity.METRICS[item["metric"]]
    unit = rule["unit"]
    sep = "" if unit == "%" else " "
    op = severity.OP_SYMBOLS[item["op"]]
    return (f"{rule['label']} is {item['value']:g}{sep}{unit} ({item['label']}: {op} {item['limit']:g}{sep}{unit}, "
            f"{item['level']}; project heuristic).")


def _missing_essential_metrics(m: Dict[str, Any]) -> list[str]:
    """
    Metrics needed before a device can be called healthy: some measured network
    signal (RSSI, or jitter plus packet loss) and some measured power signal.
    """
    missing: list[str] = []
    if m["rssi"] is None and (m["jitter_ms"] is None or m["packet_loss_pct"] is None):
        missing += ["rssi", "jitter", "packet_loss_fraction"]
    if m["battery"] is None and m["transformer"] is None:
        missing += ["battery_percentage", "transformer_voltage_vac"]
    return missing


# Values SYSTEM_PROMPT allows the model to return (the local reports may also use UNKNOWN /
# INSUFFICIENT_DATA, which the model is not asked to produce).
LLM_STATUSES = {"HEALTHY", "DEGRADED", "CRITICAL"}
LLM_DOMAINS = {
    "NETWORK", "OPTICAL_PHYSICAL", "HARDWARE_POWER", "ENVIRONMENT_POWER",
    "AUDIO_NETWORK", "ALGORITHM_CONFIGURATION", "NONE",
}


def _validate_llm_diagnostic(report: Dict[str, Any]) -> None:
    """Raises ValueError unless the model output matches the diagnostic contract (types, enums, ranges)."""
    if report["diagnostic_status"] not in LLM_STATUSES:
        raise ValueError("diagnostic_status is not an allowed value")
    if report["root_cause_domain"] not in LLM_DOMAINS:
        raise ValueError("root_cause_domain is not an allowed value")
    score = report["confidence_score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 <= score <= 1.0:
        raise ValueError("confidence_score must be a number in [0, 1]")
    for field in ("telemetry_findings", "visual_findings"):
        items = report[field]
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError(f"{field} must be a list of strings")
    mitigation = report["recommended_mitigation"]
    if not isinstance(mitigation, str) or not mitigation.strip():
        raise ValueError("recommended_mitigation must be a non-empty string")


def _offline_diagnostic(telemetry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministic report for telemetry with online is False. Ring's status is the last reported
    reading, not a live one, so a single read is reported as such (moderate confidence, read time).
    """
    reporter = "Ring Partner API" if telemetry.get("_source") == "ring_partner_api" else "The telemetry"
    read_at = telemetry.get("captured_at")
    when = f"in the status read taken at {read_at}" if read_at else "in the last status read"
    return {
        "diagnostic_status": "CRITICAL",
        "root_cause_domain": "UNKNOWN",
        "confidence_score": 0.6,
        "telemetry_findings": [
            f"{reporter} reported the device offline {when}.",
            "One status read is the device's last reported state; it does not confirm a lasting outage "
            "or its cause (power or Wi-Fi).",
        ],
        "visual_findings": ["No live video: the device is offline."],
        "recommended_mitigation": (
            "Check the device's power source and Wi-Fi connection, then confirm it reconnects in the Ring app."
        ),
    }


def _insufficient_data_diagnostic(missing: list[str], camera_active: bool) -> Dict[str, Any]:
    """Report for a device with no detected anomaly but without the data to call it healthy."""
    return {
        "diagnostic_status": "INSUFFICIENT_DATA",
        "root_cause_domain": "UNKNOWN",
        "confidence_score": 0.0,
        "telemetry_findings": [
            "No anomaly was detected in the available metrics.",
            "Not measured, so health cannot be confirmed: " + ", ".join(missing) + ".",
        ],
        "visual_findings": [STANDBY_VISUAL_FINDING] if not camera_active else [],
        "recommended_mitigation": (
            "Collect the missing metrics (for example from the Ring app's Device Health page or a live view session) "
            "before concluding the device is healthy."
        ),
    }


# Fixed confidence of every local_fallback report (a mock-up constant, not a computed estimate).
FALLBACK_CONFIDENCE = 0.86
# The pre-check's nominal verdict is deterministic: every measured metric is in the GREEN band.
PRECHECK_NOMINAL_CONFIDENCE = 1.0

NOMINAL_MITIGATION = "No action needed: your doorbell is working normally."

# KB issue categories that can supply the mitigation for each report domain.
_DOMAIN_KB_CATEGORIES = {
    "HARDWARE_POWER": ("power", "chime_hardware"),
    "NETWORK": ("network",),
    "OPTICAL_PHYSICAL": ("video", "video_optics"),
}

# Plain-language mitigations used only when no retrieved KB article matches (marked as generated).
_GENERIC_MITIGATIONS = {
    "HARDWARE_POWER": (
        "Your doorbell is not getting the power it needs, so it may restart or miss alerts. "
        "Please contact a qualified electrician or Ring Support to check its power supply."
    ),
    "NETWORK": (
        "Your doorbell's internet connection is weak or unstable, so video may freeze or alerts may be late. "
        "Restart your router and move it closer to the doorbell if you can, then check the connection in the Ring app."
    ),
    "OPTICAL_PHYSICAL": (
        "Your doorbell camera's picture doesn't look right, which can hide details in your videos. "
        "Gently clean the lens with a soft, dry cloth, then check the view in the Ring app."
    ),
}
MITIGATION_SOURCE_KB = "knowledge_base"
MITIGATION_SOURCE_GENERATED = "generated_no_kb_match"
# T15: Bedrock's report kept, its customer text replaced by the deterministic message.
MITIGATION_SOURCE_KB_DETERMINISTIC = "knowledge_base_deterministic"
MITIGATION_SOURCE_GENERIC_DETERMINISTIC = "generic_deterministic"

# Readable names for the prompt, so the model never sees (and copies) internal field names (C13).
_READABLE_METRICS = {
    "rssi": "Wi-Fi signal (RSSI)", "jitter": "jitter", "packet_loss_fraction": "packet loss",
    "battery_percentage": "battery level", "transformer_voltage_vac": "transformer voltage",
}

# KB actions the model must not relay (C5, C8): electrical work, and causes the analyzer cannot
# observe unless the analyzer issues mention them. "Power cycle" becomes a Ring app restart (C9).
_KB_ELECTRICAL_ACTION = re.compile(
    r"\b(?:transformer|wiring|wires?|terminals?|pro power kit|jumper|voltage|va rating|power requirements|"
    r"power supply|breakers?|multimeter|poe|ethernet|injector)\b", re.I)
_KB_UNSEEN_ACTION = re.compile(r"\b(?:film|condensation|fog\w*|scratch\w*|glare|reflect\w*|halo)\b", re.I)
_KB_POWER_CYCLE = re.compile(r"\bpower cycle (?:the )?(?:ring )?(?:device|doorbell|camera)(?: or camera)?", re.I)
_KB_CHARGER_SPEC = re.compile(r"\s+with an? [^.,;]*?\d[^.,;]*?charger\b", re.I)
RING_APP_RESTART = "Restart the doorbell following the steps in the Ring app"


def _kb_action_for_prompt(action: str, issues_text: str) -> Optional[str]:
    """The KB action as the model may relay it, or None when it must not reach the model."""
    action = _KB_POWER_CYCLE.sub(RING_APP_RESTART, _KB_CHARGER_SPEC.sub("", action))
    if _KB_ELECTRICAL_ACTION.search(action) or _ELECTRICAL_SPEC.search(action):
        return None
    unseen = _KB_UNSEEN_ACTION.findall(action)
    if unseen and not all(word.lower() in issues_text for word in unseen):
        return None
    return action


def _kb_for_prompt(articles: list[Dict[str, Any]], video_metrics: Dict[str, Any]) -> str:
    """
    Retrieved articles as plain text: actions and facts filtered like the customer text (no electrical
    figures or work, no unseen causes), customer messages, and no internal field names.
    """
    issues_text = " ".join(video_metrics.get("issues", [])).lower()
    blocks = []
    for article in articles:
        actions = [a for a in (_kb_action_for_prompt(x, issues_text) for x in article.get("recommended_actions", []))
                   if a]
        facts = [f for f in (_kb_action_for_prompt(x, issues_text) for x in article.get("official_facts", []))
                 if f and not _INTERNAL_FIELD.search(f)]
        messages = article.get("customer_messages") or {}
        lines = [f"[{article.get('id')}] {article.get('title')} (evidence: {article.get('evidence_level', 'unverified')})"]
        lines += [f"  Symptom: {s}" for s in article.get("symptoms", [])]
        lines += [f"  Fact: {f}" for f in facts]
        lines += [f"  Action: {a}" for a in actions]
        lines += [f"  Customer message ({key.replace('_', ' ')}): {text}" for key, text in messages.items()]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _sharpness_line(score: Any) -> str:
    """The sharpness score with the analyzer's verdict, so the model never re-interprets it (C10)."""
    if not isinstance(score, (int, float)):
        return str(score)
    if score >= MIN_SHARPNESS_SCORE:
        return f"{score} (in range, blur limit {MIN_SHARPNESS_SCORE:g}: quote it, do not interpret it)"
    return f"{score} (below the analyzer blur limit of {MIN_SHARPNESS_SCORE:g})"


def _black_frame_from_power_cut(anomalies: list[Dict[str, Any]]) -> bool:
    """A transformer power cut plus a frame with no optical signal: the black frame is a symptom (T14)."""
    return (any(a["metric"] == "transformer" and a["level"] == severity.CRITICAL for a in anomalies)
            and any(a["metric"] == "video" and a["level"] == severity.CRITICAL for a in anomalies))


def _metric_levels_section(m: Dict[str, Any], video_metrics: Dict[str, Any]) -> str:
    """Every measured metric with its precomputed level and its GREEN boundary (C3, C12)."""
    context = {"power_mode": m["power_mode"], "chime_type": m["chime_type"]}
    items = [("", item) for item in severity.classify_telemetry(m)]
    for name, key in (("fps", "fps"), ("bitrate", "bitrate_kbps")):
        value = video_metrics.get(key)
        item = severity.classify_metric(name, value, context) if value != m[name] else None
        if item:
            items.append((" (video analyzer)", item))
    lines = []
    for origin, item in items:
        rule = severity.METRICS[item["metric"]]
        level = item["level"] if item["level"] == severity.GREEN else (
            f"{item['level']} ({item['label']}, {severity.OP_SYMBOLS[item['op']]} {item['limit']:g} {rule['unit']})")
        lines.append(f"- {rule['label']}{origin}: {item['value']:g} {rule['unit']} = {level}; "
                     f"GREEN when {severity.green_target(item['metric'], context)}")
    return "\n".join(lines) or "- No metric was measured."


def _fallback_mitigation(dominant: Dict[str, Any], articles: list[Dict[str, Any]]) -> tuple[str, str]:
    """
    Plain-language mitigation for the dominant anomaly: the customer message of the first retrieved
    KB article in the same domain (per metric when the article has one), else a generic message.
    """
    categories = _DOMAIN_KB_CATEGORIES.get(dominant["domain"], ())
    for article in articles:
        messages = article.get("customer_messages") or {}
        if article.get("issue_category") in categories and messages:
            text = messages.get(dominant["metric"]) or messages.get("default")
            if text:
                return text, MITIGATION_SOURCE_KB
    return _GENERIC_MITIGATIONS.get(dominant["domain"], _GENERIC_MITIGATIONS["NETWORK"]), MITIGATION_SOURCE_GENERATED


def _nominal_diagnostic(camera_active: bool) -> Dict[str, Any]:
    """Explicit pre-check verdict when every measured metric is GREEN (no Bedrock call)."""
    return {
        "diagnostic_status": "HEALTHY",
        "root_cause_domain": "NONE",
        "confidence_score": PRECHECK_NOMINAL_CONFIDENCE,
        "telemetry_findings": ["All measured device and transport metrics are within the GREEN band."],
        "visual_findings": (["No optical issue detected in the analyzed frame."] if camera_active
                            else [STANDBY_VISUAL_FINDING]),
        "recommended_mitigation": NOMINAL_MITIGATION,
    }


def bedrock_credentials_configured() -> bool:
    """
    True when boto3 can resolve AWS credentials (explicit keys, profile, SSO cache, ...). No AWS
    request is made and the EC2 instance-metadata lookup is skipped, so this answers instantly.
    """
    if os.getenv("AWS_ACCESS_KEY_ID", "").strip() and os.getenv("AWS_SECRET_ACCESS_KEY", "").strip():
        return True
    try:
        import botocore.session
        resolver = botocore.session.get_session().get_component("credential_provider")
        resolver.remove("iam-role")
        return resolver.load_credentials() is not None
    except Exception:
        return False


class SupportConcierge:
    """
    Diagnostic Orchestrator for Ring TelemetryOps.

    Args:
        kb_path:    Path to the Ring troubleshooting JSON knowledge base.
        model_id:   Model identifier override. If None, uses provider default.
    """

    def __init__(
        self,
        kb_path: str = "data/knowledge_base/ring_video_troubleshooting.json",
        model_id: Optional[str] = None,
    ):
        self.retriever = KnowledgeRetriever(kb_path=kb_path)
        self._client   = None
        self.last_bedrock_attempts = 0
        self.model_id = (
            model_id or os.getenv("BEDROCK_MODEL_ID", BEDROCK_DEFAULT_MODEL)
        ).strip()

    @staticmethod
    def default_model_id() -> str:
        return os.getenv("BEDROCK_MODEL_ID", BEDROCK_DEFAULT_MODEL).strip()

    def _get_client(self):
        """Initializes the LLM client on demand based on selected provider."""
        if self._client is not None:
            return self._client

        import boto3
        kwargs: Dict[str, Any] = {
            "service_name": "bedrock-runtime",
            "region_name": (os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1").strip(),
        }
        key_id = os.getenv("AWS_ACCESS_KEY_ID", "").strip()
        secret = os.getenv("AWS_SECRET_ACCESS_KEY", "").strip()
        if key_id and secret:
            kwargs.update(aws_access_key_id=key_id, aws_secret_access_key=secret)
            token = os.getenv("AWS_SESSION_TOKEN", "").strip()
            if token:
                kwargs["aws_session_token"] = token
        # Otherwise boto3 resolves its standard chain (profiles, SSO, roles, temporary credentials);
        # empty strings would override that chain with invalid credentials.
        self._client = boto3.client(**kwargs)

        return self._client

    def _call_llm(self, user_prompt: str, image_base64: Optional[str]) -> str:
        """
        Invokes LLM with textual prompt and optional Base64 JPEG frame.
        Returns raw text response.
        """
        client = self._get_client()

        return self._call_bedrock(client, user_prompt, image_base64)

    def _call_bedrock(self, client, user_prompt: str, image_base64: Optional[str]) -> str:
        """Invokes Amazon Bedrock Runtime with Anthropic Claude Messages API."""
        content = []

        if image_base64:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_base64,
                },
            })

        content.append({"type": "text", "text": user_prompt})

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "system": SYSTEM_PROMPT,
            # One finding per anomaly can be long (6+ anomalies): 512 tokens truncated the JSON in live runs.
            "max_tokens": 1024,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": content}],
        })

        try:
            response = client.invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            response_body = json.loads(response["body"].read().decode("utf-8"))
            return response_body["content"][0]["text"]
        except Exception as e:
            err_str = str(e)
            if "Model use case details have not been submitted" in err_str:
                raise RuntimeError(
                    "AWS Bedrock requires Anthropic Use Case submission for your AWS Account: "
                    "Log into AWS Console -> Amazon Bedrock -> 'Model access' -> 'Anthropic' -> 'Submit Use Case Details'. "
                    "Verify that the Anthropic model is enabled for your AWS account."
                ) from e
            elif "AccessDeniedException" in err_str:
                raise RuntimeError(
                    "AWS Bedrock AccessDeniedException. Ensure your AWS credentials in .env "
                    "have 'bedrock:InvokeModel' IAM permissions."
                ) from e
            raise RuntimeError(f"Error invoking AWS Bedrock ({self.model_id}): {err_str}") from e

    def _parse_response(self, raw_text: str) -> Dict[str, Any]:
        """
        Defensively parses LLM JSON output, stripping markdown formatting if present.
        """
        text = raw_text.strip()

        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            ).strip()

        try:
            parsed = json.loads(text)
            required = {
                "diagnostic_status", "root_cause_domain", "confidence_score",
                "telemetry_findings", "visual_findings", "recommended_mitigation",
            }
            if not isinstance(parsed, dict):
                raise ValueError("Response is not a JSON object")
            missing = required - set(parsed.keys())
            if missing:
                raise ValueError(f"Missing required fields in response: {missing}")
            _validate_llm_diagnostic(parsed)
            return parsed

        except (json.JSONDecodeError, ValueError) as e:
            return {
                "diagnostic_status": "DEGRADED",
                "root_cause_domain": "NONE",
                "confidence_score": 0.0,
                "telemetry_findings": ["Error parsing LLM response"],
                "visual_findings":    ["Not available"],
                "recommended_mitigation": f"[Unparsed raw output] {raw_text[:500]}",
                "_parse_error": str(e),
            }

    def _contract_problems(self, report: Dict[str, Any], anomaly: Dict[str, Any],
                           video_metrics: Optional[Dict[str, Any]] = None) -> list[str]:
        """Why a parsed model report breaks the contract (schema, severity, customer text, findings)."""
        if "_parse_error" in report:
            return ["the answer was not a JSON object matching the diagnostic schema "
                    f"({report['_parse_error']})"]
        problems = []
        if report.get("diagnostic_status") == "HEALTHY":
            problems.append("diagnostic_status was HEALTHY although the pre-check found: " + anomaly["reason"])
        # Transformer / wiring cases: no plug checks at all, only a Ring app restart and a referral.
        wired_power = any(a["metric"] in ("transformer", "mechanical_chime") for a in anomaly.get("anomalies", []))
        problems += customer_text_problems(report["recommended_mitigation"], charging_check_allowed=not wired_power)
        problems += findings_problems(report, video_metrics)
        return problems

    def _reason_with_bedrock(self, user_prompt: str, image_base64: Optional[str],
                             anomaly: Dict[str, Any],
                             video_metrics: Optional[Dict[str, Any]] = None) -> tuple[Dict[str, Any], int]:
        """
        One Bedrock answer, plus one corrective retry when it breaks the contract (HEALTHY despite a
        pre-check anomaly, invalid schema, findings or customer text that break the rules). When the
        corrected answer breaks only the customer text, it is kept and marked `_mitigation_rejected`
        so the caller swaps in the deterministic KB message (T15). Any other second violation, or a
        call error, raises RuntimeError so the caller uses the local fallback. Returns the report and
        the number of Bedrock calls made.
        """
        self.last_bedrock_attempts = 1
        raw = self._call_llm(user_prompt, image_base64)
        report = self._parse_response(raw)
        problems = self._contract_problems(report, anomaly, video_metrics)
        if not problems:
            return report, 1
        print(f"      Bedrock answer rejected ({'; '.join(problems)}); asking once for a corrected answer.")
        correction = (
            "\n\n=== YOUR PREVIOUS ANSWER WAS REJECTED ===\n"
            + "\n".join(f"- {p}" for p in problems)
            + f"\nPrevious answer: {raw[:1500]}\n"
            "Return the corrected JSON object only, following every rule above. The pre-check found "
            f"{anomaly['level']} anomalies, so diagnostic_status must be DEGRADED or CRITICAL."
        )
        self.last_bedrock_attempts = 2
        raw = self._call_llm(user_prompt + correction, image_base64)
        report = self._parse_response(raw)
        problems = self._contract_problems(report, anomaly, video_metrics)
        if problems and all(p.startswith("recommended_mitigation") for p in problems):
            print(f"      Bedrock customer text rejected twice ({'; '.join(problems)}); "
                  "keeping the rest of the answer with the deterministic KB message.")
            report["_mitigation_rejected"] = problems
            return report, 2
        if problems:
            raise RuntimeError("Bedrock broke the diagnostic contract twice: " + "; ".join(problems))
        return report, 2

    def _build_local_diagnostic(
        self,
        video_metrics: Dict[str, Any],
        telemetry: Dict[str, Any],
        articles: list[Dict[str, Any]],
        camera_active: bool,
    ) -> Dict[str, Any]:
        """
        Deterministic report when Bedrock is unavailable or breaks the contract: a mock-up of the
        report contract, not a reasoning engine. Same schema; severity and domain come from the
        severity table; every detected anomaly is listed; the mitigation is the plain-language
        message of the matching KB article (or a generic one when no article matches).
        """
        m = _telemetry_metrics(telemetry, video_metrics, camera_active)
        if m["online"] is False:
            return _offline_diagnostic(telemetry)
        assessment = _assess(m, video_metrics, camera_active)
        if assessment["dominant"] is None:
            missing = _missing_essential_metrics(m)
            if missing:
                report = _insufficient_data_diagnostic(missing, camera_active)
                report["visual_findings"] = [STANDBY_VISUAL_FINDING] if not camera_active else []
                return report
            return _nominal_diagnostic(camera_active)

        dominant = assessment["dominant"]
        if camera_active:
            # Every analyzer issue, stream ones included (e.g. a low file bitrate the telemetry does not show).
            visual_findings = list(video_metrics.get("issues", []))
        else:
            visual_findings = [STANDBY_VISUAL_FINDING]
        mitigation, _source = _fallback_mitigation(dominant, articles)
        return {
            "diagnostic_status": assessment["level"],
            "root_cause_domain": dominant["domain"],
            "confidence_score": FALLBACK_CONFIDENCE,
            "telemetry_findings": [a["finding"] for a in assessment["anomalies"] if a["metric"] != "video"]
                                  or ["Device and transport telemetry are within the GREEN band."],
            "visual_findings": visual_findings,
            "recommended_mitigation": mitigation,
        }

    def _precheck_anomaly(
        self,
        video_metrics: Dict[str, Any],
        telemetry: Dict[str, Any],
        camera_active: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        Detects anomalies before spending a Bedrock request. Unmeasured metrics never trigger one.
        Returns the dominant anomaly's domain and reason, the deterministic level (worst metric) and
        every anomaly found, or None when everything measured is GREEN.
        """
        m = _telemetry_metrics(telemetry, video_metrics, camera_active)
        if m["online"] is False:
            return {"domain": "UNKNOWN", "reason": "Device reported offline", "level": severity.CRITICAL,
                    "anomalies": []}
        # Blur is judged by the analyzer (it reports it in "issues"); no second raw sharpness threshold here.
        assessment = _assess(m, video_metrics, camera_active)
        dominant = assessment["dominant"]
        if dominant is None:
            return None
        return {"domain": dominant["domain"], "reason": dominant["reason"], "level": assessment["level"],
                "anomalies": assessment["anomalies"]}

    def process_stream(
        self,
        video_path: Optional[str] = None,
        telemetry: Optional[Dict[str, Any]] = None,
        keyframe_second: float = 1.0,
        frame_base64: Optional[str] = None,
        camera_active: bool = True,
        no_frame_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes end-to-end multimodal diagnostic pipeline.
        `no_frame_reason`: the camera may be active but no frame can be obtained;
        runs a telemetry-only diagnosis that states this reason (never "standby").
        Steps:
        1. Quantitative video telemetry extraction (OpenCV or live Canvas frame).
        2. Base64 keyframe extraction for multimodal reasoning.
        3. Context grounding against official Ring SOPs (RAG).
        4. Multimodal LLM synthesis with Amazon Bedrock.
        5. Defensive JSON schema parsing and diagnostic report formatting.
        """
        telemetry = telemetry or {}
        scenario = telemetry.get("scenario", "unknown")
        video_available = camera_active and not no_frame_reason
        if no_frame_reason:
            no_video_finding: Optional[str] = no_frame_reason
        else:
            no_video_finding = STANDBY_VISUAL_FINDING if not camera_active else None

        def finish(result: Dict[str, Any]) -> Dict[str, Any]:
            if no_frame_reason:
                result["video_analysis"] = {"available": False, "reason": no_frame_reason}
            return result

        if no_frame_reason:
            print(f"\n[1/4] No video frame available; telemetry-only diagnosis ({no_frame_reason})")
            print("[2/4] No video frame available: no keyframe to extract")
            video_metrics = {
                "file": None,
                "resolution": None,
                "fps": None,
                "bitrate_kbps": None,
                "sharpness_score": None,
                "status": "UNAVAILABLE",
                "issues": [],
                "evidence_frame": None,
            }
            image_base64 = None
        elif not camera_active:
            print("\n[1/4] Device in Standby (Camera inactive / optical sensor off by design)")
            print("[2/4] Camera in standby: no frame to extract")
            video_metrics = {
                "file": "standby_low_power_mode",
                "resolution": "Camera in standby mode",
                "fps": 0.0,
                "bitrate_kbps": 0.0,
                "sharpness_score": 0.0,
                "status": "STANDBY",
                "issues": [],
                "evidence_frame": None,
            }
            image_base64 = None
        elif frame_base64:
            print(f"\n[1/4] Analyzing live WebRTC Canvas keyframe (scenario: {scenario})...")
            video_metrics, image_base64 = VideoAnalyzer.analyze_frame_base64(
                frame_base64=frame_base64,
                telemetry=telemetry,
                max_width=720,
                jpeg_quality=75,
            )
            print(f"      Stream status: {video_metrics['status']} | "
                  f"Sharpness: {video_metrics['sharpness_score']} | "
                  f"Brightness: {video_metrics.get('mean_brightness')} | "
                  f"Issues: {video_metrics.get('issues', [])}")
            print("[2/4] Live keyframe re-encoded for the reasoning engine (Base64 JPEG, max 720px)")
        else:
            print(f"\n[1/4] Analyzing disk stream: {video_path} (scenario: {scenario})")
            analyzer = VideoAnalyzer(video_path)
            video_metrics = analyzer.inspect_stream()

            print(f"      Video status: {video_metrics['status']} | "
                  f"FPS: {video_metrics['fps']} | "
                  f"Bitrate: {video_metrics['bitrate_kbps']} kbps | "
                  f"Sharpness: {video_metrics['sharpness_score']}")

            print(f"[2/4] Extracting keyframe at t={keyframe_second}s (Base64 JPEG, max 720px)...")
            image_base64 = analyzer.get_frame_base64(
                target_second=keyframe_second,
                max_width=720,
                jpeg_quality=75,
            )

        size_kb = len(image_base64) * 3 // 4 // 1024 if image_base64 else 0
        if image_base64:
            print(f"      Keyframe ready: ~{size_kb} KB JPEG")
        else:
            print("      No frame attached" + (" (Camera in standby mode)" if no_video_finding == STANDBY_VISUAL_FINDING else ""))

        metrics = _telemetry_metrics(telemetry, video_metrics, video_available)
        if metrics["online"] is False:
            # Offline is conclusive evidence on its own; Bedrock cannot add data the device is not sending.
            print("[3/4] Pre-check: the device is reported offline; skipping SOP retrieval")
            print("[4/4] Offline verdict from the pre-check: reasoning engine not invoked")
            return finish({
                "video_metrics": video_metrics,
                "telemetry": telemetry,
                "retrieved_articles": [],
                "diagnostic_result": _offline_diagnostic(telemetry),
                "analysis_skipped": False,
                "_provider": "precheck",
                "_model": None,
            })

        anomaly = self._precheck_anomaly(video_metrics, telemetry, video_available)
        missing = _missing_essential_metrics(metrics)
        if anomaly is None and missing:
            print(f"[3/4] Pre-check: no anomaly, but essential metrics are unavailable: {missing}; skipping SOP retrieval")
            print("[4/4] Not enough measured data: reasoning engine not invoked")
            report = _insufficient_data_diagnostic(missing, video_available)
            report["visual_findings"] = [no_video_finding] if no_video_finding else []
            return finish({
                "video_metrics": video_metrics,
                "telemetry": telemetry,
                "retrieved_articles": [],
                "diagnostic_result": report,
                "analysis_skipped": False,
                "missing_metrics": missing,
                "_provider": "precheck",
                "_model": None,
            })
        if anomaly is None:
            print("[3/4] Pre-check nominal: skipping SOP retrieval")
            print("[4/4] No anomaly: reasoning engine not invoked")
            report = _nominal_diagnostic(video_available)
            if no_video_finding:
                report["visual_findings"] = [no_video_finding]
            return finish({
                "video_metrics": video_metrics,
                "telemetry": telemetry,
                "retrieved_articles": [],
                "diagnostic_result": report,
                "severity_level": severity.GREEN,
                "analysis_skipped": True,
                "skip_reason": (
                    "No anomaly detected in measured telemetry; no video frame was analyzed."
                    if no_frame_reason else
                    "No anomaly detected by telemetry and OpenCV pre-check."
                ),
                "_provider": "precheck",
                "_model": None,
            })

        print(f"[3/4] Pre-check: {anomaly['level']} ({anomaly['reason']}); retrieving Ring troubleshooting procedures...")
        articles = self.retriever.retrieve_guidance(video_metrics, telemetry)
        print(f"      Retrieved SOPs: {len(articles)} ({[a['id'] for a in articles]})")

        dh  = telemetry.get("device_health", {})
        rtp = telemetry.get("webrtc_stats", {}).get("inbound_rtp", {})

        if no_frame_reason:
            standby_note = (
                "CAMERA STATE: NOT REPORTED. NO VIDEO FRAME AVAILABLE — TELEMETRY-ONLY DIAGNOSIS "
                f"({no_frame_reason})"
            )
            visual_instruction = (
                "IMPORTANT: No video frame is attached and the camera state is unknown; do NOT assume standby. "
                f"In 'visual_findings' you MUST output ONLY: [{json.dumps(no_frame_reason)}]. "
                "Do not describe or infer any visual or optical condition. "
                "Base the diagnosis EXCLUSIVELY on the measured telemetry above."
            )
        elif not camera_active:
            standby_note = (
                "CAMERA STATE: STANDBY (Sensor powered down by low-power design). "
                "NO ACTIVE STREAM OR FRAMES."
            )
            visual_instruction = (
                "IMPORTANT: Device is in STANDBY (camera and optical sensor turned off). "
                "There is NO attached video frame or image. "
                "In 'visual_findings' you MUST output ONLY: ['Camera in low-power standby mode. Optical sensor powered down by design.']. "
                "Do NOT describe or mention persons, walls, bricks, or visual elements. "
                "Your diagnosis and recommendations must be based EXCLUSIVELY on device telemetry (Wi-Fi, WebRTC, battery)."
            )
        else:
            standby_note = "CAMERA STATE: ACTIVE (Doorbell Ding, PIR Motion, or Live View)"
            visual_instruction = (
                "The video keyframe is attached as an image. Analyze both the visual evidence and telemetry metrics, then output ONLY the strict JSON diagnostic report."
            )

        def show(value: Any, section: str, field: str, unit: str = "", fmt: str = "") -> str:
            """Renders a metric, or says it was not measured (with the reason when known)."""
            if value is None:
                reason = _measurement_reason(telemetry, section, field)
                return f"unavailable ({reason})" if reason else "unavailable (not measured)"
            return f"{value:{fmt}}{unit}"

        power_mode = dh.get("power_mode")
        trans_v = dh.get("transformer_voltage_vac")
        if power_mode == "battery":
            pwr_note = "BATTERY ONLY (Unwired installation; transformer not applicable by customer design)"
            trans_str = "N/A (Battery installation; transformer disconnected by design)"
        elif trans_v is None:
            pwr_note = "UNKNOWN (no power mode or transformer voltage reported; do not assume battery-only or wired)"
            trans_str = show(None, "device_health", "transformer_voltage_vac")
        else:
            pwr_note = "WIRED WITH CONTINUOUS TRANSFORMER"
            power_alerts = [a for a in anomaly["anomalies"] if a["metric"] in ("transformer", "mechanical_chime")]
            trans_str = f"{trans_v} V AC" + "".join(
                f" [{a['level']}: {a['label']} ({severity.OP_SYMBOLS[a['op']]} {a['limit']:g} V AC, project rule"
                + (" for the reported mechanical in-home chime only" if a["metric"] == "mechanical_chime" else "")
                + ")]" for a in power_alerts)

        if no_frame_reason:
            video_section = "- Not available: no video frame was analyzed."
        else:
            video_section = (
                f"- File           : {video_metrics['file']}\n"
                f"- Resolution     : {video_metrics['resolution']}\n"
                f"- FPS            : {video_metrics['fps']}\n"
                f"- Bitrate        : {video_metrics['bitrate_kbps']} kbps\n"
                f"- Sharpness score: {_sharpness_line(video_metrics.get('sharpness_score'))}\n"
                f"- Video status   : {video_metrics['status']}\n"
                f"- Detected issues: {video_metrics.get('issues', [])}"
            )

        precheck_lines = []
        for a in anomaly["anomalies"]:
            if a["metric"] == "video":
                issues = "; ".join(_optical_issues(video_metrics)) or a["finding"]
                precheck_lines.append(f"- {a['level']}: {a['reason']} (video analyzer: {issues})")
            else:
                target = severity.green_target(a["metric"], {"power_mode": metrics["power_mode"],
                                                             "chime_type": metrics["chime_type"]})
                precheck_lines.append(f"- {a['level']}: {severity.describe(a)}. To return to GREEN it must be "
                                      f"{target}.")
        if _black_frame_from_power_cut(anomaly["anomalies"]):
            precheck_lines.append("- The black frame is explained by the loss of power (transformer power cut): "
                                  "it is a symptom of the same cause, not a separate optical problem.")
        precheck_section = "\n".join(precheck_lines)
        levels_section = _metric_levels_section(metrics, video_metrics if video_available else {})
        kb_section = _kb_for_prompt(articles, video_metrics) if articles else (
            "NONE: no procedure matched. Give a conservative, generic mitigation; it is reported as "
            "generated, not from the knowledge base.")

        user_prompt = f"""RING SUPPORT CASE — MULTIMODAL DIAGNOSTIC
- {standby_note}

=== OPENCV VIDEO METRICS ===
{video_section}

=== DEVICE HEALTH TELEMETRY ===
- Source         : {_source_description(telemetry.get('_source'))}
- Status read at : {telemetry.get('captured_at') or 'unavailable'}
- Last check-in  : {show(dh.get('updated_at'), 'device_health', 'updated_at')}
- Online         : {show(metrics['online'], 'device_health', 'online')}
- Power Setup    : {pwr_note}
- Model          : {telemetry.get('device_model') or 'N/A'}
- RSSI           : {show(dh.get('rssi'), 'device_health', 'rssi', ' dBm')}
- Wi-Fi SSID     : {show(dh.get('wifi_name'), 'device_health', 'wifi_name')} | Connected: {show(dh.get('wifi_is_connected'), 'device_health', 'wifi_is_connected')}
- Battery        : {show(dh.get('battery_percentage'), 'device_health', 'battery_percentage', '%')}
- Battery saver  : {show(dh.get('battery_saver_low_voltage'), 'device_health', 'battery_saver_low_voltage')}
- LED status     : {show(dh.get('led_status'), 'device_health', 'led_status')}
- Transformer    : {trans_str}
- Firmware       : {show(dh.get('firmware'), 'device_health', 'firmware')}

=== TRANSPORT METRICS (WebRTC getStats schema; same source as above) ===
- Jitter         : {show(metrics['jitter_ms'], 'webrtc_stats', 'jitter', ' ms', '.1f')}
- Packet loss    : {show(metrics['packet_loss_pct'], 'webrtc_stats', 'packet_loss_fraction', '%', '.1f')}
- Dropped frames : {show(rtp.get('frames_dropped'), 'webrtc_stats', 'frames_dropped')} / {show(rtp.get('frames_received'), 'webrtc_stats', 'frames_received')} received
- WebRTC FPS     : {show(rtp.get('frames_per_second'), 'webrtc_stats', 'frames_per_second')}
- Real Bitrate   : {show(rtp.get('bitrate_kbps'), 'webrtc_stats', 'bitrate_kbps', ' kbps')}
- RTT            : {show(metrics['rtt_ms'], 'webrtc_stats', 'current_round_trip_time', ' ms', '.1f')}
- Essential metrics unavailable: {', '.join(_READABLE_METRICS.get(n, n) for n in missing) if missing else 'none'}

=== METRIC LEVELS (precomputed from the severity table; use them, do not compare numbers yourself) ===
{levels_section}

=== PRE-CHECK ANOMALIES (deterministic, severity table: {severity.PROVENANCE}) ===
{precheck_section}
- Worst level: {anomaly['level']} (the report severity). diagnostic_status can NOT be HEALTHY.

=== APPLICABLE TROUBLESHOOTING PROCEDURES (project KB, RAG) ===
{kb_section}

{visual_instruction}"""

        print(f"[4/4] Invoking primary reasoning engine (BEDROCK / {self.model_id})...")
        provider = "bedrock"
        mitigation_source = MITIGATION_SOURCE_KB if articles else MITIGATION_SOURCE_GENERATED
        bedrock_attempts = 0
        try:
            diagnostic_result, bedrock_attempts = self._reason_with_bedrock(
                user_prompt, image_base64, anomaly, video_metrics if video_available else None)
            if diagnostic_result.pop("_mitigation_rejected", None):
                # T15: only the customer text failed twice; keep Bedrock's findings, use the deterministic message.
                dominant = _assess(metrics, video_metrics, video_available)["dominant"]
                text, source = _fallback_mitigation(dominant, articles)
                diagnostic_result["recommended_mitigation"] = text
                mitigation_source = (MITIGATION_SOURCE_KB_DETERMINISTIC if source == MITIGATION_SOURCE_KB
                                     else MITIGATION_SOURCE_GENERIC_DETERMINISTIC)
        except RuntimeError as exc:
            provider = "local_fallback"
            bedrock_attempts = self.last_bedrock_attempts   # requests made before giving up
            print(f"      Bedrock unavailable; using deterministic local fallback (mock-up): {exc}")
            diagnostic_result = self._build_local_diagnostic(
                video_metrics=video_metrics,
                telemetry=telemetry,
                articles=articles,
                camera_active=video_available,
            )
            dominant = _assess(_telemetry_metrics(telemetry, video_metrics, video_available),
                               video_metrics, video_available)["dominant"]
            mitigation_source = _fallback_mitigation(dominant, articles)[1] if dominant else None

        if no_video_finding:
            diagnostic_result["visual_findings"] = [no_video_finding]

        # The severity label is deterministic (worst metric level of the severity table), never the
        # model's opinion; the model's findings and mitigation are kept.
        severity_override = None
        model_status = diagnostic_result.get("diagnostic_status")
        if model_status != anomaly["level"]:
            severity_override = {"model": model_status, "deterministic": anomaly["level"]}
            logger.warning("Replacing model severity %s with the deterministic level %s (%s)",
                           model_status, anomaly["level"], anomaly["reason"])
            diagnostic_result["diagnostic_status"] = anomaly["level"]

        print(f"      Diagnosis: {diagnostic_result.get('diagnostic_status')} | "
              f"Domain: {diagnostic_result.get('root_cause_domain')} | "
              f"Confidence: {diagnostic_result.get('confidence_score')}")

        return finish({
            "video_metrics":     video_metrics,
            "telemetry":         telemetry,
            "retrieved_articles": articles,
            "diagnostic_result": diagnostic_result,
            "severity_level":    anomaly["level"],
            "severity_override": severity_override,
            "mitigation_source": mitigation_source,
            "bedrock_attempts":  bedrock_attempts,
            "_provider":         provider,
            "_model":            self.model_id if provider == "bedrock" else None,
        })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

CLI_PROVIDER_LABELS = {
    "bedrock": "AWS Bedrock (Claude)",
    "local_fallback": "Local fallback (mock-up; Bedrock unavailable or its answer was rejected)",
    "precheck": "Pre-check (deterministic; Bedrock not called)",
}


def main():
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Ring TelemetryOps Concierge Agent — Autonomous Multimodal Diagnostics"
    )
    parser.add_argument(
        "--video",
        type=str,
        default=os.path.join("data", "samples", "bad_internet.mp4"),
        help="Path to the MP4 video stream to inspect",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default="wifi_degradation",
        choices=TelemetrySimulator.SUPPORTED_SCENARIOS,
        help="Digital Twin telemetry scenario (simulated data).",
    )
    parser.add_argument(
        "--second",
        type=float,
        default=1.0,
        help="Target timestamp in seconds to extract keyframe from",
    )
    args = parser.parse_args()

    if not os.path.exists(args.video):
        print(f"[ERROR] Video file not found: {args.video}")
        return

    ring_client = RingClient(mock=True)
    print(f"\n[Ring] Telemetry source: Digital Twin simulator ({ring_client.mode})")
    telemetry = ring_client.get_telemetry(scenario=args.scenario)

    concierge = SupportConcierge()
    result    = concierge.process_stream(
        video_path=args.video,
        telemetry=telemetry,
        keyframe_second=args.second,
    )

    report = result["diagnostic_result"]
    print("\n" + "=" * 65)
    print("  AGENT DIAGNOSTIC REPORT (JSON)")
    print("=" * 65)
    print(json.dumps({**report, "_provider": result["_provider"], "_model": result.get("_model")},
                     indent=2, ensure_ascii=False))
    print(f"\nProblem type: {severity.DOMAIN_LABELS.get(report['root_cause_domain'], report['root_cause_domain'])}"
          f" | Engine: {CLI_PROVIDER_LABELS.get(result['_provider'], result['_provider'])}")


if __name__ == "__main__":
    main()