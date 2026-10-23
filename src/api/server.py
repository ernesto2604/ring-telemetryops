"""
server.py — Ring TelemetryOps Concierge Agent
==============================================
FastAPI server hosting the Ring Device Digital Twin Simulator and AI Diagnostic Agent.

Endpoints:
  GET  /                        → Serves the Web UI (web/index.html)
  GET  /api/scenarios           → List of predefined telemetry scenarios & descriptions
  GET  /api/kb                  → Ring-informed troubleshooting articles
  GET  /api/thresholds          → Severity table (GREEN / DEGRADED / CRITICAL per metric; project heuristics)
  GET  /api/engine              → Reasoning engine status (Bedrock model, whether AWS credentials are available)
  GET  /favicon.ico             → Page icon (SVG)
  GET  /api/ring/status         → Ring data source and mode (MOCK / LIVE / ERROR)
  GET  /api/ring/devices        → Ring Partner API devices (RING_DATA_SOURCE=partner)
  GET  /api/ring/telemetry      → Ring Partner API device status as telemetry (no video / WebRTC data)
  GET  /api/ring/snapshot       → Last Ring snapshot from memory (Partner mode, Cache-Control: no-store)
  GET  /api/ring/events         → Recent Ring Event History events and reports of new events with a problem
  POST /api/ring/whep-session   → Simulated WHEP answer for the Digital Twin (501 in Partner mode)
  POST /api/telemetry           → Generates telemetry payload (custom sliders or presets)
  POST /api/analyze             → Full diagnostic pipeline: video/canvas + telemetry + LLM
  POST /api/upload-video        → Uploads an MP4 video stream
  GET  /api/samples             → Lists available sample video clips
  GET  /api/metrics             → Analysis funnel counters since server start (in memory, no IDs or images)
  POST /api/feedback            → "Was this helpful?" vote on a report (appended to data/feedback/feedback.jsonl)
  POST /webhooks/ring           → Signed Ring webhooks (HMAC-SHA256, Partner mode only; 200 at once, processed after)
"""

import base64
import hashlib
import hmac
import json
import logging
import sys
import time
import os
from uuid import uuid4
from pathlib import Path
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Literal, Optional, Dict, Any

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from dotenv import load_dotenv
# Read before load_dotenv: a shell variable wins over .env, and the startup line says which one applied.
_SHELL_RING_SOURCE = os.environ.get("RING_DATA_SOURCE")
load_dotenv(ROOT / ".env")

import math


from src.utils.telemetry_simulator import TelemetrySimulator
from src.utils.ring_client import RingClient
from src.utils.ring_partner_client import (
    TOKEN_ENV_VAR,
    RingBlockedError,
    RingForbiddenError,
    RingNotConfiguredError,
    RingNotFoundError,
    RingPartnerClient,
    RingPartnerError,
    RingRateLimitError,
    RingServiceUnavailableError,
    RingTimeRangeNotAuthorizedError,
    RingTimeoutError,
    RingUnauthorizedError,
    DEFAULT_EVENT_TYPES,
    TIME_RANGE_NOT_AUTHORIZED_TEXT,
    SNAPSHOT_WAIT_BUDGET_SECONDS,
    EVENT_TYPES_ENV_VAR,
    first_component_id,
)
from src.utils.partner_telemetry import partner_device_to_telemetry
from src.agent.concierge import SupportConcierge, bedrock_credentials_configured
from src.video_pipeline.analyzer import STREAM_ISSUE_PREFIXES
from src.config import severity
from src.api.funnel import funnel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App & Ring data source (simulator | partner)
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Ring TelemetryOps Concierge",
    description="Ring Device Digital Twin Simulator + Autonomous AI Diagnostic Agent",
    version="1.0.0",
)

RING_SOURCE_ENV_VAR = "RING_DATA_SOURCE"
RING_SOURCES = ("simulator", "partner")
PARTNER_NO_FRAME_REASON = (
    "No video frame available: Ring does not report snapshot support for this device. "
    "This diagnosis uses device telemetry only."
)
E2E_ENCRYPTED_REASON = (
    "Video is end-to-end encrypted: image analysis is not possible; diagnosis uses device status only"
)
# Why a snapshot request produced no image (client status -> visual finding).
SNAPSHOT_REASONS = {
    "no_recent_media": "No recent recording available from Ring: snapshots come from existing recordings",
    "not_ready": "Ring's recording was not ready in time: no image was analyzed",
    "corrupt_recording": "Ring reported a damaged recording, so the image could not be checked.",
    "media_undecryptable": "Ring could not decrypt the latest recording, so the image could not be checked.",
    "unreadable_image": "Ring returned an image this app could not read, so it could not be checked.",
    "time_range_not_authorized": TIME_RANGE_NOT_AUTHORIZED_TEXT,
}
# `_source` of telemetry sent in the request body: set by the server, never taken from the caller.
CLIENT_TELEMETRY_SOURCE = "client_supplied"


def _load_ring_source():
    """
    Selects the Ring data source from RING_DATA_SOURCE only; credentials never switch it.
    Returns (source, ring_client, partner_client, error). `error` is set when the
    selected source cannot run; there is no fallback to the simulator.
    """
    source = os.getenv(RING_SOURCE_ENV_VAR, "").strip().lower() or "simulator"
    # The simulator client never contacts Ring; it backs the Digital Twin routes in every mode.
    simulator = RingClient(mock=True)
    if source == "partner":
        partner = RingPartnerClient()
        if not partner.is_configured:
            return source, simulator, None, f"{TOKEN_ENV_VAR} is not set; Ring Partner API is unavailable."
        return source, simulator, partner, None
    if source != "simulator":
        return source, simulator, None, (
            f"Unsupported {RING_SOURCE_ENV_VAR} value; expected one of: {', '.join(RING_SOURCES)}."
        )
    return source, simulator, None, None


ring_source, ring_client, partner_client, ring_source_error = _load_ring_source()


def _ring_source_origin() -> str:
    """Where RING_DATA_SOURCE came from: the shell, .env or the default (simulator)."""
    if _SHELL_RING_SOURCE is not None:
        origin = "shell environment variable (overrides .env)"
    elif os.getenv(RING_SOURCE_ENV_VAR):
        origin = ".env"
    else:
        origin = "default"
    if os.getenv("PYTHON_DOTENV_DISABLED", "").casefold() in {"1", "true", "t", "yes", "y"}:   # python-dotenv's rule
        origin += "; .env skipped because PYTHON_DOTENV_DISABLED is set"
    return origin


