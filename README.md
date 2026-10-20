# Ring TelemetryOps: Autonomous Concierge Agent & Digital Twin

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](https://python.org)
[![AWS Bedrock](https://img.shields.io/badge/AWS%20Bedrock-Claude-orange.svg)](https://aws.amazon.com/bedrock/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Digital%20Twin-teal.svg)](https://fastapi.tiangolo.com/)

An autonomous diagnostic agent and interactive Digital Twin designed for smart video doorbells (**Ring Video Doorbell 4 & Elite**). It integrates deterministic computer vision telemetry extraction with Ring-informed troubleshooting procedures and multimodal reasoning via **AWS Bedrock** (Claude) to diagnose, triage, and remediate video streaming degradation, network dropouts, and hardware electrical faults. Bedrock is the primary reasoning engine; a deterministic local fallback preserves the same report contract when Bedrock is unavailable.

Developed for the **Build, Ship, Shape: Amazon Developer Hackathon 2026** (Ring Track + AWS Builder + Open Source Mini Challenges).

---

## Architecture Overview

```
+------------------------------------+          Deterministic OpenCV Metrics
|       Video Stream / WebRTC        | =======================================> +-------------------------+
|  (MP4 sample / Twin canvas frame)  | (Effective FPS, Bitrate, Resolution,     | Video Telemetry Engine  |
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
                  |                   | Ring-informed Retrieval Engine    | <----------------+ (If DEGRADED)
                  |                   | (15 project KB procedures)        |
                  |                   +-----------------------------------+
                  |                                     |
                  |                         Retrieved KB procedures
                  v                                     v
+---------------------------------------------------------------------------------------------------------+
| Multimodal Reasoning Engine (AWS Bedrock, Anthropic Claude)                                             |
| Correlates visual frame artifacts with telemetry, transport metrics, and retrieved KB procedures       |
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
2. **Deterministic Video Telemetry Engine:** OpenCV-based analysis calculating effective framerate, stream bitrate, mean brightness, Laplacian sharpness score, and optical rolling shutter banding.
3. **Telemetry-grounded troubleshooting retrieval:** 15 Ring-informed diagnostic procedures are ranked using explicit device/network signals, engineering heuristics, symptoms, and keywords. This structured approach is intentional because troubleshooting decisions depend on measurable operating ranges, not only semantic similarity.
4. **Autonomous Diagnostic Heartbeat Engine:** 15-second background health checks with 3-pulse (45-second) persistence filtering to prevent transient false alarms while catching immediate critical outages (0V transformer cut or critically drained battery).
5. **Primary Multimodal AWS Bedrock Reasoning:**
   * **AWS Bedrock:** Anthropic Claude (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) receives visual keyframes, deterministic metrics, telemetry, and retrieved procedures.
   * **Local fallback:** If Bedrock is unavailable or returns an invalid report, deterministic rules produce the same diagnostic schema and mark the provider as `local_fallback`.
   * **Pre-check:** Nominal telemetry and OpenCV results are returned without calling Bedrock; Bedrock is reserved for cases with detected anomalies.
6. **Two explicit data sources (`RING_DATA_SOURCE`):**
   * `simulator` (default): the Digital Twin. All telemetry, video and WHEP answers are **simulated** and labelled as such (`_source="mock"`, `"simulated": true`).
   * `partner`: the official **Ring Partner API**, read-only: `GET /v1/devices`, `GET /v1/devices/{id}/status` and `GET /v1/devices/{id}/capabilities`, as described in the [Ring developer documentation](https://developer.amazon.com/docs/ring/api-documentation.html) cited by the client (not re-verified for this release). Ring's API release notes also document an Event History API, a Device Configuration API and WebRTC/WHEP live video; this project does not implement them, and the KB marks the exact paths it names (events, configurations, WHEP, device attributes) as `unverified`. It reports `online`, battery percentage (only when capabilities declare battery support) and the last check-in time (`reported_at`). It does **not** provide RSSI, transformer voltage or WebRTC statistics, so those stay unknown. Diagnoses are telemetry-only: Ring image retrieval, live video (WHEP), motion events and webhooks are **not implemented**, and frames or telemetry sent by the client are never presented as Ring data.
   * Any other value is an explicit error, never a silent fallback to the simulator. The former unofficial `ring_doorbell` adapter was removed.

---

## Diagnostic Thresholds (project heuristics)

The diagnostic rules below are **heuristics of this project**, not Ring specifications. The Ring Partner API documentation defines none of them, and the Partner API does not report RSSI, transport metrics or transformer voltage. The backend values live in `src/agent/concierge.py` and are shared by the pre-check and the local fallback. The Digital Twin heartbeat keeps its own copies in `web/js/telemetry.js`, and the KB retriever keeps one for the transformer (`KnowledgeRetriever.TRANSFORMER_MIN_VAC`); `tests/test_threshold_parity.py` and `tests/test_voltage_retrieval.py` fail if a copy diverges. Badge and metric colour bands in the UI (for example battery 30/10 %, RSSI -60/-75 dBm) are display only and never trigger a diagnosis.

| Metric | Anomaly rule in code | Domain |
| :--- | :--- | :--- |
| Battery | `battery_percentage < 10` (10 % or more is never an anomaly; the heartbeat alerts on the first pulse) | `HARDWARE_POWER` (CRITICAL) |
| Transformer (wired) | `transformer_voltage_vac < 12` (general project heuristic for any wired device) | `HARDWARE_POWER` |
| Mechanical in-home chime | `chime_type_setting == "mechanical"`, wired, and `transformer_voltage_vac < 16` (product rule for this case only, not a Ring specification) | `HARDWARE_POWER` (DEGRADED) |
| Wi-Fi RSSI | `rssi < -75 dBm` | `NETWORK` |
| Packet loss | `> 8 %` | `NETWORK` |
| Jitter | `> 40 ms` | `NETWORK` |
| Frame rate / bitrate | `< 20 FPS` or `< 150 kbps` | `NETWORK` |
| Online | `online` is `false` (one status read, reported with its read time) | `UNKNOWN` (CRITICAL, confidence 0.6) |

Missing metrics are unknown, never healthy: without a measured network signal and a measured power signal the result is `INSUFFICIENT_DATA`.

The knowledge-base retriever also ranks articles with the RSSI bands cited by KB-RING-001 (Great above -60 dBm, Okay from -60 to above -70, Poor from -70 to above -80, Very poor at -80 or lower). The KB attributes these bands to a Ring Help Center article; they rank articles only and do not change the diagnosis.

### Knowledge base sources

`data/knowledge_base/ring_video_troubleshooting.json` lists, per article, the titles of the sources it was written from (`official_sources`). The titles have no URLs and were not re-verified for this release, and the articles' original evidence labels are self-declared: every article now has `evidence_level: "unverified"` (the original label is kept in `declared_evidence_level`), and API mappings carry `verification: "unverified…"`. The keys keep their `official_*` names for `/api/kb` compatibility; those names do not mean the content is verified. Treat the articles as Ring-informed troubleshooting guidance, not as Ring standard operating procedures.

### Sharpness threshold

The sharpness check is a local heuristic of this project, not a Ring specification. The [Ring Developer API documentation](https://developer.amazon.com/docs/ring/api-documentation.html) defines no sharpness or blur metric and no image-quality threshold.

* **Metric:** the variance of the Laplacian of the grayscale frame (`sharpness_score` in `src/video_pipeline/analyzer.py`).
* **Scale:** frames wider than 720 px are first downscaled to 720 px. The variance depends on resolution, so the live canvas frame and the MP4 file use the same scale. Before this change, the file path measured at full resolution with a threshold of 20, and the frame path measured at 720 px with a threshold of 15. The two scores could not be compared.
* **Threshold:** `MIN_SHARPNESS_SCORE = 40.0`. The analyzer reports a scene below this value as `DEGRADED` with a "Deficient sharpness" issue. The concierge uses that issue and has no threshold of its own.
* **Calibration** (measured on `data/samples/good_internet.mp4` and the other samples, at 720 px):

| Case | Score |
| :--- | :--- |
| Sharp frames (`good_internet.mp4`, 28 frames) | 235 – 274 |
| Mild blur (Gaussian σ = 2 px at 1080p) | 51 – 57 (not flagged) |
| Simulator *Dirty Lens* (canvas `blur(3.8px)`, emulated in Python) | 12.7 – 13.8 |
| *Dirty Lens* observed in the browser (one sample) | 15.81 |
| `optical_obstruction.mp4` | 0.74 |

  The threshold sits between Dirty Lens and sharp frames, with margin on both sides. It is not a scientific calibration. Only one real scene was measured. A low-texture scene (a plain wall, night) can score low without blur, and a scene with a lot of fine detail can score high when slightly blurred.

---

## Quickstart & Installation

### 1. Clone & Set Up Virtual Environment

Requirements: Python 3.11 or newer (the pinned `numpy` needs it; tested with 3.11 and 3.14) and,
for the frontend tests only, Node.js 18 or newer.

Windows (PowerShell). The commands call the virtual environment's Python directly, so no
activation script (and no change to the PowerShell execution policy) is needed:
```powershell
git clone https://github.com/ernesto2604/ring-telemetryops.git
cd ring-telemetryops
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux/macOS:
```bash
git clone https://github.com/ernesto2604/ring-telemetryops.git
cd ring-telemetryops
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables (optional)

The simulator demo and the tests need no `.env`. To use Bedrock or the Ring Partner API, create
`.env` from the example only if it does not exist yet:
```powershell
# Windows (PowerShell)
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```
```bash
# Linux/macOS
[ -f .env ] || cp .env.example .env
```

Edit `.env` with your credentials:
```env
# AWS Bedrock
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0

# Data source: simulator (default, Digital Twin) | partner (official Ring Partner API)
RING_DATA_SOURCE=simulator

# Ring Partner API (used only when RING_DATA_SOURCE=partner)
RING_PARTNER_ACCESS_TOKEN=
RING_PARTNER_DEVICE_ID=
```

`.env.example` documents the Partner polling settings (`RING_PARTNER_POLL_SECONDS`,
`RING_PARTNER_OFFLINE_ALERT_READS`, `RING_PARTNER_OFFLINE_ALERT_MAX_GAP_SECONDS`); they are
prototype defaults, not Ring recommendations.

AWS access with `bedrock:InvokeModel` permission for the configured Anthropic model is
required for the primary diagnosis path. Explicit `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
(and optional `AWS_SESSION_TOKEN`) are used when both keys are set; otherwise boto3 uses its
standard credential chain (profiles, SSO, roles). If Bedrock is unavailable or returns a report
that does not match the diagnostic contract, the application uses its explicitly labelled
deterministic local fallback (`_provider: "local_fallback"`).

With `RING_DATA_SOURCE=partner`, the server reads the device status from the Ring Partner API
(for example the Ring Developer Playground test device) and runs a telemetry-only diagnosis.
Ring API errors are returned as errors (401/403 as 502, 404, 429 with `Retry-After`, 503, 504);
they never mark the device offline and never fall back to simulated data.

---

## Usage

### Option A: Launch Interactive Digital Twin (Web UI)

Run the FastAPI local server in simulator mode. Variables set in the shell take precedence over
`.env` (python-dotenv never overrides them).

**Offline demo (no Ring, no Bedrock, no `.env`)**, Windows PowerShell from the repository root.
`PYTHON_DOTENV_DISABLED` skips `.env`; the `AWS_*` settings leave boto3 without credentials, so
every anomaly uses the labelled `local_fallback` and nothing is sent to AWS:
```powershell
$env:PYTHON_DOTENV_DISABLED = "1"
$env:RING_DATA_SOURCE = "simulator"
Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY, Env:AWS_SESSION_TOKEN, Env:AWS_PROFILE -ErrorAction SilentlyContinue
$env:AWS_SHARED_CREDENTIALS_FILE = "NUL"
$env:AWS_CONFIG_FILE = "NUL"
$env:AWS_EC2_METADATA_DISABLED = "true"
.\venv\Scripts\python.exe -m uvicorn src.api.server:app --host 127.0.0.1 --port 8000
```

**Simulator with Bedrock** (AWS credentials in `.env` or the AWS credential chain; each diagnosis
of an anomaly is a billed Bedrock request):
```powershell
$env:RING_DATA_SOURCE = "simulator"
.\venv\Scripts\python.exe -m uvicorn src.api.server:app --host 127.0.0.1 --port 8000
```

Open your browser at **`http://localhost:8000`**. From the web interface you can:
* Interact directly with the 3D Ring Doorbell (ring the bell, change faceplate finish).
* Inject hardware faults in real time (*Dirty Lens*, *Night IR LEDs*, *Pink IR Filter*, *50/60Hz Flicker*, *Transformer Cut*).
* Adjust parametric sliders (RSSI, Battery, Transformer Voltage, WebRTC Jitter, Packet Loss, Bitrate).
* Monitor autonomous 15s heartbeats, smart push alerts, and inspect the Ring-informed troubleshooting knowledge base.

In Partner mode the Digital Twin controls are disabled, the metric cards show only values
Ring reported (`N/D` otherwise), the camera monitor shows no video, and diagnoses run with
**Analyze now**. Simulated elements of the Digital Twin (camera feed, chime, live view, WHEP
answer) are labelled as simulated.

### Option B: Run Headless CLI Diagnostic Agent

Execute the standalone command-line pipeline against any video stream and predefined telemetry
scenario (simulated telemetry; with AWS credentials configured, anomalies are sent to Bedrock):

```powershell
# 1. Healthy baseline stream
.\venv\Scripts\python.exe -m src.agent.concierge --video data/samples/good_internet.mp4 --scenario nominal

# 2. Wi-Fi degradation scenario
.\venv\Scripts\python.exe -m src.agent.concierge --video data/samples/bad_internet.mp4 --scenario wifi_degradation
```
On Linux/macOS, with the virtual environment activated, use `python` instead of `.\venv\Scripts\python.exe`.

### Option C: Run Tests

Run the automated test suite from the repository root:

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --test tests/frontend/ui.test.mjs
```

The tests replace the Ring Partner API with local fakes or a server on 127.0.0.1 and replace
the Bedrock call with test doubles. `tests/conftest.py` blanks Ring and AWS credentials and makes
any real boto3 client creation fail, so the suite needs no credentials or network and incurs no
Bedrock charges. The Node tests run the page scripts against a fake DOM (no real browser); on
Windows pass the file path to `node --test`, not the directory.

### Demo walkthrough (B1–B5)

Simulator mode (`RING_DATA_SOURCE=simulator`), Web UI. Without AWS credentials the diagnosis is
the labelled `local_fallback`; with them, anomalies are sent to Bedrock (billed).

* **Alerts, recovery and Reset (B1/B2):** drag Wi-Fi RSSI below -75 dBm. The heartbeat counts
  1/3, 2/3, 3/3 (15 s each) and runs one diagnosis. Restore RSSI: the alert hides and the report is
  kept, marked *Resolved*. **Reset** clears both. A late answer for a resolved incident is discarded.
* **Battery (B3):** select *Battery Only* and set the battery to 9 %: diagnosis on the first pulse.
  At 10–15 % the badge turns yellow but no diagnosis runs (badge colours are display only).
* **Voltage retrieval (B4):** wired transformer below 12 V is a power anomaly (3 pulses); 12–14 V on
  a generic wired device is not. Retrieval per voltage is covered by `tests/test_voltage_retrieval.py`.
* **Mechanical in-home chime (B5):** the UI has no scenario selector and its sliders send no
  `chime_type_setting`, so B5 is shown through the API, the CLI or the tests. With the server
  running (second PowerShell window):

  ```powershell
  $tel = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/telemetry -ContentType 'application/json' -Body '{"mode":"preset","scenario":"in_home_chime"}'
  $body = @{ video_path = 'good_internet.mp4'; telemetry = $tel; camera_active = $false; source_mode = 'twin' } | ConvertTo-Json -Depth 10
  (Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/analyze -ContentType 'application/json' -Body $body).diagnostic_result
  ```

  Without a server (same environment variables as the offline demo to avoid Bedrock):

  ```powershell
  .\venv\Scripts\python.exe -m src.agent.concierge --video data/samples/good_internet.mp4 --scenario in_home_chime
  .\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_in_home_chime.py
  ```

  Expected: `DEGRADED` / `HARDWARE_POWER` at 13.5 V (simulated telemetry with
  `chime_type_setting="mechanical"`). The API path is exercised by
  `test_documented_api_demo_procedure_for_in_home_chime`.

### What needs credentials or external services

| Operation | Needs |
| :--- | :--- |
| Simulator UI, CLI, offline demo, all tests | Nothing (no Ring device, no AWS) |
| Bedrock diagnosis (primary engine) | AWS credentials with `bedrock:InvokeModel`; billed per request |
| `RING_DATA_SOURCE=partner` | `RING_PARTNER_ACCESS_TOKEN` (and a device ID); calls the Ring Partner API |

---

## Repository Structure

```text
ring-telemetryops/
├── data/
│   ├── knowledge_base/
│   │   └── ring_video_troubleshooting.json   # 15 Ring-informed troubleshooting articles
│   └── samples/
│       ├── good_internet.mp4                 # 1080p 30fps baseline test stream
│       ├── bad_internet.mp4                  # Degraded low-bitrate test stream
│       └── optical_obstruction.mp4           # Obstructed-lens test stream
├── src/
│   ├── agent/
│   │   ├── concierge.py                      # Autonomous Bedrock LLM Orchestrator
│   │   └── knowledge_retriever.py            # Multimodal RAG Engine
│   ├── api/
│   │   └── server.py                         # FastAPI Web Server & REST endpoints
│   ├── utils/
│   │   ├── ring_client.py                    # Digital Twin adapter (simulated data only)
│   │   ├── ring_partner_client.py            # Official Ring Partner API client (read-only)
│   │   ├── partner_telemetry.py              # Partner status -> telemetry schema adapter
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
├── tests/
│   ├── conftest.py                           # Blanks Ring/AWS credentials, blocks real boto3 clients
│   ├── test_*.py                             # pytest suites (API, agent, KB, Partner client, B1–B5)
│   └── frontend/ui.test.mjs                  # Node tests for the web UI (fake DOM)
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