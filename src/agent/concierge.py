"""
SupportConcierge — Ring TelemetryOps Concierge Agent
=====================================================
Core orchestrator for the autonomous Ring diagnostic agent.
Correlates quantitative computer vision video metrics, WebRTC network transport,
Ring device health telemetry, and multimodal LLM inference.

Supported providers:
  - "groq"    : Groq API (free tier, vision with qwen3.8-27b) — for development & testing
  - "bedrock" : Amazon Bedrock Runtime (Claude Sonnet 3.5 / Haiku 4.5) — for production & judging

Agent Output (Strict JSON schema):
  {
    "diagnostic_status"    : "HEALTHY" | "DEGRADED" | "CRITICAL",
    "root_cause_domain"    : "NETWORK" | "OPTICAL_PHYSICAL" | "HARDWARE_POWER" | "ENVIRONMENT_POWER" | "AUDIO_NETWORK" | "ALGORITHM_CONFIGURATION" | "NONE",
    "confidence_score"     : 0.0 – 1.0,
    "telemetry_findings"   : ["anomaly 1", "anomaly 2", ...],
    "visual_findings"      : ["visual observation 1", ...],
    "recommended_mitigation": "actionable resolution step based on official Ring standard operating procedures"
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
GROQ_VISION_MODEL     = "qwen/qwen3.8-27b"
BEDROCK_DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a Principal Telemetry & Support Systems Engineer at Amazon Ring,
specialized in cross-modal diagnostics of smart video doorbells, computer vision stream analysis,
Ring Partner API device health, and network transport telemetry (WebRTC/RTCP).

Your objective is to SIMULTANEOUSLY evaluate:
1. The attached video keyframe (visual clarity, exposure, sharpness, artifacts, color tints, reflections, or black frames).
2. Quantitative video stream metrics (sharpness_score via Laplacian variance, effective FPS, bitrate_kbps, mean_brightness, issues).
3. Ring Device Health telemetry (RSSI, battery_percentage, transformer_voltage_vac, led_status, battery_saver_low_voltage).
4. WebRTC W3C getStats metrics (jitter, packet_loss_fraction, RTT, frames_dropped, frames_per_second).
5. Applicable official Ring Standard Operating Procedures (SOPs) retrieved from the technical Knowledge Base.

CRITICAL DIAGNOSTIC RULES:
- BATTERY-ONLY INSTALLATION: If power_mode="battery" or transformer_voltage_vac is None/N/A, the device operates on battery by customer design. An unconnected transformer is NOT a failure. If battery is above 10% and network metrics are nominal, the power subsystem is HEALTHY.
- STANDBY / LOW-POWER MODE: If the camera is inactive (camera_active=false or status="STANDBY"), the optical sensor is shut down by low-power design. There is NO video frame or active stream. In "visual_findings" you MUST output EXCLUSIVELY: ["Camera in low-power standby mode. Optical sensor powered down by design."]. NEVER invent or hallucinate visual scenes, people, walls, bricks, or optical features during Standby. Root cause and remediation must be 100% telemetry-driven (Wi-Fi, WebRTC, battery).
- OPTICAL VS NETWORK DISAMBIGUATION: If video is blurry, exhibits pink/purple color tint, IR glare/halo, or 50Hz/60Hz flickering horizontal bands, BUT network metrics are excellent (RSSI > -65 dBm, loss < 1%, jitter < 15ms) → assign OPTICAL_PHYSICAL root cause.
- NETWORK DEGRADATION: If video shows macroblocking, pixelation, stuttering, OR RSSI < -75 dBm, OR packet loss > 8%, OR jitter > 40ms → assign NETWORK root cause.
- POWER SUBSYSTEM: If battery_percentage < 10% OR battery_saver_low_voltage=true OR transformer_voltage_vac < 12V AC (on wired setups) → assign HARDWARE_POWER or ENVIRONMENT_POWER.
- AUDIO DEGRADATION: If two-way audio distortion or choppy audio correlates with high jitter → assign AUDIO_NETWORK.
- FIELD OF VIEW / CONFIGURATION: If issue involves mounting height, false motion triggers, or PIR angle → assign ALGORITHM_CONFIGURATION.
- NOMINAL: If all telemetry and optical metrics satisfy healthy thresholds → assign HEALTHY with root_cause_domain="NONE".

RESPOND ONLY with a valid, parseable JSON object in English, without markdown wrapping, without explanations outside JSON:
{
  "diagnostic_status": "HEALTHY|DEGRADED|CRITICAL",
  "root_cause_domain": "NETWORK|OPTICAL_PHYSICAL|HARDWARE_POWER|ENVIRONMENT_POWER|AUDIO_NETWORK|ALGORITHM_CONFIGURATION|NONE",
  "confidence_score": 0.0,
  "telemetry_findings": ["Max 2 concise, factual bullet points in English"],
  "visual_findings": ["Max 2 concise visual observations on the frame in English"],
  "recommended_mitigation": "Single direct and actionable resolution sentence based on Ring SOPs"
}"""