print(f"[Ring] Data source: {ring_source} (from {_ring_source_origin()})", flush=True)


def _ring_mode() -> str:
    """'MOCK', 'LIVE' or 'ERROR' for the selected source (Partner LIVE means configured, not verified)."""
    if ring_source_error:
        return "ERROR"
    if ring_source == "partner":
        return "LIVE"
    return "MOCK"


def _redact(text: str) -> str:
    token = os.getenv(TOKEN_ENV_VAR, "").strip()
    return text.replace(token, "[redacted]") if token else text


def _partner_error_reason(exc: RingPartnerError) -> str:
    """Machine-readable cause of a Partner failure, for the UI hint (never contains the token)."""
    if isinstance(exc, RingNotConfiguredError):
        return "missing_token"
    if isinstance(exc, RingTimeRangeNotAuthorizedError):
        return "time_range_not_authorized"   # a 403 about the time asked for, not the token
    if isinstance(exc, (RingUnauthorizedError, RingForbiddenError)):
        return "token_rejected"
    if isinstance(exc, RingBlockedError):
        return "blocked_406"
    if isinstance(exc, RingRateLimitError):
        return "rate_limited"
    if isinstance(exc, RingServiceUnavailableError):
        return "device_offline_503"
    if isinstance(exc, RingTimeoutError) or exc.status is None:
        return "network_error"   # timeout or connection error: no HTTP answer from Ring
    return "api_error"


def _partner_http_error(exc: RingPartnerError) -> HTTPException:
    """Translates a Ring Partner API error into a safe HTTP error (no token, upstream status kept)."""
    if isinstance(exc, RingRateLimitError):
        status = 429
    elif isinstance(exc, RingServiceUnavailableError):
        status = 503
    elif isinstance(exc, RingTimeoutError):
        status = 504
    elif isinstance(exc, RingNotFoundError):
        status = 404
    else:
        # 401/403 concern this server's credentials, not the caller's request.
        status = 502
    headers = None
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        headers = {"Retry-After": str(math.ceil(retry_after))}
    detail = {
        "source": "ring_partner_api",
        "error": type(exc).__name__,
        "upstream_status": exc.status,
        "reason": _partner_error_reason(exc),
        "message": _redact(str(exc)),
    }
    if retry_after is not None:
        detail["retry_after_seconds"] = math.ceil(retry_after)
    return HTTPException(status_code=status, detail=detail, headers=headers)


def _snapshot_available(devices: list) -> bool:
    """A discovered device lists snapshot support and its video is not end-to-end encrypted."""
    return any(
        (d.get("device_context") or {}).get("snapshot_supported") is True
        and (d.get("device_context") or {}).get("media_readable") is not False
        for d in devices
    )


def _require_partner() -> RingPartnerClient:
    if ring_source != "partner":
        raise HTTPException(
            status_code=409,
            detail=f"Ring Partner API is not the selected data source ({RING_SOURCE_ENV_VAR}={ring_source}).",
        )
    if ring_source_error or partner_client is None:
        raise HTTPException(status_code=503, detail=ring_source_error)
    return partner_client


# Devices from the last discovery (id -> normalized device with name and capabilities), in
# discovery order. Discovery and capabilities are fetched only by discovery; periodic
# refreshes of a known device read GET /v1/devices/{id}/status alone.
_partner_known_devices: Dict[str, Dict[str, Any]] = {}

# Prototype polling defaults: design choices, not Ring recommendations (Ring documents none).
POLL_INTERVAL_ENV_VAR = "RING_PARTNER_POLL_SECONDS"
OFFLINE_ALERT_ENV_VAR = "RING_PARTNER_OFFLINE_ALERT_READS"
OFFLINE_GAP_ENV_VAR = "RING_PARTNER_OFFLINE_ALERT_MAX_GAP_SECONDS"
MAX_OFFLINE_GAP_SECONDS = 86400
DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS, MAX_POLL_SECONDS = 15, 5, 3600
DEFAULT_OFFLINE_ALERT_READS, MAX_OFFLINE_ALERT_READS = 3, 100


