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
import sys
import json
import argparse
from pathlib import Path
from typing import Dict, Any, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from src.video_pipeline.analyzer import VideoAnalyzer
from src.agent.knowledge_retriever import KnowledgeRetriever
from src.utils.telemetry_simulator import TelemetrySimulator
from src.utils.ring_client import RingClient

load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# Default Models by Provider
# ---------------------------------------------------------------------------
BEDROCK_DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a Principal Telemetry & Support Systems Engineer at Amazon Ring,
specialized in cross-modal diagnostics of smart video doorbells, computer vision stream analysis,
device health telemetry, and network transport telemetry (WebRTC/RTCP).
The case states the source of every telemetry block: only data marked as Ring Partner API comes
from Ring. Digital Twin data is simulated, and client-supplied data is unverified.

Your objective is to SIMULTANEOUSLY evaluate:
1. The attached video keyframe (visual clarity, exposure, sharpness, artifacts, color tints, reflections, or black frames).
2. Quantitative video stream metrics (sharpness_score via Laplacian variance, effective FPS, bitrate_kbps, mean_brightness, issues).
3. Device Health telemetry (RSSI, battery_percentage, transformer_voltage_vac, led_status, battery_saver_low_voltage).
4. Transport metrics in the WebRTC W3C getStats schema (jitter, packet_loss_fraction, RTT, frames_dropped, frames_per_second).
5. Troubleshooting procedures retrieved from the project knowledge base (Ring Help Center-informed; not Ring SOPs).

CRITICAL DIAGNOSTIC RULES:
- UNAVAILABLE METRICS: A value shown as "unavailable" was NOT measured. Never treat it as zero, nominal, or degraded, and never cite it as evidence. Network metrics can only be called excellent or nominal when they were actually measured. Never return HEALTHY when power or network health is unavailable.
- BATTERY-ONLY INSTALLATION: Only if power_mode="battery", the device operates on battery by customer design. An unconnected transformer is NOT a failure. If battery is above 10% and network metrics are nominal, the power subsystem is HEALTHY.
- UNKNOWN POWER SETUP: If power_mode is not given and transformer_voltage_vac is unavailable, the power setup is UNKNOWN. Do NOT assume a battery-only or a wired installation.
- STANDBY / LOW-POWER MODE: If the camera is inactive (camera_active=false or status="STANDBY"), the optical sensor is shut down by low-power design. There is NO video frame or active stream. In "visual_findings" you MUST output EXCLUSIVELY: ["Camera in low-power standby mode. Optical sensor powered down by design."]. NEVER invent or hallucinate visual scenes, people, walls, bricks, or optical features during Standby. Root cause and remediation must be 100% telemetry-driven (Wi-Fi, WebRTC, battery).
- TELEMETRY-ONLY (NO FRAME): If the case says no video frame is available, the camera state is NOT known to be standby. Do not describe or infer any visual or optical condition; in "visual_findings" output only the stated reason. Root cause and remediation must be telemetry-driven.
- OPTICAL VS NETWORK DISAMBIGUATION: If video is blurry, exhibits pink/purple color tint, IR glare/halo, or 50Hz/60Hz flickering horizontal bands, BUT network metrics are excellent (RSSI > -65 dBm, loss < 1%, jitter < 15ms) → assign OPTICAL_PHYSICAL root cause.
- NETWORK DEGRADATION: If video shows macroblocking, pixelation, stuttering, OR RSSI < -75 dBm, OR packet loss > 8%, OR jitter > 40ms → assign NETWORK root cause.
- POWER SUBSYSTEM: If battery_percentage < 10% OR battery_saver_low_voltage=true OR transformer_voltage_vac < 12V AC (on wired setups) → assign HARDWARE_POWER or ENVIRONMENT_POWER.
- MECHANICAL IN-HOME CHIME: Only if chime_type_setting="mechanical" on a wired setup and transformer_voltage_vac < 16V AC → assign DEGRADED with HARDWARE_POWER (project diagnostic rule for mechanical chimes, not a Ring specification; it does not apply to other devices).
- AUDIO DEGRADATION: If two-way audio distortion or choppy audio correlates with high jitter → assign AUDIO_NETWORK.
- FIELD OF VIEW / CONFIGURATION: If issue involves mounting height, false motion triggers, or PIR angle → assign ALGORITHM_CONFIGURATION.
- NOMINAL: If all telemetry and optical metrics satisfy healthy thresholds → assign HEALTHY with root_cause_domain="NONE".
- EVIDENCE: Only telemetry labelled ring_partner_api comes from Ring. The numeric rules above are project diagnostic heuristics, not Ring requirements. The troubleshooting procedures are this project's knowledge base: content with evidence_level "unverified", verification "unverified" or source_verification "pending" is not confirmed, so never cite them as official Ring documentation and never state that a Ring API endpoint or capability exists because a procedure mentions it.