class SupportConcierge:
    """
    Diagnostic Orchestrator for Ring TelemetryOps.

    Args:
        provider:   "groq" (testing) or "bedrock" (production & judging).
        kb_path:    Path to the Ring troubleshooting JSON knowledge base.
        model_id:   Model identifier override. If None, uses provider default.
    """

    def __init__(
        self,
        provider: str = "bedrock",
        kb_path: str = "data/knowledge_base/ring_video_troubleshooting.json",
        model_id: Optional[str] = None,
    ):
        if provider not in ("groq", "bedrock"):
            raise ValueError(f"Provider '{provider}' is not supported. Use 'groq' or 'bedrock'.")

        self.provider  = provider
        self.retriever = KnowledgeRetriever(kb_path=kb_path)
        self._client   = None

        # Resolve model ID: parameter > .env > default
        if model_id:
            self.model_id = model_id
        elif provider == "bedrock":
            self.model_id = os.getenv("BEDROCK_MODEL_ID", BEDROCK_DEFAULT_MODEL).strip()
        else:
            self.model_id = GROQ_VISION_MODEL

    def _get_client(self):
        """Initializes the LLM client on demand based on selected provider."""
        if self._client is not None:
            return self._client

        if self.provider == "groq":
            from groq import Groq
            api_key = os.getenv("GROQ_API_KEY", "").strip()
            if not api_key or api_key == "your_groq_api_key_here":
                raise RuntimeError(
                    "GROQ_API_KEY is not configured in .env. "
                    "Obtain a free key at https://console.groq.com"
                )
            self._client = Groq(api_key=api_key)

        elif self.provider == "bedrock":
            import boto3
            self._client = boto3.client(
                service_name="bedrock-runtime",
                region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1").strip(),
                aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "").strip(),
                aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "").strip(),
            )

        return self._client

    def _call_llm(self, user_prompt: str, image_base64: Optional[str]) -> str:
        """
        Invokes LLM with textual prompt and optional Base64 JPEG frame.
        Returns raw text response.
        """
        client = self._get_client()

        if self.provider == "groq":
            return self._call_groq(client, user_prompt, image_base64)
        elif self.provider == "bedrock":
            return self._call_bedrock(client, user_prompt, image_base64)

    def _call_groq(self, client, user_prompt: str, image_base64: Optional[str]) -> str:
        """Invokes Groq Chat Completions API with multimodal vision."""
        user_content = []

        if image_base64:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_base64}"
                },
            })

        user_content.append({
            "type": "text",
            "text": user_prompt,
        })

        response = client.chat.completions.create(
            model=self.model_id,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_content},
            ],
            temperature=0.1,
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content

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
                    "Alternatively, use provider 'groq'."
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
            missing = required - set(parsed.keys())
            if missing:
                raise ValueError(f"Missing required fields in response: {missing}")
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

    def process_stream(
        self,
        video_path: Optional[str] = None,
        telemetry: Optional[Dict[str, Any]] = None,
        keyframe_second: float = 1.0,
        frame_base64: Optional[str] = None,
        camera_active: bool = True,
    ) -> Dict[str, Any]:
        """
        Executes end-to-end multimodal diagnostic pipeline:
        1. Quantitative video telemetry extraction (OpenCV or live Canvas frame).
        2. Base64 keyframe extraction for multimodal reasoning.
        3. Context grounding against official Ring SOPs (RAG).
        4. Multimodal LLM synthesis (Bedrock or Groq).
        5. Defensive JSON schema parsing and diagnostic report formatting.
        """
        telemetry = telemetry or {}
        scenario = telemetry.get("scenario", "unknown")

        if not camera_active:
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
            print("      No frame attached (Camera in standby mode)")

        print("[3/4] Retrieving official Ring guidelines (RAG)...")
        articles = self.retriever.retrieve_guidance(video_metrics, telemetry)
        print(f"      Retrieved SOPs: {len(articles)} ({[a['id'] for a in articles]})")

        dh  = telemetry.get("device_health", {})
        rtp = telemetry.get("webrtc_stats", {}).get("inbound_rtp", {})
        cp  = telemetry.get("webrtc_stats", {}).get("candidate_pair", {})

        if not camera_active:
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

        power_mode = dh.get("power_mode", "wired")
        trans_v = dh.get("transformer_voltage_vac")
        if power_mode == "battery" or trans_v is None:
            pwr_note = "BATTERY ONLY (Unwired installation; transformer not applicable by customer design)"
            trans_str = "N/A (Battery installation; transformer disconnected by design)"
        else:
            pwr_note = "WIRED WITH CONTINUOUS TRANSFORMER (16-24 VAC)"
            if trans_v == 0 or trans_v == 0.0:
                trans_str = "0.0 V AC [CRITICAL ALERT: COMPLETE POWER LOSS / DISCONNECTED TRANSFORMER]"
            elif trans_v is not None and trans_v < 12:
                trans_str = f"{trans_v} V AC [ALERT: INSUFFICIENT SUBVOLTAGE]"
            else:
                trans_str = f"{trans_v} V AC"

        user_prompt = f"""RING SUPPORT CASE — MULTIMODAL DIAGNOSTIC
- {standby_note}

=== OPENCV VIDEO METRICS ===
- File           : {video_metrics['file']}
- Resolution     : {video_metrics['resolution']}
- FPS            : {video_metrics['fps']}
- Bitrate        : {video_metrics['bitrate_kbps']} kbps
- Sharpness score: {video_metrics['sharpness_score']}
- Video status   : {video_metrics['status']}
- Detected issues: {video_metrics.get('issues', [])}

=== RING DEVICE HEALTH (Ring Partner API) ===
- Power Setup    : {pwr_note}
- Model          : {telemetry.get('device_model', 'N/A')}
- RSSI           : {dh.get('rssi')} dBm
- Wi-Fi SSID     : {dh.get('wifi_name')} | Connected: {dh.get('wifi_is_connected')}
- Battery        : {dh.get('battery_percentage')}%
- Battery saver  : {dh.get('battery_saver_low_voltage')}
- LED status     : {dh.get('led_status')}
- Transformer    : {trans_str}
- Firmware       : {dh.get('firmware')}

=== WEBRTC TRANSPORT METRICS (RTCPeerConnection.getStats — WHEP) ===
- Jitter         : {rtp.get('jitter', 0) * 1000:.1f} ms
- Packet loss    : {rtp.get('packet_loss_fraction', 0) * 100:.1f}%
- Dropped frames : {rtp.get('frames_dropped', 0)} / {rtp.get('frames_received', 0)} received
- WebRTC FPS     : {rtp.get('frames_per_second')}
- Real Bitrate   : {rtp.get('bitrate_kbps')} kbps
- RTT            : {cp.get('current_round_trip_time', 0) * 1000:.1f} ms

=== APPLICABLE OFFICIAL RING SOPS (RAG) ===
{json.dumps(articles, indent=2, ensure_ascii=False)}

{visual_instruction}"""

        print(f"[4/4] Invoking LLM ({self.provider.upper()} / {self.model_id})...")
        raw_response = self._call_llm(user_prompt, image_base64)
        diagnostic_result = self._parse_response(raw_response)

        if not camera_active:
            diagnostic_result["visual_findings"] = [
                "Camera in low-power standby mode. Optical sensor powered down by design."
            ]

        print(f"      Diagnosis: {diagnostic_result.get('diagnostic_status')} | "
              f"Domain: {diagnostic_result.get('root_cause_domain')} | "
              f"Confidence: {diagnostic_result.get('confidence_score')}")

        return {
            "video_metrics":     video_metrics,
            "telemetry":         telemetry,
            "retrieved_articles": articles,
            "diagnostic_result": diagnostic_result,
            "_provider":         self.provider,
            "_model":            self.model_id,
        }


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
        help="Telemetry scenario (used in mock mode). Ignored in LIVE mode.",
    )
    parser.add_argument(
        "--provider",
        type=str,
        default="bedrock",
        choices=["groq", "bedrock"],
        help="LLM provider: 'bedrock' (AWS Bedrock Claude) or 'groq' (development)",
    )
    parser.add_argument(
        "--second",
        type=float,
        default=1.0,
        help="Target timestamp in seconds to extract keyframe from",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=False,
        help="Force mock/simulated mode even if live Ring credentials are configured",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=0,
        help="Ring device index to target if multiple devices exist on account (default: 0)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.video):
        print(f"[ERROR] Video file not found: {args.video}")
        return

    ring_client = RingClient(mock=args.mock, device_index=args.device)
    print(f"\n[Ring] Telemetry operational mode: {ring_client.mode}")
    telemetry = ring_client.get_telemetry(scenario=args.scenario)

    concierge = SupportConcierge(provider=args.provider)
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