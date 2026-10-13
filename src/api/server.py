"""
server.py — Ring TelemetryOps Concierge Agent
==============================================
FastAPI server hosting the Ring Device Digital Twin Simulator and AI Diagnostic Agent.

Endpoints:
  GET  /                        → Serves the Web UI (web/index.html)
  GET  /api/scenarios           → List of predefined telemetry scenarios & descriptions
  GET  /api/kb                  → Official Ring knowledge base articles
  GET  /api/ring/status         → Ring API operational status (LIVE vs MOCK)
  POST /api/ring/whep-session   → Ring Partner API WHEP streaming session endpoint
  POST /api/telemetry           → Generates telemetry payload (custom sliders or presets)
  POST /api/analyze             → Full diagnostic pipeline: video/canvas + telemetry + LLM
  POST /api/upload-video        → Uploads an MP4 video stream
  GET  /api/samples             → Lists available sample video clips
"""

import os
import sys
import json
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from src.utils.telemetry_simulator import TelemetrySimulator
from src.utils.ring_client import RingClient
from src.agent.concierge import SupportConcierge

# ---------------------------------------------------------------------------
# App & Ring Client Singleton (Dual Mode: Live API / Mock Simulator)
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Ring TelemetryOps Concierge",
    description="Ring Device Digital Twin Simulator + Autonomous AI Diagnostic Agent",
    version="1.0.0",
)

ring_client = RingClient()

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
    frames_dropped: Optional[int]             = 0
    frames_received: Optional[int]            = 900
    frames_per_second: Optional[float]        = 30.0
    bitrate_kbps: Optional[int]               = 1200
    rtt_ms: Optional[float]                   = 18.0


class AnalyzeRequest(BaseModel):
    video_path: Optional[str] = None          # File name under samples/ or uploads/
    frame_base64: Optional[str] = None        # Current frame in Base64 (direct from WebRTC Canvas)
    telemetry: Optional[Dict[str, Any]] = None  # Complete telemetry payload
    provider: str = "bedrock"                 # "bedrock" | "groq"
    keyframe_second: float = 1.0
    camera_active: bool = False               # Whether camera is active or in low-power standby


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_custom_telemetry(req: TelemetryRequest) -> Dict[str, Any]:
    """Constructs a telemetry payload using custom slider parameters from the UI."""
    import datetime
    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
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
                "packets_lost": int((req.packet_loss_pct / 100.0) * req.frames_received),
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


def _resolve_video_path(video_path: str) -> Path:
    """Resolves video file path across samples/, uploads/, and data/ directories."""
    name = Path(video_path).name
    for base in [SAMPLES_DIR, UPLOADS_DIR, ROOT / "data" / "videos"]:
        candidate = base / name
        if candidate.exists():
            return candidate
    candidate = ROOT / video_path
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"Video file not found: {video_path}")


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
            "pink_purple_tint":   "IR Cut mechanical filter jammed over day sensor (magenta/purple tint).",
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
    """Returns official Ring knowledge base standard operating procedures."""
    from src.agent.knowledge_retriever import KnowledgeRetriever
    retriever = KnowledgeRetriever()
    return {"articles": retriever.kb_data}


@app.get("/api/samples")
async def get_samples():
    """Lists available sample video streams and uploads."""
    samples = []
    for ext in ["*.mp4", "*.MP4", "*.mov"]:
        for f in SAMPLES_DIR.glob(ext):
            samples.append({
                "name": f.name,
                "path": f"data/samples/{f.name}",
                "size_kb": round(f.stat().st_size / 1024, 1),
            })
    uploads = []
    for ext in ["*.mp4", "*.MP4", "*.mov"]:
        for f in UPLOADS_DIR.glob(ext):
            uploads.append({
                "name": f.name,
                "path": f"data/uploads/{f.name}",
                "size_kb": round(f.stat().st_size / 1024, 1),
            })
    return {"samples": samples, "uploads": uploads}


@app.get("/api/ring/status")
async def get_ring_status():
    """
    Returns Ring API connection status, operational mode (LIVE vs MOCK),
    and discovered device information.
    """
    devices = ring_client.list_devices()
    return {
        "mode": ring_client.mode,
        "is_live": ring_client.is_live,
        "device_count": len(devices),
        "devices": devices,
    }


@app.post("/api/ring/whep-session")
async def create_ring_whep_session(device_id: Optional[str] = "a1b2c3d4e5f6"):
    """
    Initiates or negotiates a WebRTC WHEP streaming session
    via Ring Partner API specifications.
    """
    sdp_dummy_offer = (
        "v=0\r\n"
        "o=- 461173200 2 IN IP4 127.0.0.1\r\n"
        "s=-\r\nt=0 0\r\n"
        "a=recvonly\r\n"
        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
    )
    return ring_client.create_whep_session(
        device_id=device_id or "a1b2c3d4e5f6",
        sdp_offer=sdp_dummy_offer,
    )


@app.post("/api/telemetry")
async def generate_telemetry(req: TelemetryRequest):
    """
    Generate telemetry payload.
    - mode='preset': executes through RingClient (Live Ring API if configured, otherwise Mock schema)
    - mode='custom': user-adjusted parameters (sliders)
    """
    if req.mode == "preset":
        if req.scenario not in TelemetrySimulator.SUPPORTED_SCENARIOS:
            raise HTTPException(status_code=400, detail=f"Invalid scenario: '{req.scenario}'.")
        return ring_client.get_telemetry(scenario=req.scenario)
    else:
        return _build_custom_telemetry(req)


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    """Full diagnostic pipeline: video/canvas keyframe + telemetry + LLM -> diagnostic report."""
    video_path = None
    if not req.frame_base64:
        if not req.video_path:
            raise HTTPException(status_code=400, detail="Either 'frame_base64' or 'video_path' is required.")
        try:
            video_path = str(_resolve_video_path(req.video_path))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

    if req.provider not in ("groq", "bedrock"):
        raise HTTPException(status_code=400, detail="Invalid provider. Must be 'bedrock' or 'groq'.")

    try:
        concierge = SupportConcierge(provider=req.provider)
        result = concierge.process_stream(
            video_path=video_path,
            telemetry=req.telemetry,
            keyframe_second=req.keyframe_second,
            frame_base64=req.frame_base64,
            camera_active=req.camera_active,
        )
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")


@app.post("/api/upload-video")
async def upload_video(file: UploadFile = File(...)):
    """Uploads an MP4/MOV video stream file to the server."""
    if not file.filename.lower().endswith((".mp4", ".mov", ".avi")):
        raise HTTPException(status_code=400, detail="Only .mp4, .mov, or .avi formats are accepted.")
    dest = UPLOADS_DIR / file.filename
    with open(str(dest), "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {
        "ok": True,
        "filename": file.filename,
        "path": f"data/uploads/{file.filename}",
        "size_kb": round(dest.stat().st_size / 1024, 1),
    }


# ---------------------------------------------------------------------------
# Direct Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.api.server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