RESPOND ONLY with a valid, parseable JSON object in English, without markdown wrapping, without explanations outside JSON:
{
  "diagnostic_status": "HEALTHY|DEGRADED|CRITICAL",
  "root_cause_domain": "NETWORK|OPTICAL_PHYSICAL|HARDWARE_POWER|ENVIRONMENT_POWER|AUDIO_NETWORK|ALGORITHM_CONFIGURATION|NONE",
  "confidence_score": 0.0,
  "telemetry_findings": ["Max 2 concise, factual bullet points in English"],
  "visual_findings": ["Max 2 concise visual observations on the frame in English"],
  "recommended_mitigation": "Single direct and actionable resolution sentence based on Ring SOPs"
}"""

STANDBY_VISUAL_FINDING = "Camera in low-power standby mode. Optical sensor powered down by design."

# Anomaly thresholds shared by the pre-check and the local fallback (same meaning, one definition).
# Project heuristics, not Ring Partner API specifications. SYSTEM_PROMPT states the same values.
BATTERY_CRITICAL_PCT = 10        # battery_percentage < this
TRANSFORMER_MIN_VAC = 12.0       # transformer_voltage_vac < this (wired installs)
MECHANICAL_CHIME_MIN_VAC = 16.0  # transformer < this, only with chime_type_setting="mechanical" (product rule)
RSSI_DEGRADED_DBM = -75          # rssi < this
PACKET_LOSS_DEGRADED_PCT = 8.0   # packet loss % > this
JITTER_DEGRADED_MS = 40.0        # jitter ms > this
STREAM_MIN_FPS = 20.0            # fps < this
STREAM_MIN_BITRATE_KBPS = 150.0  # bitrate kbps < this


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


def _network_degraded(m: Dict[str, Any]) -> bool:
    return (
        (m["rssi"] is not None and m["rssi"] < RSSI_DEGRADED_DBM)
        or (m["packet_loss_pct"] is not None and m["packet_loss_pct"] > PACKET_LOSS_DEGRADED_PCT)
        or (m["jitter_ms"] is not None and m["jitter_ms"] > JITTER_DEGRADED_MS)
    )


def _mechanical_chime_undervoltage(m: Dict[str, Any]) -> bool:
    """Product rule: a reported mechanical in-home chime on a wired install needs >= 16 V AC."""
    return (
        m["chime_type"] == "mechanical"
        and m["power_mode"] != "battery"
        and m["transformer"] is not None
        and m["transformer"] < MECHANICAL_CHIME_MIN_VAC
    )


def _stream_degraded(m: Dict[str, Any]) -> bool:
    return (
        (m["fps"] is not None and m["fps"] < STREAM_MIN_FPS)
        or (m["bitrate"] is not None and m["bitrate"] < STREAM_MIN_BITRATE_KBPS)
    )


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
        self.model_id = (
            model_id or os.getenv("BEDROCK_MODEL_ID", BEDROCK_DEFAULT_MODEL)
        ).strip()

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
            "max_tokens": 512,
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

    def _build_local_diagnostic(
        self,
        video_metrics: Dict[str, Any],
        telemetry: Dict[str, Any],
        articles: list[Dict[str, Any]],
        camera_active: bool,
    ) -> Dict[str, Any]:
        """Builds a deterministic report when Bedrock is unavailable."""
        m = _telemetry_metrics(telemetry, video_metrics, camera_active)
        if m["online"] is False:
            return _offline_diagnostic(telemetry)
        rssi = m["rssi"]
        battery = m["battery"]
        transformer = m["transformer"]
        jitter_ms = m["jitter_ms"]
        packet_loss = m["packet_loss_pct"]
        fps = m["fps"]
        bitrate = m["bitrate"]
        findings: list[str] = []
        visual_findings: list[str] = []
        root_cause = "NONE"
        status = "HEALTHY"
        confidence = 0.86

        if not camera_active:
            visual_findings.append(
                "Camera in low-power standby mode. Optical sensor powered down by design."
            )
        else:
            visual_findings.extend(video_metrics.get("issues", [])[:2])

        # Same rule order as _precheck_anomaly, so both name the same domain for the same metrics.
        if battery is not None and battery < BATTERY_CRITICAL_PCT:
            root_cause = "HARDWARE_POWER"
            status = "CRITICAL"
            findings.append(f"Battery level is critically low at {battery}%.")
        elif transformer is not None and transformer < TRANSFORMER_MIN_VAC:
            root_cause = "HARDWARE_POWER"
            status = "CRITICAL" if transformer == 0 else "DEGRADED"
            findings.append(f"Transformer voltage is {transformer} VAC.")
        elif _mechanical_chime_undervoltage(m):
            root_cause = "HARDWARE_POWER"
            status = "DEGRADED"
            findings.append(
                f"Transformer voltage is {transformer} VAC, below the {MECHANICAL_CHIME_MIN_VAC:g} VAC "
                "project diagnostic rule for a mechanical in-home chime."
            )
        elif _network_degraded(m):
            root_cause = "NETWORK"
            status = "DEGRADED"
            if rssi is not None and rssi < RSSI_DEGRADED_DBM:
                findings.append(f"Wi-Fi RSSI is weak at {rssi} dBm.")
            if ((packet_loss is not None and packet_loss > PACKET_LOSS_DEGRADED_PCT)
                    or (jitter_ms is not None and jitter_ms > JITTER_DEGRADED_MS)):
                loss_str = f"{packet_loss:.1f}%" if packet_loss is not None else "unmeasured"
                jitter_str = f"{jitter_ms:.1f} ms" if jitter_ms is not None else "unmeasured"
                findings.append(
                    f"Transport quality is degraded ({loss_str} packet loss, {jitter_str} jitter)."
                )
        elif _stream_degraded(m):
            root_cause = "NETWORK"
            status = "DEGRADED"
            fps_str = fps if fps is not None else "unknown"
            bitrate_str = bitrate if bitrate is not None else "unknown"
            findings.append(f"Stream quality is reduced ({fps_str} FPS, {bitrate_str} kbps).")
        elif camera_active and video_metrics.get("status") == "CRITICAL":
            root_cause = "OPTICAL_PHYSICAL"
            status = "CRITICAL"
            findings.append("The analyzed frame has no usable optical signal.")
        elif camera_active and video_metrics.get("issues"):
            root_cause = "OPTICAL_PHYSICAL"
            status = "DEGRADED"
            findings.append("The video analyzer detected visual quality issues.")
        elif camera_active and video_metrics.get("optical_banding_detected", False):
            root_cause = "OPTICAL_PHYSICAL"
            status = "DEGRADED"
            findings.append("OpenCV detected degraded optical clarity or banding.")
        elif _missing_essential_metrics(m):
            report = _insufficient_data_diagnostic(_missing_essential_metrics(m), camera_active)
            report["visual_findings"] = visual_findings[:2]
            return report
        else:
            findings.append("Device, transport, and analyzed video metrics are nominal.")

        if not findings:
            findings.append("No deterministic anomaly was identified.")

        mitigation = "Continue monitoring the device."
        if articles:
            actions = articles[0].get("recommended_actions", [])
            if actions:
                mitigation = actions[0]

        return {
            "diagnostic_status": status,
            "root_cause_domain": root_cause,
            "confidence_score": confidence,
            "telemetry_findings": findings[:2],
            "visual_findings": visual_findings[:2],
            "recommended_mitigation": mitigation,
        }

    def _precheck_anomaly(
        self,
        video_metrics: Dict[str, Any],
        telemetry: Dict[str, Any],
        camera_active: bool,
    ) -> Optional[Dict[str, Any]]:
        """Detects clear anomalies before spending a Bedrock request. Unmeasured metrics never trigger one."""
        m = _telemetry_metrics(telemetry, video_metrics, camera_active)
        battery = m["battery"]
        transformer = m["transformer"]

        if m["online"] is False:
            return {"domain": "UNKNOWN", "reason": "Device reported offline"}
        if battery is not None and battery < BATTERY_CRITICAL_PCT:
            return {"domain": "HARDWARE_POWER", "reason": "Critical battery level"}
        if transformer is not None and transformer < TRANSFORMER_MIN_VAC:
            return {"domain": "HARDWARE_POWER", "reason": "Transformer undervoltage"}
        if _mechanical_chime_undervoltage(m):
            return {"domain": "HARDWARE_POWER", "reason": "Transformer voltage too low for a mechanical chime"}
        if _network_degraded(m):
            return {"domain": "NETWORK", "reason": "Degraded network metrics"}
        if _stream_degraded(m):
            return {"domain": "NETWORK", "reason": "Reduced stream quality"}
        if camera_active and video_metrics.get("status") == "CRITICAL":
            return {"domain": "OPTICAL_PHYSICAL", "reason": "No usable optical signal"}
        # Blur is judged by the analyzer (it reports it in "issues"); no second raw sharpness threshold here.
        if camera_active and video_metrics.get("issues"):
            return {"domain": "OPTICAL_PHYSICAL", "reason": "Visual quality issue detected"}
        if camera_active and video_metrics.get("optical_banding_detected", False):
            return {"domain": "OPTICAL_PHYSICAL", "reason": "Degraded optical metrics"}
        return None

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
            print("[3/4] Pre-check: Ring reports the device offline; skipping Bedrock.")
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
            print(f"[3/4] Pre-check: no anomaly, but essential metrics are unavailable: {missing}")
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
            print("[3/4] Pre-check: telemetry and video metrics are nominal; skipping Bedrock.")
            return finish({
                "video_metrics": video_metrics,
                "telemetry": telemetry,
                "retrieved_articles": [],
                "diagnostic_result": None,
                "analysis_skipped": True,
                "skip_reason": (
                    "No anomaly detected in measured telemetry; no video frame was analyzed."
                    if no_frame_reason else
                    "No anomaly detected by telemetry and OpenCV pre-check."
                ),
                "_provider": "precheck",
                "_model": None,
            })

        print("[3/4] Retrieving Ring troubleshooting procedures...")
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
            if trans_v == 0:
                trans_str = "0.0 V AC [CRITICAL ALERT: COMPLETE POWER LOSS / DISCONNECTED TRANSFORMER]"
            elif trans_v < TRANSFORMER_MIN_VAC:
                trans_str = f"{trans_v} V AC [ALERT: INSUFFICIENT SUBVOLTAGE]"
            elif _mechanical_chime_undervoltage(metrics):
                trans_str = (f"{trans_v} V AC [ALERT: below the {MECHANICAL_CHIME_MIN_VAC:g} V AC project rule "
                             "for the reported mechanical in-home chime]")
            else:
                trans_str = f"{trans_v} V AC"

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

=== APPLICABLE TROUBLESHOOTING PROCEDURES (project KB, RAG) ===
{json.dumps(articles, indent=2, ensure_ascii=False)}

{visual_instruction}"""

        print(f"[4/4] Invoking primary reasoning engine (BEDROCK / {self.model_id})...")
        provider = "bedrock"
        try:
            raw_response = self._call_llm(user_prompt, image_base64)
            diagnostic_result = self._parse_response(raw_response)
            if "_parse_error" in diagnostic_result:
                raise RuntimeError("Bedrock returned a response that did not match the diagnostic schema.")
            if diagnostic_result.get("diagnostic_status") == "HEALTHY":
                raise RuntimeError(
                    "Bedrock returned HEALTHY despite a pre-check anomaly: "
                    + anomaly["reason"]
                )
        except RuntimeError as exc:
            provider = "local_fallback"
            print(f"      Bedrock unavailable; using deterministic local fallback: {exc}")
            diagnostic_result = self._build_local_diagnostic(
                video_metrics=video_metrics,
                telemetry=telemetry,
                articles=articles,
                camera_active=video_available,
            )

        if no_video_finding:
            diagnostic_result["visual_findings"] = [no_video_finding]

        print(f"      Diagnosis: {diagnostic_result.get('diagnostic_status')} | "
              f"Domain: {diagnostic_result.get('root_cause_domain')} | "
              f"Confidence: {diagnostic_result.get('confidence_score')}")

        return finish({
            "video_metrics":     video_metrics,
            "telemetry":         telemetry,
            "retrieved_articles": articles,
            "diagnostic_result": diagnostic_result,
            "_provider":         provider,
            "_model":            self.model_id if provider == "bedrock" else None,
        })


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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

    print("\n" + "=" * 65)
    print("  AGENT DIAGNOSTIC REPORT (JSON)")
    print("=" * 65)
    print(json.dumps(result["diagnostic_result"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()