def _env_int(name: str, default: int, low: int, high: int) -> int:
    """Integer setting within [low, high]; anything else falls back to the default."""
    try:
        value = int(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if low <= value <= high else default


def _partner_monitoring() -> Dict[str, Any]:
    poll = _env_int(POLL_INTERVAL_ENV_VAR, DEFAULT_POLL_SECONDS, MIN_POLL_SECONDS, MAX_POLL_SECONDS)
    # Default gap tolerates three missed polls; it can never be shorter than one poll interval.
    gap = _env_int(OFFLINE_GAP_ENV_VAR, poll * 4, poll, MAX_OFFLINE_GAP_SECONDS)
    return {
        "mechanism": "polling",          # Event History polling; signed webhooks also trigger analyses
        "status_endpoint": "GET /v1/devices/{device_id}/status",
        "poll_interval_seconds": poll,
        "offline_alert_consecutive_reads": _env_int(
            OFFLINE_ALERT_ENV_VAR, DEFAULT_OFFLINE_ALERT_READS, 1, MAX_OFFLINE_ALERT_READS),
        "offline_alert_max_gap_seconds": gap,
    }


async def _discover_partner_devices(client: RingPartnerClient) -> list:
    """Device discovery (1 + 2N requests); refreshes the known-device list. Raises RingPartnerError."""
    devices = await client.list_devices()
    _partner_known_devices.clear()
    _partner_known_devices.update({d["device_id"]: d for d in devices if d.get("device_id")})
    return devices


async def _partner_devices() -> list:
    client = _require_partner()
    try:
        return await _discover_partner_devices(client)
    except RingPartnerError as exc:
        raise _partner_http_error(exc) from None


async def _partner_telemetry(device_id: Optional[str]) -> Dict[str, Any]:
    """
    Official telemetry for one device: explicit id, then RING_PARTNER_DEVICE_ID, then the
    first discovered device. A known device costs one status request; an unknown one
    triggers discovery first.
    """
    client = _require_partner()
    target = device_id or os.getenv("RING_PARTNER_DEVICE_ID", "").strip() or None
    if target is None and _partner_known_devices:
        target = next(iter(_partner_known_devices))
    known = _partner_known_devices.get(target) if target else None

    if known is None:
        devices = await _partner_devices()  # discovery already includes fresh status
        if not devices:
            raise HTTPException(status_code=404, detail="Ring Partner API returned no devices.")
        if target is None:
            return partner_device_to_telemetry(devices[0])
        for device in devices:
            if device.get("device_id") == target:
                return partner_device_to_telemetry(device)
        raise HTTPException(status_code=404, detail="Requested Ring device was not found for this account.")

    try:
        status = await client.get_status(target, capabilities=known.get("capabilities"))
    except RingNotFoundError as exc:
        _partner_known_devices.pop(target, None)  # removed device: rediscover on the next request
        raise _partner_http_error(exc) from None
    except RingPartnerError as exc:
        # Timeouts / 5xx are API failures, never evidence that the device is offline.
        raise _partner_http_error(exc) from None
    health = {key: value for key, value in status.items() if key != "device_id"}
    # Status just succeeded; a capabilities failure from discovery still stands until rediscovery.
    errors = {k: v for k, v in (known.get("fetch_errors") or {}).items() if k != "status"}
    return partner_device_to_telemetry({**known, "device_health": health, "fetch_errors": errors})


# Directories
SAMPLES_DIR  = ROOT / "data" / "samples"
UPLOADS_DIR  = ROOT / "data" / "uploads"
WEB_DIR      = ROOT / "web"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

# Mount static files and video sample assets
app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
app.mount("/css", StaticFiles(directory=str(WEB_DIR / "css")), name="css")
app.mount("/js",  StaticFiles(directory=str(WEB_DIR / "js")),  name="js")
app.mount("/data/samples", StaticFiles(directory=str(SAMPLES_DIR)), name="samples")
app.mount("/data/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------

class TelemetryRequest(BaseModel):
    """Parameters for generating telemetry. Mode 'preset' loads predefined scenario.
    Mode 'custom' reads user-adjusted hardware and network parameters from sliders."""
    mode: str = "preset"                        # "preset" | "custom"
    scenario: Optional[str] = "wifi_degradation"

    # Device info
    device_id: Optional[str]    = "a1b2c3d4e5f6"
    device_model: Optional[str] = "Ring Video Doorbell 4"
    wifi_ssid: Optional[str]    = "HomeNetwork_2G"
    firmware: Optional[str]     = "2.4.18"

    # Custom: device_health parameters
    power_mode: Optional[str]                 = "wired"
    rssi: Optional[int]                       = -48
    battery_percentage: Optional[int]         = 100
    battery_saver_low_voltage: Optional[bool] = False
    transformer_voltage_vac: Optional[float]  = None
    wifi_is_connected: Optional[bool]         = True
    led_status: Optional[str]                 = "on"

    # Custom: webrtc parameters
    jitter_ms: Optional[float]                = 2.0
    packet_loss_pct: Optional[float]          = 0.0
    # The Digital Twin has no dropped-frame slider: unless a caller sends them, these are not simulated
    # (None, with the reason in measurement_status) instead of a fixed 0 / 900 placeholder.
    frames_dropped: Optional[int]             = None
    frames_received: Optional[int]            = None
    frames_per_second: Optional[float]        = 30.0
    bitrate_kbps: Optional[int]               = 1200
    rtt_ms: Optional[float]                   = 18.0


# ~6 MB of JPEG; the canvas sends a 720p JPEG of a few hundred KB.
MAX_FRAME_BASE64_CHARS = 8 * 1024 * 1024


class AnalyzeRequest(BaseModel):
    video_path: Optional[str] = None          # File name under samples/ or uploads/
    # Current frame in Base64 (Digital Twin canvas). Bounded so one request cannot exhaust memory.
    frame_base64: Optional[str] = Field(default=None, max_length=MAX_FRAME_BASE64_CHARS)
    telemetry: Optional[Dict[str, Any]] = None  # Complete telemetry payload
    keyframe_second: float = 1.0
    camera_active: bool = False               # Whether camera is active or in low-power standby
    source_mode: str = "auto"                # "auto" | "twin" | "ring"
    device_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_custom_telemetry(req: TelemetryRequest) -> Dict[str, Any]:
    """Constructs a telemetry payload using custom slider parameters from the UI."""
    from src.utils.telemetry_simulator import _utcnow
    now = _utcnow()
    not_simulated = "not simulated by the Digital Twin sliders"
    frame_status = {name: not_simulated for name, value in
                    (("frames_dropped", req.frames_dropped), ("frames_received", req.frames_received))
                    if value is None}

    telemetry = {
        "scenario": "custom",
        "captured_at": now,
        "device_id": req.device_id,
        "device_model": req.device_model,
        "device_health": {
            "power_mode": req.power_mode,
            "rssi": req.rssi,
            "latest_signal_strength": req.rssi,
            "wifi_name": req.wifi_ssid,
            "wifi_is_connected": req.wifi_is_connected,
            "network_connection_type": "wifi",
            "battery_percentage": req.battery_percentage,
            "battery_saver_low_voltage": req.battery_saver_low_voltage,
            "led_status": req.led_status,
            "firmware": req.firmware,
            "transformer_voltage_vac": req.transformer_voltage_vac,
            "updated_at": now,
        },
        "webrtc_stats": {
            "inbound_rtp": {
                "kind": "video",
                "jitter": req.jitter_ms / 1000.0,
                "packets_lost": (int((req.packet_loss_pct / 100.0) * req.frames_received)
                                 if req.frames_received is not None else None),
                "packet_loss_fraction": req.packet_loss_pct / 100.0,
                "frames_dropped": req.frames_dropped,
                "frames_received": req.frames_received,
                "frames_per_second": req.frames_per_second,
                "bytes_received": int(req.bitrate_kbps * 30 * 1000 / 8),
                "bitrate_kbps": req.bitrate_kbps,
            },
            "candidate_pair": {
                "current_round_trip_time": req.rtt_ms / 1000.0,
                "available_outgoing_bitrate": req.bitrate_kbps * 1000,
            },
        },
    }
    if frame_status:
        telemetry["measurement_status"] = {"webrtc_stats": frame_status}
    return telemetry


def _resolve_video_path(video_path: str) -> Path:
    """
    Resolves a video by file name inside samples/ or uploads/ only. Directory parts of the
    request ("data/samples/x.mp4") are ignored, so absolute or "../" paths cannot reach other files.
    """
    name = Path(video_path.replace("\\", "/")).name
    if name and name not in (".", ".."):
        for base in (SAMPLES_DIR, UPLOADS_DIR):
            candidate = (base / name).resolve()
            if candidate.parent == base.resolve() and candidate.is_file():
                return candidate
    raise FileNotFoundError("Video file not found in the samples or uploads directories.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_ui():
    """Serves the interactive Digital Twin control dashboard."""
    index = WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Web UI not found.")
    return FileResponse(str(index))


@app.get("/api/scenarios")
async def get_scenarios():
    """Lists supported telemetry scenarios and technical descriptions."""
    return {
        "scenarios": list(TelemetrySimulator.SUPPORTED_SCENARIOS),
        "descriptions": {
            "nominal":            "Nominal device health and network transport. All parameters optimal.",
            "wifi_degradation":   "Critical RSSI (-84 dBm), 18% packet loss, 85 ms WebRTC jitter.",
            "optical_physical":   "Optimal network transport, obstructed lens or inactive IR LEDs.",
            "power_issue":        "Critical battery (4%) and transformer subvoltage (10.2 V AC).",
            "pink_purple_tint":   "Possible stuck mechanical IR-cut filter: pink/purple tint in daylight.",
            "night_vision_glare": "IR LED reflection off adjacent wall (white haze/fog halo).",
            "cold_battery_drain": "Freezing temperature (< 0°C) blocking Li-ion charge with severe drain.",
            "live_view_timeout":  "WebRTC connection timeout / ICE failure caused by firewall or VPN.",
            "audio_delay_jitter": "Bidirectional audio lag and robotic distortion caused by elevated jitter.",
            "motion_delayed":     "Delayed PIR trigger or incorrect doorbell mounting height/angle.",
            "solar_charging":     "Connected solar panel: idle by design when battery is above 90%.",
            "poe_loss":           "Ring Doorbell Elite: incompatible passive PoE or damaged data pair.",
            "in_home_chime":       "In-home mechanical chime failure due to transformer voltage sag (<16V AC).",
            "whep_session_failure": "Live View WHEP: SDP negotiation failure or stream blocked by active E2EE.",
        },
    }


@app.get("/api/kb")
async def get_kb_articles():
    """Returns Ring-informed troubleshooting procedures with evidence metadata."""
    from src.agent.knowledge_retriever import KnowledgeRetriever
    retriever = KnowledgeRetriever()
    return {"articles": retriever.kb_data}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(str(WEB_DIR / "favicon.svg"), media_type="image/svg+xml")


@app.get("/api/engine")
async def get_engine():
    """Which engine diagnoses anomalies: Bedrock when AWS credentials resolve, else the local fallback mock-up."""
    return {
        "primary": "bedrock",
        "model": SupportConcierge.default_model_id(),
        "credentials_configured": bedrock_credentials_configured(),
        "fallback": "local_fallback",
    }


@app.get("/api/thresholds")
async def get_thresholds():
    """The severity table the backend uses; the web UI reads its levels from here (no copies)."""
    return severity.TABLE


@app.get("/api/samples")
async def get_samples():
    """Lists available sample video streams and uploads."""
    def videos(directory: Path, prefix: str) -> list:
        # One pass with a case-insensitive suffix check: separate "*.mp4" / "*.MP4" globs list
        # each file twice on case-insensitive filesystems (Windows, macOS).
        return [
            {"name": f.name, "path": f"{prefix}/{f.name}", "size_kb": round(f.stat().st_size / 1024, 1)}
            for f in sorted(directory.iterdir())
            if f.is_file() and f.suffix.lower() in (".mp4", ".mov")
        ]

    return {"samples": videos(SAMPLES_DIR, "data/samples"), "uploads": videos(UPLOADS_DIR, "data/uploads")}


@app.get("/api/ring/status")
async def get_ring_status():
    """
    Returns the selected Ring data source, operational mode (MOCK / LIVE / ERROR),
    and discovered device information.
    """
    if ring_source_error:
        reason = "missing_token" if ring_source == "partner" else "invalid_source"
        return {"source": ring_source, "mode": "ERROR", "is_live": False,
                "error": ring_source_error, "error_reason": reason, "device_count": 0, "devices": []}
    if ring_source == "partner":
        try:
            devices = await _discover_partner_devices(partner_client)
        except RingPartnerError as exc:
            # Report the failure as the state; never substitute simulated devices.
            error = _partner_http_error(exc).detail
            return {"source": ring_source, "mode": "ERROR", "is_live": False,
                    "error": error, "error_reason": error["reason"], "device_count": 0, "devices": []}
        return {
            "source": ring_source,
            "mode": "LIVE",
            "is_live": True,
            "error": None,
            "error_reason": None,
            "device_count": len(devices),
            "devices": devices,
            "features": {"telemetry": True, "snapshot": _snapshot_available(devices), "whep": False,
                         "webhooks": bool(os.getenv(WEBHOOK_KEY_ENV_VAR, "").strip())},
            "monitoring": _partner_monitoring(),
        }

    devices = ring_client.list_devices()
    return {
        "source": ring_source,
        "mode": ring_client.mode,
        "is_live": ring_client.is_live,
        "error": ring_client.live_error,
        "device_count": len(devices),
        "devices": devices,
    }


@app.get("/api/ring/devices")
async def get_ring_devices():
    """Lists Ring Partner API devices with status, capabilities and per-device fetch errors."""
    devices = await _partner_devices()
    return {"source": "ring_partner_api", "device_count": len(devices), "devices": devices}


@app.get("/api/ring/telemetry")
async def get_ring_telemetry(device_id: Optional[str] = None):
    """Ring Partner API device status as telemetry, or an explicit error (never simulated data)."""
    if ring_source == "partner":
        return await _partner_telemetry(device_id)
    if ring_source_error:
        raise HTTPException(status_code=503, detail=ring_source_error)
    raise HTTPException(
        status_code=503,
        detail=(
            f"Ring Partner API is not the selected data source ({RING_SOURCE_ENV_VAR}={ring_source}); "
            "use the Digital Twin telemetry endpoint."
        ),
    )


@app.post("/api/ring/whep-session")
async def create_ring_whep_session(device_id: Optional[str] = None):
    """
    Digital Twin only: returns a simulated WHEP answer labelled `simulated`. Ring Partner API
    live streaming is not implemented, so Partner and error modes get HTTP 501.
    """
    if ring_source == "partner" or ring_source_error:
        # The Partner token is not used for WHEP; a simulated SDP answer would fake a live stream.
        raise HTTPException(
            status_code=501,
            detail=f"Live view (WHEP) is not available for {RING_SOURCE_ENV_VAR}={ring_source}.",
        )
    return await ring_client.create_whep_session(
        device_id=device_id or ring_client.list_devices()[0]["id"],
        sdp_offer="",
    )


@app.post("/api/telemetry")
async def generate_telemetry(req: TelemetryRequest):
    """
    Generate telemetry payload.
    - mode='preset': Digital Twin scenario from the simulator (`_source="mock"`)
    - mode='custom': user-adjusted parameters (sliders)
    """
    if req.mode == "preset":
        if req.scenario not in TelemetrySimulator.SUPPORTED_SCENARIOS:
            raise HTTPException(status_code=400, detail=f"Invalid scenario: '{req.scenario}'.")
        return ring_client.get_telemetry(scenario=req.scenario)
    else:
        return _build_custom_telemetry(req)


# Last Ring snapshot, in memory only (never written to disk or logged); served by /api/ring/snapshot.
_latest_snapshot: Optional[Dict[str, Any]] = None


def _snapshot_guard(telemetry: Dict[str, Any]) -> Optional[str]:
    """Why no snapshot is requested (end-to-end encrypted, or no snapshot support), else None."""
    context = telemetry.get("device_context") or {}
    if context.get("media_readable") is False:
        return E2E_ENCRYPTED_REASON
    if context.get("snapshot_supported") is not True:
        return PARTNER_NO_FRAME_REASON
    return None


async def _download_snapshot(telemetry: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    """Client result; credential errors raise as HTTP errors (T21), other Ring errors become status "error"."""
    try:
        return await partner_client.download_snapshot(
            telemetry["device_id"], component_id=first_component_id(telemetry.get("capabilities")), **kwargs)
    except (RingUnauthorizedError, RingForbiddenError, RingBlockedError, RingNotConfiguredError) as exc:
        raise _partner_http_error(exc) from None
    except RingPartnerError as exc:
        return {"status": "error",
                "reason": f"Ring could not provide a snapshot ({_partner_error_reason(exc)}): no image was analyzed"}


def _snapshot_outcome(telemetry: Dict[str, Any], result: Dict[str, Any]) -> tuple:
    """(frame_base64, no_frame_reason, snapshot_meta); an image is kept in memory for /api/ring/snapshot."""
    global _latest_snapshot
    funnel.record_snapshot(result["status"])
    if result["status"] == "error":
        return None, result["reason"], None
    if result["status"] != "ok":
        return None, SNAPSHOT_REASONS[result["status"]], None
    meta = {"device_id": telemetry["device_id"], "media_timestamp": result["media_timestamp"],
            "media_origin": result["media_origin"], "content_type": result["content_type"]}
    _latest_snapshot = {**meta, "image": result["image"]}
    return base64.b64encode(result["image"]).decode("ascii"), None, meta


async def _partner_snapshot(telemetry: Dict[str, Any], at_timestamp_ms: Optional[int] = None) -> tuple:
    """
    (frame_base64, no_frame_reason, snapshot_meta) for one Partner device. No request when the
    video is end-to-end encrypted or the device does not list snapshot support. Credential errors
    raise as HTTP errors (T21 reasons); other failures leave a telemetry-only diagnosis.
    """
    reason = _snapshot_guard(telemetry)
    if reason:
        return None, reason, None
    result = await _download_snapshot(telemetry, at_timestamp_ms=at_timestamp_ms,
                                      wait_for_recording=at_timestamp_ms is not None)
    return _snapshot_outcome(telemetry, result)


# Analyze now asks for the recording of the newest known events first: Ring's "latest image of the
# last 24 h" can point at a recording it cannot decrypt (422 GRECO_NO_VALID_KEY, seen live).
ANALYZE_NOW_EVENT_ATTEMPTS = 3
ANALYZE_NOW_BUDGET_SECONDS = 20.0


async def _recent_event_starts(device_id: str) -> list:
    """Start times (epoch ms) of the device's known events, newest first; one Ring query when none are known."""
    events = _partner_events if _events_poll.get("device_id") == device_id else []
    if not events:
        event_types = os.getenv(EVENT_TYPES_ENV_VAR, "").strip() or DEFAULT_EVENT_TYPES
        try:
            events = await partner_client.list_events(device_id, event_types, max_pages=1)
        except (RingUnauthorizedError, RingForbiddenError, RingBlockedError, RingNotConfiguredError) as exc:
            raise _partner_http_error(exc) from None
        except RingPartnerError as exc:
            logger.info("Ring events unavailable for Analyze now (%s); using the latest image", type(exc).__name__)
            events = []
    return sorted({e["start"] for e in events if isinstance(e.get("start"), int)}, reverse=True)


async def _analyze_now_snapshot(telemetry: Dict[str, Any]) -> tuple:
    """
    Analyze now: the snapshot at the start of the newest event, then of up to two older ones when
    Ring answers without a usable image (416, 422, unreadable bytes), within ANALYZE_NOW_BUDGET_SECONDS.
    Ring errors (timeouts, 429, 5xx) stop at once; credential errors raise (T21). With no event,
    or when every event fails, the latest image of the last 24 h is the last resort.
    """
    reason = _snapshot_guard(telemetry)
    if reason:
        return None, reason, None
    deadline = time.monotonic() + ANALYZE_NOW_BUDGET_SECONDS

    def budget() -> float:
        return max(0.0, min(SNAPSHOT_WAIT_BUDGET_SECONDS, deadline - time.monotonic()))

    for start in (await _recent_event_starts(telemetry["device_id"]))[:ANALYZE_NOW_EVENT_ATTEMPTS]:
        if budget() <= 0:
            break
        result = await _download_snapshot(telemetry, at_timestamp_ms=start, wait_budget_seconds=budget())
        if result["status"] in ("ok", "error"):
            return _snapshot_outcome(telemetry, result)
    return _snapshot_outcome(telemetry, await _download_snapshot(telemetry, wait_budget_seconds=budget()))


@app.get("/api/ring/snapshot")
async def get_ring_snapshot():
    """The last Ring snapshot from memory (never cached by the browser), or 404."""
    if _latest_snapshot is None:
        raise HTTPException(status_code=404, detail="No Ring snapshot in memory yet: run Analyze now.")
    headers = {"Cache-Control": "no-store"}
    if _latest_snapshot["media_timestamp"] is not None:
        headers["X-Media-Timestamp"] = str(_latest_snapshot["media_timestamp"])
    if _latest_snapshot["media_origin"]:
        headers["X-Media-Origin"] = _latest_snapshot["media_origin"]
    return Response(content=_latest_snapshot["image"], media_type=_latest_snapshot["content_type"], headers=headers)


# Event History polling (webhooks are not implemented). In memory: the last 20 events, the IDs
# already seen, and the query pacing. Events that started before this server did are listed but
# never analyzed, so a restart does not replay the history.
MAX_RECENT_EVENTS = 20
_SERVER_STARTED_MS = int(time.time() * 1000)
_partner_events: list = []
_seen_event_ids: set = set()
_events_poll = {"last_query": None, "paused_until": 0.0, "device_id": None}
# Event type -> UI trigger key (the Digital Twin's Ding / PIR / Live View names).
EVENT_TRIGGERS = {"ding": "ding", "motion": "motion", "on_demand": "live"}


PARTNER_NOT_SHARED = "Ring does not share Wi-Fi or power data with partner apps"
PARTNER_IMAGE_OK_MITIGATION = (
    "The camera image looks fine. Ring doesn't share Wi-Fi or battery data with this app: "
    "check them in the Ring app under Device Health."
)
PARTNER_NO_IMAGE_MITIGATION = (
    "Ring doesn't share Wi-Fi or battery data with this app, and no recent recording was available to "
    "check the image. Check Device Health in the Ring app."
)
PARTNER_DEVICE_HEALTH_HINT = (
    "Ring doesn't share Wi-Fi or battery data with this app: check them in the Ring app under Device Health."
)
# Image-detail text -> mitigation, so the mitigation names the same cause as the visual finding.
PARTNER_CAUSE_MITIGATIONS = {
    SNAPSHOT_REASONS[cause]: f"{SNAPSHOT_REASONS[cause]} {PARTNER_DEVICE_HEALTH_HINT}"
    for cause in ("media_undecryptable", "corrupt_recording", "unreadable_image")
}


def _finish_partner_report(result: Dict[str, Any], snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Partner reports: which domains were checked (image / network / power) and, when the verdict is
    INSUFFICIENT_DATA, customer wording for the mitigation. diagnostic_status is never changed.
    """
    issues = [i for i in (result.get("video_metrics") or {}).get("issues") or []
              if not i.startswith(STREAM_ISSUE_PREFIXES)]
    if snapshot is None:
        image = {"domain": "image", "status": "not_available",
                 "detail": (result.get("video_analysis") or {}).get("reason") or "No Ring image was analyzed"}
    elif issues:
        image = {"domain": "image", "status": "issue", "detail": "; ".join(issues)}
    else:
        image = {"domain": "image", "status": "ok", "detail": "Ring snapshot checked: no image issue detected"}
    result["checked_domains"] = [
        image,
        {"domain": "network", "status": "not_available", "detail": PARTNER_NOT_SHARED},
        {"domain": "power", "status": "not_available", "detail": PARTNER_NOT_SHARED},
    ]
    if snapshot is not None:
        result["snapshot"] = snapshot
    diag = result.get("diagnostic_result") or {}
    if diag.get("diagnostic_status") == "INSUFFICIENT_DATA":
        diag["recommended_mitigation"] = (
            PARTNER_IMAGE_OK_MITIGATION if image["status"] == "ok"
            else PARTNER_CAUSE_MITIGATIONS.get(image["detail"], PARTNER_NO_IMAGE_MITIGATION))
    return result


# The last reports (id -> engine, severity, domain, voted), in memory only, to validate feedback votes.
MAX_FEEDBACK_REPORTS = 100
_recent_reports: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()


def _run_concierge(**kwargs: Any) -> Dict[str, Any]:
    """
    One concierge analysis, counted in the funnel metrics with its latency (simulator and Partner),
    with a server-generated report_id that a feedback vote can refer to.
    """
    started = time.perf_counter()
    result = SupportConcierge().process_stream(**kwargs)
    funnel.record_analysis(result, time.perf_counter() - started)
    diag = result.get("diagnostic_result") or {}
    report_id = str(uuid4())
    _recent_reports[report_id] = {"engine": result.get("_provider"), "severity": diag.get("diagnostic_status"),
                                  "domain": diag.get("root_cause_domain"), "voted": False}
    while len(_recent_reports) > MAX_FEEDBACK_REPORTS:
        _recent_reports.popitem(last=False)
    result["report_id"] = report_id
    return result


def _run_partner_concierge(telemetry: Dict[str, Any], frame_base64: Optional[str],
                           no_frame_reason: Optional[str], snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    result = _run_concierge(
        video_path=None, telemetry=telemetry, frame_base64=frame_base64, camera_active=True,
        no_frame_reason=no_frame_reason, ring_media=snapshot is not None,
    )
    return _finish_partner_report(result, snapshot)


async def _analyze_partner_event(device_id: str, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Analysis of one new event with the snapshot at its start; None when it fails (logged, no frame data)."""
    try:
        telemetry = await _partner_telemetry(device_id)
        frame, reason, snapshot = await _partner_snapshot(telemetry, at_timestamp_ms=event["start"])
        return _run_partner_concierge(telemetry, frame, reason, snapshot)
    except Exception as exc:  # one failed event never stops the poll
        logger.warning("Ring event analysis failed (%s)", type(exc).__name__)
        return None


@app.get("/api/ring/events")
async def get_ring_events():
    """
    Recent Ring events (Event History polling, at most one Ring query per poll interval) and the
    reports of new events with a problem. A new event (unseen ID, started after this server) is
    analyzed at once with the snapshot at its start; a healthy result is not returned (silence).
    """
    client = _require_partner()
    now = time.monotonic()
    if now < _events_poll["paused_until"]:
        wait = math.ceil(_events_poll["paused_until"] - now)
        raise HTTPException(status_code=429, headers={"Retry-After": str(wait)},
                            detail={"source": "ring_partner_api", "reason": "rate_limited",
                                    "retry_after_seconds": wait, "message": "Ring asked to wait (Retry-After)."})
    poll_seconds = _partner_monitoring()["poll_interval_seconds"]
    # Webhook reports are handed over on every call, also when Ring is not queried this cycle.
    reports = _webhook_reports[:]
    _webhook_reports.clear()
    last = _events_poll["last_query"]
    if last is None or now - last >= poll_seconds:
        _events_poll["last_query"] = now
        target = os.getenv("RING_PARTNER_DEVICE_ID", "").strip() or next(iter(_partner_known_devices), None)
        if target is None:
            devices = await _partner_devices()
            target = devices[0]["device_id"] if devices else None
        if target is None:
            return {"source": "ring_partner_api", "events": [], "reports": reports}
        event_types = os.getenv(EVENT_TYPES_ENV_VAR, "").strip() or DEFAULT_EVENT_TYPES
        try:
            events = await client.list_events(target, event_types)
            _events_poll["device_id"] = target
        except RingRateLimitError as exc:
            _events_poll["paused_until"] = now + (exc.retry_after if exc.retry_after is not None else poll_seconds)
            raise _partner_http_error(exc) from None
        except RingPartnerError as exc:
            raise _partner_http_error(exc) from None
        new_events = []
        for event in reversed(events):  # oldest first
            if not event["id"] or event["id"] in _seen_event_ids:
                continue
            _seen_event_ids.add(event["id"])
            _partner_events.insert(0, event)
            # An event already analyzed from a webhook (same device, type and start ±10 s) is not analyzed again.
            if (event["start"] is not None and event["start"] > _SERVER_STARTED_MS
                    and _claim_event(target, event["event_type"], event["start"])):
                new_events.append(event)
                funnel.count("events_seen")
        del _partner_events[MAX_RECENT_EVENTS:]
        for event in new_events:
            result = await _analyze_partner_event(target, event)
            status = ((result or {}).get("diagnostic_result") or {}).get("diagnostic_status")
            if status in ("DEGRADED", "CRITICAL"):
                reports.append({"event": event, "trigger": EVENT_TRIGGERS.get(event["event_type"]), **result})
    return {"source": "ring_partner_api", "events": list(_partner_events), "reports": reports}


# Analyses started by Event History or by webhooks: (device_id, event_type, start_ms), the last 1000.
EVENT_MATCH_TOLERANCE_MS = 10_000
_analyzed_event_keys: deque = deque(maxlen=1000)


def _claim_event(device_id: str, event_type: Optional[str], start_ms: int) -> bool:
    """True the first time an event is seen; False when Event History or a webhook already claimed it."""
    if any(d == device_id and t == event_type and abs(s - start_ms) <= EVENT_MATCH_TOLERANCE_MS
           for d, t, s in _analyzed_event_keys):
        return False
    _analyzed_event_keys.append((device_id, event_type, start_ms))
    return True


# ---------------------------------------------------------------------------
# Ring webhooks (signed with the app's HMAC key; Partner mode only)
# ---------------------------------------------------------------------------

WEBHOOK_KEY_ENV_VAR = "RING_PARTNER_HMAC_KEY"
WEBHOOK_SKIP_ENV_VAR = "RING_WEBHOOK_SKIP_SUBTYPES"
WEBHOOK_MAX_BODY_ENV_VAR = "RING_WEBHOOK_MAX_BODY_BYTES"
DEFAULT_WEBHOOK_MAX_BODY_BYTES = 64 * 1024
# Webhook type -> Event History event type (the same analysis and UI trigger as a polled event).
WEBHOOK_ANALYZED_TYPES = {"motion_detected": "motion", "button_press": "ding"}
WEBHOOK_DEVICE_TYPES = ("device_online", "device_offline", "device_added", "device_removed")
_webhook_request_ids: deque = deque(maxlen=1000)
# Webhook state in memory: deliveries per type and the last device_* type per device.
_webhook_state: Dict[str, Any] = {"received": {}, "devices": {}}
# Reports of webhook events with a problem, handed to the UI by the next GET /api/ring/events.
_webhook_reports: list = []


def _webhook_signature_ok(body: bytes, header: Optional[str], key: str) -> bool:
    """X-Signature: sha256=<hex HMAC-SHA256 of the raw body>, compared in constant time."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


async def _read_limited_body(request: Request, limit: int) -> bytes:
    """The raw body, or 413 as soon as it exceeds `limit` bytes (checked while reading, not after)."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        raise HTTPException(status_code=413, detail="Webhook body too large.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail="Webhook body too large.")
    return bytes(body)


async def _process_webhook_event(device_id: str, event: Dict[str, Any]) -> None:
    """Background analysis of a webhook event, as for a new Event History event: silence when healthy."""
    result = await _analyze_partner_event(device_id, event)
    status = ((result or {}).get("diagnostic_result") or {}).get("diagnostic_status")
    if status in ("DEGRADED", "CRITICAL"):
        _webhook_reports.append({"event": event, "trigger": EVENT_TRIGGERS.get(event["event_type"]), **result})
        del _webhook_reports[:-MAX_RECENT_EVENTS]


@app.post("/webhooks/ring", include_in_schema=False)
async def ring_webhook(request: Request, background: BackgroundTasks):
    """
    Ring webhook receiver. The signature is checked on the raw body before any parsing; duplicates
    (meta.request_id) and unknown types answer 200 without work. Ring expects 2xx within 5 s, so
    motion_detected / button_press are analyzed after the response (BackgroundTasks).
    """
    if ring_source != "partner":
        raise HTTPException(status_code=404, detail="Not Found")
    key = os.getenv(WEBHOOK_KEY_ENV_VAR, "").strip()
    if not key:
        raise HTTPException(status_code=503, detail=f"{WEBHOOK_KEY_ENV_VAR} is not set: webhooks cannot be verified.")
    limit = _env_int(WEBHOOK_MAX_BODY_ENV_VAR, DEFAULT_WEBHOOK_MAX_BODY_BYTES, 1024, 1024 * 1024)
    body = await _read_limited_body(request, limit)
    if not _webhook_signature_ok(body, request.headers.get("x-signature"), key):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed JSON.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Malformed JSON.")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}

    request_id = meta.get("request_id")
    if isinstance(request_id, str) and request_id:
        if request_id in _webhook_request_ids:
            return {"status": "duplicate"}
        _webhook_request_ids.append(request_id)

    kind = data.get("type")
    if kind not in WEBHOOK_ANALYZED_TYPES and kind not in WEBHOOK_DEVICE_TYPES:
        return {"status": "ignored"}        # a new Ring type must never break the receiver
    _webhook_state["received"][kind] = _webhook_state["received"].get(kind, 0) + 1
    device_id = attrs.get("source") if isinstance(attrs.get("source"), str) else None
    if kind in WEBHOOK_DEVICE_TYPES:
        if device_id:
            _webhook_state["devices"][device_id] = kind
        return {"status": "accepted"}

    timestamp = attrs.get("timestamp")
    if not device_id or not isinstance(timestamp, int) or isinstance(timestamp, bool):
        return {"status": "ignored"}
    event_type = WEBHOOK_ANALYZED_TYPES[kind]
    sub_type = attrs.get("sub_type") if isinstance(attrs.get("sub_type"), str) else None
    # A skipped event is claimed too, so Event History does not analyze it later either.
    if not _claim_event(device_id, event_type, timestamp):
        return {"status": "already_analyzed"}
    funnel.count("events_seen")
    skip = {x.strip().lower() for x in os.getenv(WEBHOOK_SKIP_ENV_VAR, "").split(",") if x.strip()}
    if kind == "motion_detected" and sub_type and sub_type.lower() in skip:
        return {"status": "skipped"}        # Stage 0 of Ring's vision funnel: cheap sub_type filter
    event = {"id": data.get("id"), "event_type": event_type, "sub_type": sub_type, "start": timestamp,
             "origin": "webhook"}
    background.add_task(_process_webhook_event, device_id, event)
    return {"status": "accepted"}


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    """Full diagnostic pipeline: video/canvas keyframe + telemetry + LLM -> diagnostic report."""
    video_path = None
    frame_base64 = req.frame_base64
    # Caller telemetry keeps its values but never its origin label (no spoofed "ring_partner_api").
    telemetry = {**req.telemetry, "_source": CLIENT_TELEMETRY_SOURCE} if req.telemetry else None
    camera_active = req.camera_active
    no_frame_reason = None

    # In Partner mode "auto" always means the official API: a caller frame or telemetry must not
    # skip it. Only an explicit "twin" request analyzes caller data there.
    wants_live = req.source_mode == "ring" or (
        req.source_mode == "auto" and _ring_mode() != "MOCK"
        and (ring_source == "partner" or not frame_base64)
    )
    if wants_live and ring_source_error:
        raise HTTPException(status_code=503, detail=ring_source_error)
    snapshot = None
    partner_analysis = wants_live and ring_source == "partner"
    if partner_analysis:
        # Always official telemetry; caller-supplied telemetry is not presented as Ring data.
        telemetry = await _partner_telemetry(req.device_id)
        # Only a Ring snapshot is a Ring camera image: the caller's frame and camera_active are ignored.
        frame_base64, no_frame_reason, snapshot = await _analyze_now_snapshot(telemetry)
        camera_active = True
    elif wants_live:
        # Ring data was requested but Partner is not the selected source: explicit error, no Twin fallback.
        raise HTTPException(
            status_code=503,
            detail=f"Ring Partner API is not the selected data source ({RING_SOURCE_ENV_VAR}={ring_source}).",
        )

    if not frame_base64 and not no_frame_reason:
        if not req.video_path:
            raise HTTPException(status_code=400, detail="Either 'frame_base64' or 'video_path' is required.")
        try:
            video_path = str(_resolve_video_path(req.video_path))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from None

    try:
        result = _run_concierge(
            video_path=video_path,
            telemetry=telemetry,
            keyframe_second=req.keyframe_second,
            frame_base64=frame_base64,
            camera_active=camera_active,
            no_frame_reason=no_frame_reason,
            ring_media=snapshot is not None,
        )
        if partner_analysis:
            result = _finish_partner_report(result, snapshot)
        return result
    except ValueError:
        # Undecodable frame or unreadable video; details (paths) stay in the server log.
        logger.warning("Analysis input could not be decoded", exc_info=True)
        raise HTTPException(status_code=422, detail="The video frame or file could not be decoded.") from None
    except Exception:
        logger.exception("Diagnostic pipeline failed")
        raise HTTPException(status_code=500, detail="Internal error while running the diagnosis.") from None


@app.get("/api/metrics")
async def get_metrics():
    """Analysis funnel counters and latencies since server start (memory only; no device IDs, images or text)."""
    return funnel.snapshot()


FEEDBACK_FILE = ROOT / "data" / "feedback" / "feedback.jsonl"
FEEDBACK_COMMENT_MAX_CHARS = 200
FEEDBACK_RATE_LIMIT = 30          # votes per minute, for the whole server
_feedback_times: deque = deque()


class FeedbackRequest(BaseModel):
    report_id: str = Field(max_length=64)
    rating: Literal["up", "down"]
    # Longer comments are cut to FEEDBACK_COMMENT_MAX_CHARS; this bound only stops oversized bodies.
    comment: Optional[str] = Field(default=None, max_length=2000)


@app.post("/api/feedback")
async def post_feedback(req: FeedbackRequest):
    """
    One "Was this helpful?" vote per report. Appends one JSON line (time, report_id, engine, severity,
    domain, rating, comment) to data/feedback/feedback.jsonl: no image, device ID or IP address.
    """
    now = time.monotonic()
    while _feedback_times and now - _feedback_times[0] >= 60:
        _feedback_times.popleft()
    if len(_feedback_times) >= FEEDBACK_RATE_LIMIT:
        raise HTTPException(status_code=429, headers={"Retry-After": "60"},
                            detail="Too many feedback votes; try again in a minute.")
    report = _recent_reports.get(req.report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Unknown or expired report_id.")
    if report["voted"]:
        raise HTTPException(status_code=409, detail="This report already has a vote.")
    comment = " ".join((req.comment or "").split())[:FEEDBACK_COMMENT_MAX_CHARS] or None
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "report_id": req.report_id,
            "engine": report["engine"], "severity": report["severity"], "domain": report["domain"],
            "rating": req.rating, "comment": comment}
    try:
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Feedback could not be saved (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Feedback could not be saved on the server; try again later.") from None
    _feedback_times.append(now)
    report["voted"] = True
    funnel.count("feedback_up" if req.rating == "up" else "feedback_down")
    return {"ok": True, "report_id": req.report_id, "rating": req.rating}


@app.post("/api/upload-video")
async def upload_video(file: UploadFile = File(...)):
    """Uploads an MP4/MOV video stream file to the server."""
    original_name = Path(file.filename or "").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in (".mp4", ".mov", ".avi"):
        raise HTTPException(status_code=400, detail="Only .mp4, .mov, or .avi formats are accepted.")
    dest = UPLOADS_DIR / f"{uuid4().hex}{suffix}"
    max_size = 100 * 1024 * 1024
    size = 0
    try:
        with open(str(dest), "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_size:
                    raise HTTPException(status_code=413, detail="Video file exceeds the 100 MB limit.")
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {
        "ok": True,
        "filename": original_name,
        "path": f"data/uploads/{dest.name}",
        "size_kb": round(size / 1024, 1),
    }


# ---------------------------------------------------------------------------
# Direct Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    # Local demo: loopback only and no auto-reload. Expose it deliberately (--host) if needed.
    uvicorn.run(
        "src.api.server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
    )
