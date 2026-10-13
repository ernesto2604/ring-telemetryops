# Ring TelemetryOps: Autonomous Concierge Agent & Digital Twin

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://python.org)
[![AWS Bedrock](https://img.shields.io/badge/AWS%20Bedrock-Claude%203%20Haiku-orange.svg)](https://aws.amazon.com/bedrock/)
[![Groq](https://img.shields.io/badge/Groq-Llama--4%20Scout-purple.svg)](https://groq.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-WHEP%20Digital%20Twin-teal.svg)](https://fastapi.tiangolo.com/)

An autonomous diagnostic agent and interactive Digital Twin designed for smart video doorbells (**Ring Video Doorbell 4 & Elite**). It integrates deterministic computer vision telemetry extraction with official Ring Standard Operating Procedures (RAG Knowledge Base) and Multimodal LLMs via **AWS Bedrock** (Claude 3 Haiku) and **Groq** (Llama-4 Scout) to diagnose, triage, and remediate video streaming degradation, network dropouts, and hardware electrical faults in real time.

Developed for the **Build, Ship, Shape: Amazon Developer Hackathon 2026** (Ring Track + AWS Builder + Open Source Mini Challenges).

---

## Architecture Overview

```
+------------------------------------+          Deterministic OpenCV Metrics
|       Video Stream / WebRTC        | =======================================> +-------------------------+
|     (RTSP / MP4 / WHEP Session)    | (Effective FPS, Bitrate, Resolution,     | Video Telemetry Engine  |
+------------------------------------+  Laplacian Variance, Rolling Shutter)    |        (OpenCV)         |
                  |                                                             +-------------------------+
                  |                                                                          |
                  v                                                                          v
+------------------------------------+                                          +-------------------------+
|     Multimodal Keyframe Capture    |                                          |    State Classifier     |
|         (Base64 at t=1.0s)         |                                          |  (OPTIMAL vs DEGRADED)  |
+------------------------------------+                                          +-------------------------+
                  |                                                                          |
                  |                   +-----------------------------------+                  |
                  |                   | Official Ring KB Engine (RAG v2.1)| <----------------+ (If DEGRADED)
                  |                   | (15 Standard Operating Procedures)|
                  |                   +-----------------------------------+
                  |                                     |
                  |                            Retrieved Ring SOPs
                  v                                     v
+---------------------------------------------------------------------------------------------------------+
| Multimodal Reasoning Engine (AWS Bedrock Claude 3 Haiku / Groq Llama-4 Scout)                           |
| Correlates visual frame artifacts with telemetry metrics, WebRTC stats, and official Ring SOPs          |
+---------------------------------------------------------------------------------------------------------+
                                                    |
                                                    v
                                  +-----------------------------------+
                                  | Autonomous Diagnostic Report      |
                                  | (Root cause, confidence & actions)|
                                  +-----------------------------------+
```

---

## Key Features

1. **Autonomous Digital Twin Simulator:** Interactive 3D Ring Doorbell hardware replica with real-time Web Audio chime synthesis, physical fault injection (dirty lens, night IR LEDs, stuck IR-cut filter, 50/60Hz AC lighting flicker, and 18V transformer power cut).
2. **Deterministic Video Telemetry Engine:** OpenCV-based analysis calculating effective framerate, stream bitrate, black pixel ratio, mean brightness, Laplacian sharpness score, and optical rolling shutter banding.
3. **Official Ring Troubleshooting SOPs (RAG v2.1):** 15 comprehensive diagnostic operating procedures derived from official Ring Support documentation, covering RSSI thresholds, upstream bandwidth, PoE standards (IEEE 802.3af/at), transformer load requirements, and WHEP live view streaming errors.
4. **Autonomous Diagnostic Heartbeat Engine:** 15-second background health checks with 3-pulse (45-second) persistence filtering to prevent transient false alarms while catching immediate critical outages (0V transformer cut or critically drained battery).
5. **Multimodal Multi-Provider LLM Integration:**
   * **AWS Bedrock:** Anthropic Claude 3 Haiku (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) with visual keyframe inspection.
   * **Groq Cloud:** Llama-4 Scout for ultra-fast low-latency inference.
   * **Deterministic Local Fallback:** Operates with structured rule-matching when external cloud credentials are not supplied.
6. **Ring Partner API & WHEP Live Streaming Support:** Built-in `RingClient` adapter that authenticates with official Ring credentials when configured, with automatic fallback to simulated telemetry.

---

## Diagnostic Benchmarks & Thresholds

| Metric | Official Ring Healthy Threshold | Degraded Stream Condition | Critical Failure Condition |
| :--- | :--- | :--- | :--- |
| **Wi-Fi RSSI** | `0 to -60 dBm` (Great) | `-70 to -80 dBm` (Poor) | `< -80 dBm` (Critical alert) |
| **Frame Rate** | $\ge 25.0\text{ FPS}$ | $10.0 - 20.0\text{ FPS}$ | $< 5.0\text{ FPS}$ |
| **Stream Bitrate** | $\ge 1000\text{ kbps}$ | $300 - 600\text{ kbps}$ | $< 150\text{ kbps}$ |
| **Packet Loss** | $\le 1.0\%$ | $3.0\% - 8.0\%$ | $> 10.0\%$ |
| **Transformer** | $16.0 - 24.0\text{ VAC}$ | $< 14.0\text{ VAC}$ (Undervoltage) | $0.0\text{ VAC}$ (Circuit power cut) |
| **Status** | **HEALTHY** | **DEGRADED** | **CRITICAL** |

---

## Quickstart & Installation

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/ernesto2604/ring-telemetryops.git
cd ring-telemetryops

# Create and activate virtual environment
python -m venv venv
# On Windows (PowerShell):
.\venv\Scripts\Activate.ps1
# On Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy the example configuration file:
```bash
cp .env.example .env
```

Edit `.env` with your API credentials (optional):
```env
# AWS Bedrock
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key

# Groq Cloud
GROQ_API_KEY=gsk_your_groq_key

# Ring Partner API (Optional - will use mock simulator if omitted)
RING_USERNAME=
RING_PASSWORD=
```

---

## Usage

### Option A: Launch Interactive Digital Twin (Web UI)

Run the FastAPI local server:
```bash
uvicorn src.api.server:app --port 8000 --reload
```

Open your browser at **`http://localhost:8000`**. From the web interface you can:
* Interact directly with the 3D Ring Doorbell (ring the bell, change faceplate finish).
* Inject hardware faults in real time (*Dirty Lens*, *Night IR LEDs*, *Pink IR Filter*, *50/60Hz Flicker*, *Transformer Cut*).
* Adjust parametric sliders (RSSI, Battery, Transformer Voltage, WebRTC Jitter, Packet Loss, Bitrate).
* Monitor autonomous 15s heartbeats, smart push alerts, and inspect the official Ring Knowledge Base (RAG).

### Option B: Run Headless CLI Diagnostic Agent

Execute the standalone command-line pipeline against any video stream and predefined telemetry scenario:

```bash
# 1. Healthy baseline stream
python -m src.agent.concierge --video data/samples/good_internet.mp4 --scenario healthy --provider bedrock

# 2. Wi-Fi degradation scenario
python -m src.agent.concierge --video data/samples/bad_internet.mp4 --scenario wifi_degradation --provider bedrock

# 3. Fast inference via Groq
python -m src.agent.concierge --video data/samples/bad_internet.mp4 --scenario wifi_degradation --provider groq
```

---

## Repository Structure

```text
ring-telemetryops/
├── data/
│   ├── knowledge_base/
│   │   └── ring_video_troubleshooting.json   # 15 Official Ring diagnostic SOPs
│   └── samples/
│       ├── good_internet.mp4                 # 1080p 30fps baseline test stream
│       └── bad_internet.mp4                  # Degraded low-bitrate test stream
├── src/
│   ├── agent/
│   │   ├── concierge.py                      # Autonomous LLM Orchestrator (Bedrock + Groq)
│   │   └── knowledge_retriever.py            # Multimodal RAG Engine
│   ├── api/
│   │   └── server.py                         # FastAPI Web Server & REST endpoints
│   ├── utils/
│   │   ├── ring_client.py                    # Ring Partner API & WHEP client adapter
│   │   └── telemetry_simulator.py            # Parametric telemetry generator
│   └── video_pipeline/
│       └── analyzer.py                       # OpenCV video frame & stream analyzer
├── web/
│   ├── css/
│   │   ├── ring-device.css                   # 3D Ring Doorbell physical styling
│   │   ├── style.css                         # Dark theme design system tokens
│   │   └── video-monitor.css                 # WebRTC monitor & HUD styles
│   ├── js/
│   │   ├── app.js                            # Autonomous heartbeat & notification engine
│   │   ├── canvas-engine.js                  # Video degradation & rendering engine
│   │   ├── chime-audio.js                    # Web Audio chime tone synthesizer
│   │   └── telemetry.js                      # Live slider & badge synchronizer
│   └── index.html                            # Digital Twin interactive interface
├── FRICTION_LOG.md                           # Hackathon Developer Experience Friction Log
├── LICENSE                                   # MIT License
├── README.md                                 # Technical documentation
└── requirements.txt                          # Python dependencies
```

---

## Developer Experience & Hackathon Friction Log

A comprehensive **Friction Log** documenting developer experience improvements, Ring API discovery opportunities, and WebRTC streaming integration patterns is available in [FRICTION_LOG.md](FRICTION_LOG.md).

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.