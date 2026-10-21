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
from src.video_pipeline.analyzer import VideoAnalyzer, STREAM_ISSUE_PREFIXES
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


def customer_text_problems(text: str) -> list[str]:
    """Reasons why a mitigation is not fit for the customer notification (empty when it is)."""
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
    if ELECTRICAL_DIY.search(text):
        problems.append("recommended_mitigation asks the customer to do electrical work")
    return problems


def findings_problems(report: Dict[str, Any]) -> list[str]:
    """Technician findings may be technical, but never internal field names."""
    names = sorted({m for field in ("telemetry_findings", "visual_findings")
                    for item in report.get(field, []) for m in _INTERNAL_FIELD.findall(item)})
    return [f"findings contain internal field names: {', '.join(names)}"] if names else []


SYSTEM_PROMPT = f"""You are a Principal Telemetry & Support Systems Engineer at Amazon Ring,
specialized in cross-modal diagnostics of smart video doorbells, computer vision stream analysis,
device health telemetry, and network transport telemetry (WebRTC/RTCP).
The case states the source of every telemetry block: only data marked as Ring Partner API comes
from Ring. Digital Twin data is simulated, and client-supplied data is unverified.

Your objective is to SIMULTANEOUSLY evaluate:
1. The attached video keyframe (visual clarity, exposure, sharpness, artifacts, color tints, reflections, or black frames).
2. Quantitative video stream metrics (sharpness_score via Laplacian variance, effective FPS, bitrate_kbps, mean_brightness, issues).
3. Device Health telemetry (RSSI, battery_percentage, transformer_voltage_vac, led_status, battery_saver_low_voltage).
4. Transport metrics in the WebRTC W3C getStats schema (jitter, packet_loss_fraction, RTT, frames_dropped, frames_per_second).
5. The anomalies found by the deterministic pre-check, each with its severity level.
6. Troubleshooting procedures retrieved from the project knowledge base (Ring Help Center-informed; not Ring SOPs).

SEVERITY LEVELS ({severity.PROVENANCE}; the pre-check already applied them):
{severity.table_for_prompt()}
The application sets diagnostic_status from the worst pre-check level; your job is the root cause, the findings and the mitigation.

CRITICAL DIAGNOSTIC RULES:
- PRE-CHECK ANOMALIES: When the case lists at least one pre-check anomaly, diagnostic_status can NOT be HEALTHY. A Weak, Poor or Critical Wi-Fi signal (RSSI) on its own IS an anomaly even when packet loss and jitter are fine: it risks dropouts and late alerts.
- UNAVAILABLE METRICS: A value shown as "unavailable" was NOT measured. Never treat it as zero, nominal, or degraded, and never cite it as evidence. Network metrics can only be called excellent or nominal when they were actually measured. Never return HEALTHY when power or network health is unavailable.
- BATTERY-ONLY INSTALLATION: Only if power_mode="battery", the device operates on battery by customer design. An unconnected transformer is NOT a failure. If the battery is GREEN in the severity levels and the network metrics are GREEN, the power subsystem is HEALTHY.
- UNKNOWN POWER SETUP: If power_mode is not given and transformer_voltage_vac is unavailable, the power setup is UNKNOWN. Do NOT assume a battery-only or a wired installation.
- BACKUP BATTERY: A wired doorbell with a charged internal battery and a transformer at 0 V is consistent: the battery is a backup that keeps the device running. Never call this inconsistent and never suggest stale telemetry for that reason.
- STANDBY / LOW-POWER MODE: If the camera is inactive (camera_active=false or status="STANDBY"), the optical sensor is shut down by low-power design. There is NO video frame or active stream. In "visual_findings" you MUST output EXCLUSIVELY: ["Camera in low-power standby mode. Optical sensor powered down by design."]. NEVER invent or hallucinate visual scenes, people, walls, bricks, or optical features during Standby. Root cause and remediation must be 100% telemetry-driven (Wi-Fi, WebRTC, battery).
- TELEMETRY-ONLY (NO FRAME): If the case says no video frame is available, the camera state is NOT known to be standby. Do not describe or infer any visual or optical condition; in "visual_findings" output only the stated reason. Root cause and remediation must be telemetry-driven.
- OPTICAL VS NETWORK DISAMBIGUATION: If video is blurry, exhibits pink/purple color tint, IR glare/halo, or 50Hz/60Hz flickering horizontal bands, BUT every network metric is GREEN in the severity levels → assign OPTICAL_PHYSICAL root cause.
- NETWORK DEGRADATION: If video shows macroblocking, pixelation, stuttering, OR any network metric (RSSI, packet loss, jitter, frame rate, bitrate) is DEGRADED or CRITICAL → assign NETWORK root cause.
- POWER SUBSYSTEM: If the battery or the transformer is DEGRADED or CRITICAL in the severity levels, OR battery_saver_low_voltage=true → assign HARDWARE_POWER or ENVIRONMENT_POWER.
- MECHANICAL IN-HOME CHIME: The mechanical chime level applies only if chime_type_setting="mechanical" on a wired setup → assign DEGRADED with HARDWARE_POWER. It is a project diagnostic rule for mechanical chimes, never a requirement of the doorbell itself (for example not of the Video Doorbell 4).
- AUDIO DEGRADATION: If two-way audio distortion or choppy audio correlates with high jitter → assign AUDIO_NETWORK.
- FIELD OF VIEW / CONFIGURATION: If issue involves mounting height, false motion triggers, or PIR angle → assign ALGORITHM_CONFIGURATION.
- NOMINAL: If all telemetry and optical metrics are GREEN → assign HEALTHY with root_cause_domain="NONE".
- THRESHOLDS YOU QUOTE: When you say how far a metric must improve, quote ONLY the boundary of its next better level given in the case (for example, Poor RSSI must reach "at or above -75 dBm"). Never invent other limits.
- MANUFACTURER REQUIREMENTS: Do not state Ring requirements (voltages, VA ratings, device models, accessories) unless they appear in a retrieved knowledge base article, and then call them "reference values (unverified)".
- ELECTRICAL SAFETY: Never tell the customer to do electrical work: no switching or resetting circuit breakers, no multimeter or voltage measurements, no rewiring, no replacing the transformer. For wiring or transformer problems tell them to contact a qualified electrician or Ring Support.
- VISUAL EVIDENCE: Recommend only what the image and metrics support. For a blurry lens do not mention condensation, protective film, obstructions or sun glare unless the image clearly shows them; do not list them as possibilities either.
- STANDBY IS NORMAL: Camera standby is the doorbell's normal idle state. Never tell the customer the device entered standby because of a problem.
- NO KNOWLEDGE BASE MATCH: If no troubleshooting procedure was retrieved, give a conservative, generic mitigation (check the device in the Ring app, contact Ring Support) and do not invent a specific procedure.
- EVIDENCE: Only telemetry labelled ring_partner_api comes from Ring. The numeric rules above are project diagnostic heuristics, not Ring requirements. The troubleshooting procedures are this project's knowledge base: content with evidence_level "unverified", verification "unverified" or source_verification "pending" is not confirmed, so never cite them as official Ring documentation and never state that a Ring API endpoint or capability exists because a procedure mentions it.

WHO READS EACH FIELD:
- "recommended_mitigation" is the notification the customer reads in the Ring app. Write plain, everyday English, at most {CUSTOMER_TEXT_MAX_WORDS - 10} words, in this order: what is happening, why it matters, what to do. Use no technical terms (no {', '.join(CUSTOMER_JARGON_SHOWN)}), no knowledge base article IDs, no internal field names, no electrical specifications (volts, amps, VA, charger ratings), and numbers only when they help the customer. Settings that live in the Ring app are written as "in the Ring app". When a retrieved article has customer_messages, start from the message that matches the problem and adapt it to the evidence.
- "telemetry_findings" and "visual_findings" are for a support technician: one short factual sentence per anomaly (technical terms allowed), but no internal field names (write "battery saver flag", not battery_saver_low_voltage).

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

    def _contract_problems(self, report: Dict[str, Any], anomaly: Dict[str, Any]) -> list[str]:
        """Why a parsed model report breaks the contract (schema, severity, customer text)."""
        if "_parse_error" in report:
            return ["the answer was not a JSON object matching the diagnostic schema "
                    f"({report['_parse_error']})"]
        problems = []
        if report.get("diagnostic_status") == "HEALTHY":
            problems.append("diagnostic_status was HEALTHY although the pre-check found: " + anomaly["reason"])
        problems += customer_text_problems(report["recommended_mitigation"])
        problems += findings_problems(report)
        return problems

    def _reason_with_bedrock(self, user_prompt: str, image_base64: Optional[str],
                             anomaly: Dict[str, Any]) -> tuple[Dict[str, Any], int]:
        """
        One Bedrock answer, plus one corrective retry when it breaks the contract (HEALTHY despite a
        pre-check anomaly, invalid schema, or customer text with jargon / field names / electrical
        work / too many words). A second violation, or a call error, raises RuntimeError so the caller
        uses the local fallback. Returns the report and the number of Bedrock calls made.
        """
        self.last_bedrock_attempts = 1
        raw = self._call_llm(user_prompt, image_base64)
        report = self._parse_response(raw)
        problems = self._contract_problems(report, anomaly)
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
        problems = self._contract_problems(report, anomaly)
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
                f"- Sharpness score: {video_metrics['sharpness_score']}\n"
                f"- Video status   : {video_metrics['status']}\n"
                f"- Detected issues: {video_metrics.get('issues', [])}"
            )

        precheck_lines = []
        for a in anomaly["anomalies"]:
            if a["metric"] == "video":
                issues = "; ".join(_optical_issues(video_metrics)) or a["finding"]
                precheck_lines.append(f"- {a['level']}: {a['reason']} (video analyzer: {issues})")
            else:
                precheck_lines.append(f"- {a['level']}: {severity.describe(a)}. To leave this band it must be "
                                      f"{severity.next_level_target(a)}.")
        precheck_section = "\n".join(precheck_lines)
        kb_section = json.dumps(articles, indent=2, ensure_ascii=False) if articles else (
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
- Essential metrics unavailable: {', '.join(missing) if missing else 'none'}

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
            diagnostic_result, bedrock_attempts = self._reason_with_bedrock(user_prompt, image_base64, anomaly)
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