# Ring TelemetryOps: Autonomous Concierge Agent & Digital Twin

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-brightgreen.svg)](https://python.org)
[![AWS Bedrock](https://img.shields.io/badge/AWS%20Bedrock-Claude-orange.svg)](https://aws.amazon.com/bedrock/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Digital%20Twin-teal.svg)](https://fastapi.tiangolo.com/)

An autonomous diagnostic agent and interactive Digital Twin designed for smart video doorbells (**Ring Video Doorbell 4 & Elite**). It integrates deterministic computer vision telemetry extraction with Ring-informed troubleshooting procedures and multimodal reasoning via **AWS Bedrock** (Claude) to diagnose, triage, and remediate video streaming degradation, network dropouts, and hardware electrical faults. Bedrock is the primary reasoning engine; a deterministic local fallback preserves the same report contract when Bedrock is unavailable.

Developed for the **Build, Ship, Shape: Amazon Developer Hackathon 2026** (Ring Track + AWS Builder + Open Source Mini Challenges).

## Hackathon submission

* **Track:** Ring. **Mini challenges:** AWS Builder and Open Source (MIT license, see [LICENSE](LICENSE)).
* **Ring:** a Digital Twin simulator of the Ring Video Doorbell 4 and the read-only Ring Partner API
  mode (`RING_DATA_SOURCE=partner`: device status and capabilities).
* **AWS service used:** Amazon Bedrock with Anthropic Claude Haiku 4.5
  (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, region `us-east-1`). It reasons over a camera
  keyframe, deterministic OpenCV metrics, the pre-check anomalies and the retrieved troubleshooting
  procedures. Severity and root-cause domain are decided by the deterministic pre-check, the answer is
  validated against a report contract with one corrective retry, and a labelled local fallback keeps
  the same schema when Bedrock is unavailable. Details: [Local fallback (mock-up) vs Bedrock (realistic
  mode)](#local-fallback-mock-up-vs-bedrock-realistic-mode).
* **Development workflow:** the project was built with AI coding agents (Claude Code) under human
  direction, with a test suite (pytest and Node) and live verification against Bedrock.

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

1. **Autonomous Digital Twin Simulator:** Interactive 3D Ring Doorbell hardware replica with real-time Web Audio chime synthesis, physical fault injection (dirty lens, stuck IR-cut filter, 50/60Hz AC lighting flicker, and 18V transformer power cut) and a separate *Night mode (IR LEDs)* control, which is a normal operating condition, not a fault.
2. **Deterministic Video Telemetry Engine:** OpenCV-based analysis calculating effective framerate, stream bitrate, mean brightness, Laplacian sharpness score, and optical rolling shutter banding.
3. **Telemetry-grounded troubleshooting retrieval:** 15 Ring-informed diagnostic procedures are ranked using explicit device/network signals, engineering heuristics, symptoms, and keywords. This structured approach is intentional because troubleshooting decisions depend on measurable operating ranges, not only semantic similarity.
4. **Autonomous Diagnostic Heartbeat Engine:** background health checks every 15 s with three severity levels per metric (see [Severity levels](#severity-levels-project-heuristics)). A CRITICAL metric (transformer power cut, critically drained battery, critical Wi-Fi signal) is diagnosed on the first heartbeat; a DEGRADED one needs 3 consecutive heartbeats (45 s), which filters transient glitches.
5. **Primary Multimodal AWS Bedrock Reasoning:**
   * **AWS Bedrock (realistic mode):** Anthropic Claude Haiku 4.5 (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) receives visual keyframes, deterministic metrics, the pre-check anomalies with their levels, telemetry, and retrieved procedures.
   * **Local fallback (mock-up):** If Bedrock is unavailable or breaks the report contract twice, deterministic rules produce the same diagnostic schema and mark the provider as `local_fallback` (see [Local fallback (mock-up) vs Bedrock (realistic mode)](#local-fallback-mock-up-vs-bedrock-realistic-mode)).
   * **Pre-check:** Nominal telemetry and OpenCV results are returned without calling Bedrock; Bedrock is reserved for cases with detected anomalies.
6. **Two explicit data sources (`RING_DATA_SOURCE`):**
   * `simulator` (default): the Digital Twin. All telemetry, video and WHEP answers are **simulated** and labelled as such (`_source="mock"`, `"simulated": true`).
   * `partner`: the official **Ring Partner API**, read-only: `GET /v1/devices`, `GET /v1/devices/{id}/status` and `GET /v1/devices/{id}/capabilities`, as listed in the [Ring Partner API documentation](https://developer.amazon.com/docs/ring/api-documentation.html) (endpoint paths checked against the documentation on 2026-10-21; the response fields the client reads were not re-verified). It also reads device settings through `GET /v1/devices/{id}?include=...`, image snapshots (`POST /v1/devices/{device_id}/media/image/download`) and the Event History (`GET /v1/history/devices/{device_id}/events`); see "Ring media constraints" below. WHEP live video (`POST /v1/devices/{device_id}/media/streaming/whep/sessions`) is implemented with this server as the SDP intermediary (see "Ring live view" below). The KB marks the Event History, Device Configuration and WHEP paths as matching the documentation (checked 2026-10-21) while the fields it attaches to them, and the device attributes it names, stay `unverified`. It reports `online`, battery percentage (only when capabilities declare battery support) and the last check-in time (`reported_at`). It does **not** provide RSSI, transformer voltage or WebRTC statistics, so those stay unknown. Diagnoses use that telemetry plus a Ring snapshot when the device supports one. Recorded clips (`POST /v1/devices/{id}/media/video/download`) and best-effort RTSPS live stream statistics are available on demand (see "Recorded clips" and "Live stream stats" below); Ring live view (WHEP) in the browser is implemented (see "Verification status" below for what was checked live; signed webhooks are implemented and tested with fixtures only, see "Webhooks" below), and frames or telemetry sent by the client are never presented as Ring data.
   * Any other value is an explicit error, never a silent fallback to the simulator. The former unofficial `ring_doorbell` adapter was removed.

---

## Severity levels (project heuristics)

Every metric has three levels: **GREEN**, **DEGRADED** and **CRITICAL**. The levels are
**heuristics of this project, not Ring specifications**: the Ring developer documentation reviewed
for this project (developer.amazon.com, Ring API documentation, Get Started, UX design guide)
defines no battery, Wi-Fi, transport or voltage thresholds, and the Partner API does not report
RSSI, transport metrics or transformer voltage.

The numbers live in one place, [`src/config/severity_thresholds.json`](src/config/severity_thresholds.json),
read by `src/config/severity.py` (pre-check, local fallback, Bedrock prompt, KB retriever, video
analyzer) and served to the web UI at `GET /api/thresholds`; the UI keeps no copy.
`tests/test_threshold_parity.py` fails if a threshold literal appears in the classification code, and
`tests/test_readme_concordance.py` fails if the table below differs from the JSON file.
Change a value only in the JSON file (and this table).

| Metric (table key) | GREEN | DEGRADED | CRITICAL | Domain | Applies when |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Transformer voltage (wired) (`transformer`) | `>= 12` V AC | Undervoltage: `< 12` V AC and `> 0.5` | No power: `<= 0.5` V AC | `HARDWARE_POWER` | not power_mode=battery |
| Transformer voltage for a mechanical in-home chime (`mechanical_chime`) | `>= 16` V AC | Too low for the mechanical chime: `< 16` V AC | none | `HARDWARE_POWER` | chime_type=mechanical, not power_mode=battery |
| Battery (`battery`) | `>= 30` % | Low: `< 30` % and `>= 10` (power_mode=battery only) | Critical: `< 10` % | `HARDWARE_POWER` | always |
| Wi-Fi signal (RSSI) (`rssi`) | `>= -60` dBm | Poor: `< -75` dBm and `>= -88`; Weak: `< -60` dBm and `>= -75` | Critical: `< -88` dBm | `NETWORK` | always |
| Packet loss (`packet_loss_pct`) | `<= 8` % | High: `> 8` % | none | `NETWORK` | always |
| Jitter (`jitter_ms`) | `<= 40` ms | High: `> 40` ms | none | `NETWORK` | always |
| Frame rate (`fps`) | `>= 20` FPS | Low: `< 20` FPS | none | `NETWORK` | always |
| Bitrate (`bitrate`) | `>= 150` kbps | Low: `< 150` kbps | none | `NETWORK` | always |

* Bands are checked from worst to mildest; a boundary written `<` is exclusive and `>=` inclusive
  (RSSI -60 dBm is GREEN, -61 to -75 dBm Weak, -76 to -88 dBm Poor, -89 dBm Critical).
* Battery: the critical band applies to any measured battery; the low band only to a battery-only
  install (`power_mode=battery`), as before. A wired doorbell keeps its internal battery charged as a
  backup, so the transformer bands decide there. The Ring Partner API reports no power setup, so a
  Partner battery is only judged by the critical band (unchanged Partner behaviour).
* Transformer: there is no yellow band between 12 and 16 V for a generic wired device. The 16 V rule
  applies only when the telemetry reports `chime_type_setting="mechanical"`.
* Unmeasured metrics are unknown, never GREEN: without a measured network signal and a measured power
  signal the result is `INSUFFICIENT_DATA`. A Ring-reported `online: false` is `CRITICAL` / `UNKNOWN`
  (confidence 0.6) from the pre-check.
* Every colour in the UI is a level and is always written next to the colour (Excellent / Weak / Poor /
  Critical, Low, Undervoltage, No power...). Yellow is DEGRADED and counts towards a diagnosis; red is
  CRITICAL. There are no display-only colour bands in the Digital Twin.
* The knowledge-base retriever also ranks articles with the RSSI bands cited by KB-RING-001 (Great
  above -60 dBm, Okay from -60 to above -70, Poor from -70 to above -80, Very poor at -80 or lower).
  Those bands only order articles; they never change the diagnosis or its severity.

### Heartbeat, triggers and incidents

* **Heartbeat (camera in standby, nobody watching):** every 15 s the Twin classifies all metrics.
  CRITICAL is diagnosed on the first heartbeat (the counter jumps to 3/3); DEGRADED needs 3 consecutive heartbeats
  and then one diagnosis runs. GREEN resets the counter.
* **One incident per problem family** (power or network): Weak -> Poor Wi-Fi, or RSSI -> packet loss,
  continues the same incident and its counter; a change of family starts a new one.
* **Escalation** DEGRADED -> CRITICAL is diagnosed immediately. A drop CRITICAL -> DEGRADED keeps the
  incident and its report open, without a new diagnosis. **Resolved** is shown only when every metric is
  back to GREEN; Reset clears the alert, the report and the counter. A late answer for an incident that
  has already ended is discarded.
* **Triggers (Ring Chime / Ding, PIR Motion, Live View):** the session is analyzed at once, without
  waiting for the counter. The backend pre-check checks the telemetry and the captured frame: with a
  problem the report is titled "Issue during Doorbell Press / Motion Detection / Live View"; without one
  Bedrock is not called and the UI shows and notifies nothing (the API and CLI still return an explicit
  `HEALTHY`). Triggers fired close together share one analysis.
* **No duplicate alerts:** a trigger marks the open incident as diagnosed, so the heartbeat does not
  report it again when it reaches 3/3, and a new analysis of an incident whose alert is already shown
  updates the report without a second notification.
* **Heartbeat during Live View:** the heartbeat keeps running every 15 s while Live View is open. If it
  diagnoses then, the request also carries the current canvas frame (`camera_active: true`), so the
  backend analyzes the image as well.

### Severity and confidence in the report

* The report's `diagnostic_status` (DEGRADED / CRITICAL) is the deterministic worst level of the table,
  never the model's opinion. If Bedrock answers a different label, it is replaced, a warning is logged
  and the response carries `severity_override: {"model": ..., "deterministic": ...}`; the model's
  findings and mitigation are kept.
* `confidence_score` from Bedrock is **self-assessed by the model**; in the observed runs it took only
  the values 0.85, 0.92 and 0.95, so read it as a coarse signal. The local fallback always reports the
  fixed value **0.86** (a mock-up constant), and the pre-check's nominal verdict reports 1.0 (every
  measured metric is GREEN).

### Design decisions

Some bands are product decisions rather than documented facts: the battery low band (10-29 %), the
transformer bands (0.5 V and 12 V), the transport metrics without a CRITICAL level, and the
confidence handling above. They all live in `src/config/severity_thresholds.json`, so each one can be
changed in a single place. The Partner "offline" rule (3 consecutive reads) is outside the severity
table and keeps its own settings.

### Knowledge base sources

`data/knowledge_base/ring_video_troubleshooting.json` lists, per article, the titles of the sources it was written from (`official_sources`). The titles have no URLs and were not re-verified for this release, and the articles' original evidence labels are self-declared: every article now has `evidence_level: "unverified"` (the original label is kept in `declared_evidence_level`), and API mappings carry `verification: "unverified…"`. The keys keep their `official_*` names for `/api/kb` compatibility; those names do not mean the content is verified. Treat the articles as Ring-informed troubleshooting guidance, not as Ring standard operating procedures.

### Video analyzer checks (OpenCV)

All the image limits below are local heuristics of this project, not Ring specifications. The
[Ring Developer API documentation](https://developer.amazon.com/docs/ring/api-documentation.html)
defines no sharpness, blur, colour or banding metric.

* **Sharpness:** the variance of the Laplacian of the grayscale frame (`sharpness_score` in
  `src/video_pipeline/analyzer.py`). Below `MIN_SHARPNESS_SCORE = 40.0` the analyzer reports a
  "Deficient sharpness" issue (`DEGRADED`); the concierge uses that issue and has no threshold of its own.
* **Scale:** only frames **wider than 720 px** are downscaled to 720 px before measuring; narrower
  frames are measured at their native resolution. The score therefore depends on the resolution of the
  source, and the calibration and the threshold of 40 are only meaningful for sources of 720 px or more.
* **Chromatic imbalance (stuck IR-cut filter):** when the red mean exceeds 1.35 x (green + 5) and the
  blue mean 1.15 x (green + 5), the analyzer reports "Severe chromatic imbalance: possible stuck
  mechanical IR-cut filter (pink/purple tint in daylight)" (`DEGRADED`).
* **Rolling-shutter banding (50/60 Hz flicker):** the standard deviation of the row-to-row brightness
  differences; above **4.5** the analyzer reports periodic horizontal banding (`DEGRADED`). Observed:
  the simulator's flicker gave 15.8 in the browser and 17.2 in the Python emulation used by the tests.
  The customer message points to the Anti-Flicker setting in the Ring app: that setting comes from the
  project knowledge base (KB-RING-014, unverified), not from Ring documentation.
* **Ring images (watermark and banding):** for frames from Ring, the watermark band is cropped in pixels
  (`RING_WATERMARK_BAND_PX`, 100 px, at most 35 % of the height), because the watermark has a fixed size,
  and banding also requires a clear periodic row-brightness profile (`RING_BANDING_PERIODICITY_LIMIT`):
  detailed real outdoor scenes exceeded the simulator-calibrated 4.5 without any flicker. The real
  calibration used only three Ring Playground frames. The simulator keeps the variance-only rule.
* **Black frame:** mean brightness below 4.0 is `CRITICAL` (no usable optical signal).
* **Stream metrics:** frame rate and bitrate use the FPS and bitrate bands of the severity table.

Calibration (measured with the analyzer; files at the analyzer scale, frames as the browser sends them):

| Case | Score |
| :--- | :--- |
| `good_internet.mp4` (1920x1080, 25 fps, 245 frames, 9.8 s): all frames | 230.32 – 273.49 |
| `good_internet.mp4`: `inspect_stream` average (every 5th frame, 49 samples) | 253.05 |
| Mild blur (Gaussian σ = 2 px at 1080p) | 51 – 57 (not flagged) |
| Simulator *Dirty Lens* (canvas `blur(3.8px)`, emulated in Python) | 12.7 – 13.8 |
| Simulator *Dirty Lens*, observed in the browser | 15.81 and 15.72 |
| Simulator *Pink IR Filter*: browser / Python emulation | 170.03 / 189.19 (tint flagged) |
| Simulator *Night mode (IR LEDs)*, observed in the browser | 601.71 (nominal: night mode is not a fault) |
| Simulator *50/60Hz Flicker*: browser / Python emulation | 626.32 / 759.59 (banding flagged) |
| `optical_obstruction.mp4` | 0.74 |
| `bad_internet.mp4` (320x240, native resolution, CLI) | 536.07 |

  The threshold sits between Dirty Lens and sharp frames, with margin on both sides. It is not a
  scientific calibration: only one real scene was measured. Infrared noise and banding raise the
  Laplacian variance, so a high score does not prove the absence of blur; a low-texture scene (a plain
  wall, night) can score low without blur.

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
# Only for temporary credentials (access key IDs starting with ASIA):
# AWS_SESSION_TOKEN=
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0

# Data source: simulator (default, Digital Twin) | partner (official Ring Partner API)
RING_DATA_SOURCE=simulator

# Ring Partner API (used only when RING_DATA_SOURCE=partner)
RING_PARTNER_ACCESS_TOKEN=
RING_PARTNER_DEVICE_ID=
```

**Order of precedence** (read once, when the server or the CLI starts): a variable set in the shell
wins over `.env`; `.env` wins over the defaults (`RING_DATA_SOURCE` defaults to `simulator`);
`PYTHON_DOTENV_DISABLED=1` skips `.env` entirely. Restart the server after editing `.env`. At start
the server prints where the source came from, for example
`[Ring] Data source: partner (from .env)` or
`[Ring] Data source: simulator (from shell environment variable (overrides .env))`.
PowerShell keeps `$env:` variables for the whole window, so a `$env:RING_DATA_SOURCE` or
`$env:PYTHON_DOTENV_DISABLED` set by an earlier command (such as the demo commands below) still
overrides `.env`; clear them before using the `.env` value:
```powershell
Remove-Item Env:RING_DATA_SOURCE, Env:PYTHON_DOTENV_DISABLED -ErrorAction SilentlyContinue
```

`.env.example` documents the Partner polling settings (`RING_PARTNER_POLL_SECONDS`,
`RING_PARTNER_OFFLINE_ALERT_READS`, `RING_PARTNER_OFFLINE_ALERT_MAX_GAP_SECONDS`); they are
prototype defaults, not Ring recommendations.
Two optional Partner settings: `RING_PARTNER_HARDWARE_ID` (the `Hardware_Id` header Ring requires;
default: a UUID5 derived from the computer name and `RING_PARTNER_DEVICE_ID`, stable between runs) and
`RING_PARTNER_BASE_URL` (default `https://api.amazonvision.com`; the tests point it at a local fake server).

AWS access with `bedrock:InvokeModel` permission for the configured Anthropic model is
required for the primary diagnosis path. Explicit `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
are used when both keys are set; otherwise boto3 uses its standard credential chain (profiles, SSO,
roles).

**Temporary credentials** (access key IDs that start with `ASIA`, for example from AWS SSO, STS or a
lab account) also need `AWS_SESSION_TOKEN`: uncomment it in `.env`, or set it in the shell
(`$env:AWS_SESSION_TOKEN = "..."` in PowerShell, `export AWS_SESSION_TOKEN=...` in bash) together with
the two keys. Long-term keys (`AKIA...`) do not need it. The token is only used when it is set and both
keys are set. Temporary credentials expire: when they do, every anomaly falls back to the local mock-up
until you refresh them. If Bedrock is unavailable or returns a report
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
`.env` (python-dotenv never overrides them), and they stay set in that PowerShell window: run the
`Remove-Item` line from the configuration section before starting with the `.env` values (for example
`RING_DATA_SOURCE=partner`).

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
* Inject hardware faults in real time (*Dirty Lens*, *Pink IR Filter*, *50/60Hz Flicker*, *Transformer Cut*) and switch *Night mode (IR LEDs)*, a normal condition that the pre-check reports as nominal.
* Adjust parametric sliders (RSSI, Battery, Transformer Voltage, WebRTC Jitter, Packet Loss, Bitrate).
* Monitor autonomous 15s heartbeats, smart push alerts, and inspect the Ring-informed troubleshooting knowledge base.

Each report has two views:
* **Customer view** (what the Ring app would show): status, problem type (for example
  "Network Issue (Wi-Fi / Upstream)", "Power Supply Issue", "Camera Image Issue"), confidence and the recommended
  mitigation in plain language. The push banner shows only a title and one short line.
* **Technical details (for support ticket)**, collapsed: telemetry and visual findings, engine (Bedrock,
  local fallback or pre-check), model, deterministic severity level, retrieved KB articles, mitigation
  source ("project knowledge base (Ring-informed, unverified)" or "generated, no KB match") and the
  device ID, with a *Copy ticket details* button. The device ID is never shown in the customer view.

The *Autonomous AI Reasoning Engine* panel names the engine of the **last** diagnosis: "No diagnosis
yet" at start and after Reset / Clear, "Not configured (using local fallback)" when the server has no
AWS credentials, then "AWS Bedrock (Claude)", "Local fallback (mock-up)" or "Local pre-check (no
Bedrock call)". In standby the camera monitor shows "Camera standby • no stream", no frame rate, and no
*Stop Camera* button (there is no stream to stop); the button appears while a session is active.

In Partner mode the Digital Twin controls are disabled, the metric cards show only values
Ring reported (`N/D` otherwise), the camera monitor shows no video except during a Ring live view, and diagnoses run with
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
The CLI always prints the four steps `[1/4]` to `[4/4]` (a skipped step says why, for example
"[3/4] Pre-check nominal: skipping SOP retrieval" and "[4/4] No anomaly: reasoning engine not invoked")
and ends with the report JSON, including `_provider` (`bedrock`, `local_fallback` or `precheck`) and
`_model`, plus the problem type. A nominal case prints an explicit `HEALTHY` report from the pre-check
(also returned by `/api/analyze`, with `analysis_skipped: true`), never `null`.
On Linux/macOS, with the virtual environment activated, use `python` instead of `.\venv\Scripts\python.exe`.

### Option C: Run Tests

Run the automated test suite from the repository root:

```powershell
.\venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --test tests/frontend/ui.test.mjs tests/frontend/whep.test.mjs
```

The tests replace the Ring Partner API with local fakes or a server on 127.0.0.1 and replace
the Bedrock call with test doubles. `tests/conftest.py` blanks Ring and AWS credentials and makes
any real boto3 client creation fail, so the suite needs no credentials or network and incurs no
Bedrock charges. The Node tests run the page scripts against a fake DOM (no real browser); on
Windows pass the file path to `node --test`, not the directory. The Node output contains traces such
as HTTP 504, HTTP 429 and "Ring credentials are configured but the LIVE adapter failed to connect":
those errors are simulated on purpose by the tests (they check how the UI handles them), not failures.
Current totals: 869 pytest tests and 195 Node tests, all passing.

#### Verification status

* **Verified live against the Ring Developer Playground test device:** image
  snapshots, recorded clips ("Analyze last recording"), RTSPS live stream stats and the Ring live view
  (WHEP through this server). Bedrock analyses were verified live against AWS Bedrock with the simulator.
* **Verified only with the test tool and fixtures:** signed webhooks (`tools/send_test_webhook` and signed
  fixtures in the tests). Live Ring webhooks need a public HTTPS URL and a registered app, and were not
  received.
* Everything else in this README is covered by the automated tests above (local fakes and test doubles),
  not by a live check.

### Demo walkthrough (B1–B5)

Simulator mode (`RING_DATA_SOURCE=simulator`), Web UI. Without AWS credentials the diagnosis is
the labelled `local_fallback`; with them, anomalies are sent to Bedrock (billed).

* **Alerts, recovery and Reset (B1/B2):** drag Wi-Fi RSSI to -84 dBm (Poor, DEGRADED). The heartbeat
  counts 1/3, 2/3, 3/3 (15 s each) and runs one diagnosis. At -90 dBm (CRITICAL) it diagnoses on the
  first heartbeat. Back to -70 dBm the incident stays open; at -50 dBm the alert hides and the report is
  kept, greyed out and marked *Resolved*. **Reset** clears both. A late answer for a resolved incident
  is discarded.
* **Battery (B3):** select *Battery Only*: 9 % is CRITICAL (first heartbeat), 10–29 % is Low (DEGRADED,
  3 heartbeats), 30 % or more is GREEN.
* **Voltage retrieval (B4):** a wired transformer at 0.5 V or less is CRITICAL (first heartbeat), above
  0.5 V and below 12 V is DEGRADED (3 heartbeats), 12 V or more is GREEN for a generic wired device.
  Retrieval per voltage is covered by `tests/test_voltage_retrieval.py`.
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

  B5 exists only in the simulator, the CLI and the API. In Ring's documentation "Chime" is a separate
  device (audio, volume, snooze); no Ring field with the in-home chime type of a doorbell was verified,
  and in Partner mode this data does not exist.

### Local fallback (mock-up) vs Bedrock (realistic mode)

* **Bedrock (realistic mode)** is the diagnosis this project is built around: Claude Haiku 4.5 reads the
  keyframe, the metrics, the pre-check anomalies with their levels and the retrieved KB articles, and
  writes the findings and a plain-language mitigation. It is the most realistic and precise mode.
  Severity and root-cause domain are derived from the deterministic pre-check; the model only provides
  the reasoning. A different model domain is replaced by the pre-check's dominant domain (worst level,
  then table order, as for the heartbeat incident) and reported as `domain_source: "precheck_override"`
  under Technical details (`"model"` when they agree, `"precheck"` for the local fallback).
  Its answer must respect the report contract (`src/agent/concierge.py`):
  * valid schema, and not HEALTHY when the pre-check found an anomaly;
  * a customer mitigation of at most 60 words without technical terms, KB IDs, internal field names,
    electrical specifications (volts, amps, VA) or visual causes the analyzer cannot observe (film,
    condensation, glare, reflections, obstructions, scratches);
  * no electrical work. Safe-check policy: the customer may restart the doorbell following the steps in
    the Ring app and, for battery or charging problems only, make sure the charging cable or plug adapter
    is firmly connected, followed by "Don't open the doorbell or touch any wires; if the problem
    continues, contact a qualified electrician or Ring Support." Turning off or cutting the power, power
    cycles, unplugging, fuses, breakers and any action on wires, wiring, terminals, connections or the
    transformer are rejected. Transformer, wiring and chime cases allow only the Ring app restart and a
    referral to a qualified electrician or Ring Support;
  * findings without internal field names; `visual_findings` about image quality only (no people,
    faces, clothing, objects, vehicles, license plates, buildings or vegetation); a sharpness score at or
    above 40 is never called low; and no numerically false "below / above the X threshold" claim.

  What reaches the model is sanitized first: KB actions and facts lose electrical figures, electrical
  work and unobservable causes (unless the analyzer reports them), "power cycle" becomes a restart from
  the Ring app, the case uses readable names instead of field names, and a METRIC LEVELS section gives
  every measured metric its precomputed level and its GREEN boundary (the improvement target, e.g.
  "at or above -60 dBm" for a Poor RSSI). With a transformer power cut and a black frame, only the power
  article (KB-RING-009) is retrieved and the black frame is described as a symptom of the power loss.

  A violation gets **one** corrective retry. If the corrected answer breaks only the customer text,
  Bedrock's report is kept and the mitigation is replaced by the deterministic KB message
  (`mitigation_source: "knowledge_base_deterministic"`, or `"generic_deterministic"` without a KB
  match), shown under Technical details. Any other second violation, or a call error (credentials,
  permissions, region, throttling), uses the local fallback.

  Live verification (12 cases against `us.anthropic.claude-haiku-4-5-20251001-v1:0`, 12 Bedrock
  calls): 0 retries (the first round had 4 of 14), 0 deterministic replacements and 0 fallbacks. One
  validator gap was found and closed (mounting advice about obstructions or reflective surfaces for a
  dirty lens); the nominal night-mode frame did not call Bedrock.
* **Local fallback (mock-up)** (`_provider: "local_fallback"`) is a deterministic stand-in that validates
  the report contract and the end-to-end functions without AWS. It uses the same schema and the same
  severity table, lists every detected anomaly, and takes the mitigation from the plain-language
  `customer_messages` of the matching KB article (or a generic message when none matches). Its wording is
  generic by design and its confidence is a fixed 0.86. Use Bedrock for realistic diagnoses.
* **Pre-check** (`_provider: "precheck"`): nominal, offline and insufficient-data verdicts are decided
  without calling Bedrock.

### Ring media constraints: required headers, watermark, encrypted video

Partner mode only; the simulator is unchanged.

* **Required headers:** every Ring request sends `User-Agent: RingTelemetryOps/<version>` and a
  `Hardware_Id` UUID (`RING_PARTNER_HARDWARE_ID`, or a stable UUID5 derived from the computer name and
  `RING_PARTNER_DEVICE_ID`). An HTTP 406 is shown as "Ring blocked the request: required headers are
  missing, the request came from a datacenter IP, or Ring Security blocked this IP (not retryable; contact
  Ring developer support)"; it is not retried and never switches the data source or substitutes simulated data. `X-RateLimit-Remaining`
  is read and `Retry-After` is honoured on 429.
* **Error reasons:** `/api/ring/status` returns `error_reason` (`missing_token`, `token_rejected`,
  `blocked_406`, `time_range_not_authorized`, `rate_limited`, `device_offline_503`, `network_error`,
  `api_error`); the device card shows a one-line explanation under the "Ring credentials error" header.
  The token is never shown.
* **403 before consent:** a 403 whose JSON:API `code` is `TIME_RANGE_NOT_AUTHORIZED` (media download or
  Event History) means the requested time is earlier than the user's consent, not a bad token. Its reason is
  `time_range_not_authorized` ("Ring only allows access to recordings made after you connected this app; the
  requested time is earlier."); "Analyze now" treats it as a failed candidate and tries the next event or
  `latest_in_range`. A 403 without that code is still `token_rejected`.
* **Device settings:** one `GET /v1/devices/{id}?include=status,capabilities,configurations,location`
  per device fills `device_context` (end-to-end encryption, motion detection and zones, night vision,
  HDR, privacy zones, video resolutions, coarse location, audio snooze). Values Ring does not report stay
  `null`. If that call fails or has no `included` data, the individual status and capabilities calls run
  as before. The settings appear on the device card, in the report's Technical details and, without IDs,
  in the Bedrock prompt as context; they do not change severity.
* **Encrypted video:** when Ring reports `e2e_encryption.enabled: true`, `device_context.media_readable`
  is `false`; the card and the report's visual findings say image analysis is not possible and the
  diagnosis uses device status only. It is not an error and is not retried.
* **Supported settings only:** when the capabilities list the image enhancements, a setting missing from
  that list is `null` (shown as "n/a"), not "off". `device_context.snapshot_supported` is true only when
  the list includes `snapshot`; `features.snapshot` in `/api/ring/status` also requires readable video.
* **Snapshot (two steps, server to server):** "Analyze now" requests the image at the start of the newest
  known event (`at_timestamp`), tries up to two older events when Ring returns no usable image (about 20 s in
  total), and falls back to the latest image of the last 24 h (`POST .../media/image/download`, JPEG, no
  resolution so the watermark covers the least, the first component on multi-camera devices). Ring answers `303` with a pre-signed URL, which the server fetches
  **without** the access token and reads `X-Media-Timestamp` / `X-Media-Origin`. Only a decodable JPEG or
  PNG of at most 10 MB is used. "Not ready" (425) is retried for up to about 15 s, 500/503 once; "no recent
  recording" (416), "could not decrypt" (422 `GRECO_NO_VALID_KEY`), "damaged recording" (422
  `CORRUPT_RECORDING`) and an unreadable image are normal outcomes, each explained in the visual findings
  and the mitigation. No
  snapshot is requested for encrypted video or a device without snapshot support.
* **Watermark:** Ring media carries a mandatory watermark in its top corners, which would inflate the
  sharpness score and the banding measure. Ring snapshots are measured without the top
  `RING_WATERMARK_BAND_PX` rows (100 px, at most 35 % of the height; a project heuristic); the simulator
  and the sample videos are measured as before.
* **In memory:** Ring snapshots are processed in memory and are never written to disk or logged. The one
  exception is a recorded clip, which passes through a temporary file deleted in a `finally` block (see
  "Recorded clips"). The last snapshot is kept in memory and
  served by `GET /api/ring/snapshot` (`Cache-Control: no-store`) for the Partner monitor, which shows it
  with Ring's time and origin.
* **What reaches Bedrock:** a Ring image is sent only when the pre-check finds an image problem (blur,
  tint, banding, brightness). A healthy frame is reported as "image OK" and never leaves the server, even
  when another metric (for example a low battery) calls Bedrock.
* **Event History (polling):** each Partner poll cycle reads
  `GET /v1/history/devices/{id}/events` at most once (types from `RING_PARTNER_EVENT_TYPES`, default
  `ding,motion,on_demand`; up to 2 pages; `Retry-After` honoured) and lists the last 20 events under
  "Ring events". An empty list is normal: Ring returns only events after the user's consent. A new event
  (unseen ID, started after the server) is analyzed at once with the snapshot at its start; it stays silent
  unless there is a problem, then the report is titled "Issue during Doorbell Press / Motion Detection /
  Live View". Events from before the server started are listed, not analyzed.
* **Per-domain verdict:** Partner reports carry `checked_domains` (image `ok` / `issue` / `not_available`;
  network and power `not_available`, because Ring does not share Wi-Fi or power data with partner apps).
  `diagnostic_status` is unchanged; the customer view says "Partial check" or "Not enough data" and the
  mitigation is written for the customer.

#### Recorded clips (M2): "Analyze last recording (5 s)"

* **Request:** `POST /api/ring/clip-analysis` asks Ring for 5 s without audio from the start of the newest
  known event (`POST /v1/devices/{id}/media/video/download`, one `components` entry on multi-camera
  devices). Ring's documentation describes two replies and both are handled: `200` / `206` (partial,
  `X-Media-Length`) with the `video/mp4` body, or a `301` / `302` / `303` redirect whose pre-signed URL is
  fetched **without** the access token. 416 / 425 are retried for about 20 s in total, 422 is classified as
  for snapshots (`GRECO_NO_VALID_KEY` = could not decrypt), 403 `TIME_RANGE_NOT_AUTHORIZED` is explained,
  401 / 403 / 406 keep their reasons. Only an MP4 (`ftyp` box) of at most 20 MB is analyzed. No clip is
  requested for end-to-end encrypted video or without a known event.
* **Temporary file (documented exception):** OpenCV cannot read an MP4 from memory, so the clip is written
  to a temporary file in the system temp directory (created with `mkstemp`: owner-only permissions where
  the OS supports them). It is deleted in a `finally` block, also when the analysis fails, and its path is
  never logged or returned. This is the only exception to "Ring media stays in memory".
* **What is measured:** duration (container frame count / fps), effective fps (frames actually decoded over
  that duration), resolution and approximate bitrate (file size / duration). The three sharpest non-black
  frames, measured without the watermark band, get the same image check as a snapshot: a healthy image
  stays on the server, an image problem gets the same report and the same privacy rule.
* **Not network metrics:** the UI shows "Recording stats (from Ring recording, not live network): 5.0 s ·
  N fps · WxH · X Mbps" and the image verdict. These numbers describe the recording; they never fill the
  Wi-Fi, jitter or packet-loss cards. The funnel counts clips requested, OK and failed by cause.

#### Live stream stats (M4, best effort): "Live stream stats (8 s)"

* **Implemented; verified live against the Ring Developer Playground test device.** The tests use a test
  double; whether a given OpenCV build can read the stream still depends on its FFmpeg and TLS support.
* `POST /api/ring/live-stats` opens `rtsps://video.rtsp.amazonvision.com:322/v1/devices/{id}/stream` with
  OpenCV / FFmpeg over TCP (`OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp`, set only around the open
  and then restored). The token goes in `?token=`, because FFmpeg truncates long credentials in the URL user
  part; the URL is never logged or returned. It reads for about 8 s (at most 10; Ring ends sessions after
  30 s on battery and 60 s on line power) and reports effective fps, frame count and resolution. The bitrate
  is not measured (OpenCV does not expose it). The capture is always released.
* Not started for an offline or end-to-end encrypted device. When this OpenCV build cannot read the stream
  the UI says "Live stream stats: not available (reason)": `rtsps_unavailable` (no FFmpeg / TLS, refused,
  or rejected credentials, which OpenCV does not tell apart) or `rtsps_timeout`. That is not an app error.
* The result is labelled "Live stream as received by this server": it is not the jitter or packet loss of
  the home network.

#### Ring live view (M3, WHEP): "Ring live view (up to 60 s)"

* **Implemented; verified live against the Ring Developer Playground test device.** The automated tests
  use a fake WHEP server and a stubbed RTCPeerConnection (no real browser).
* **Why this server is in the middle:** Ring's API only accepts server-to-server calls (browsers are blocked
  by CORS). The browser builds the SDP offer (receive-only video, no audio, public STUN
  `stun:stun.l.google.com:19302`, handlers attached before the offer, ICE gathering awaited for at most 4 s)
  and sends it to `POST /api/ring/whep/session`. This server checks it (at most 64 KB, `m=video`,
  `a=recvonly`; `sendrecv` / `sendonly` get a 400), forwards it to
  `POST /v1/devices/{id}/media/streaming/whep/sessions` (`Content-Type: application/sdp`, the token,
  `User-Agent`, `Hardware_Id`, `component_id` on multi-camera devices) and returns only `session_id`
  (128 random bits), the SDP answer and `max_seconds_hint` (30 s battery, 60 s wired). The browser never
  receives the token or Ring's session URL (`Location`), and neither is logged.
* **Session URL check:** the `Location` is used only if it is HTTPS (plain HTTP only on `127.0.0.1`, for
  tests) on exactly Ring's API host; otherwise the session is dropped with `invalid_session_url` and nothing
  (no `Authorization`) is sent there.
* **What it does:** shows Ring's live video on the Partner monitor in place of the snapshot ("Ring live view ·
  started HH:MM:SS", a countdown, **Stop**) and a line under the monitor with the browser's WebRTC stats:
  "Live stream as received by this browser (Ring cloud to browser, not the doorbell's Wi-Fi): N fps · WxH ·
  X Mbps · jitter N ms · lost N" (`getStats()` every second), kept as "Last live session: ..." afterwards.
  That line is informative only: it never fills the Wi-Fi, battery, jitter or packet-loss cards or the
  heartbeat, and it is separate from the server's RTSPS line above.
* **What it does not do:** it does not analyze or store the browser's frames (in Partner mode client frames
  are never presented as Ring data), and it makes one attempt per click (no automatic reconnection).
* **One session at a time:** a new request closes the previous session first. The browser closes its
  session (`DELETE /api/ring/whep/session/{session_id}`, with `keepalive` on page unload) on Stop, error,
  `failed` / `disconnected` or the end of the session; Ring's 404 on that close means it had already ended.
  A session the browser does not close is closed by this server after 65 s, and all are closed when the app
  shuts down. Errors keep the existing reasons (`token_rejected`, `blocked_406`, `rate_limited`,
  `device_offline_503`, `network_error`, ...) and are shown as "Ring live view is not available: ...". The
  funnel counts sessions started, failed by reason and closed by the timer.

#### Alignment with Ring's Computer Vision Guidelines

* **Funnel:** the cheap local pre-check (telemetry levels and OpenCV image checks) runs first; Bedrock is
  called only when it finds an anomaly.
* **Frame selection:** frames are judged by sharpness (variance of the Laplacian) and brightness, with the
  watermark band excluded, before any model sees them.
* **Guardrails:** Bedrock answers are validated against the diagnostic contract (allowed values, findings
  and customer-text rules, one corrective retry) with a deterministic fallback when they still fail.
* **Privacy:** Ring snapshots are processed in memory (a recorded clip only passes through a temporary
  file deleted in a `finally` block), never stored or logged, and sent to Bedrock only for
  an image anomaly; unreadable (encrypted) media is an expected outcome, not retried.

### Webhooks (implemented and tested with signed fixtures; not verified against live Ring webhooks, which need a public HTTPS URL and a registered app)

* **Endpoint:** `POST /webhooks/ring`, active only with `RING_DATA_SOURCE=partner` (404 otherwise).
* **Signature:** `X-Signature: sha256=<hex>` is checked with HMAC-SHA256 over the raw body bytes and the
  app's signing key in `RING_PARTNER_HMAC_KEY` (constant-time compare), before the JSON is parsed. A missing
  or wrong signature is 401 and nothing is processed. **Without the key the endpoint answers 503** and never
  accepts unsigned messages. Bodies above `RING_WEBHOOK_MAX_BODY_BYTES` (default 64 KB) are 413; malformed
  JSON with a valid signature is 400.
* **Deduplication:** Ring may deliver the same message again; a repeated `meta.request_id` (last 1000 kept
  in memory) answers 200 without new work. An event that arrives both as a webhook and through Event History
  (same device, type and start within 10 s) is analyzed once.
* **Fast response:** Ring expects a 2xx within 5 s, so the receiver answers 200 at once and analyzes
  `motion_detected` and `button_press` afterwards (FastAPI `BackgroundTasks`), exactly like a new Event
  History event: the snapshot at `data.attributes.timestamp`, silence when healthy, and a report titled
  "Issue during Motion Detection / Doorbell Press" (delivered to the UI by the next events poll) when there
  is a problem. `device_online`, `device_offline`, `device_added` and `device_removed` only update in-memory
  state; unknown types answer 200 and are ignored.
* **Sub-type filter:** `RING_WEBHOOK_SKIP_SUBTYPES` (comma-separated, empty by default) skips
  `motion_detected` sub-types such as `vehicle` (Stage 0 of Ring's vision funnel). A skipped event is not
  analyzed when Event History lists it either.
* **Local demo:** `python -m tools.send_test_webhook --type motion_detected|button_press|device_offline`
  signs a v1.1 payload with the key from the environment or `.env` (never printed) and posts it to
  `http://127.0.0.1:8000/webhooks/ring` (`--timestamp-ms`; default: the newest listed event, else now).

### Funnel metrics

`GET /api/metrics` returns counters since the server started: analyses, pre-check nominal ("healthy
frames kept on server": the frame was checked as healthy and not sent to Bedrock, including a Partner
"Partial check" whose image was OK), pre-check anomaly, insufficient data, Bedrock calls and retries (a call that failed
before the fallback counts), local fallbacks, snapshots OK and failed by cause, new events (Event
History or webhooks) and feedback votes, plus median and p95 latency per engine (`bedrock`,
`local_fallback`, `precheck`) over the last 100 analyses, and recorded clips requested, OK and failed
by cause (`clips_requested`, `clips_ok`, `clips_failed`). They live in memory only and reset on restart;
no device IDs, images or free text. The engine panel shows them as "Funnel (this session)", refreshed
after each analysis from the UI and after each Ring events poll (event and webhook analyses run in the
background).

### User feedback

Each report carries a server-generated `report_id`. Under the mitigation the UI asks "Was this helpful?"
(👍 / 👎, with an optional comment of up to 200 characters after 👎); one vote per report.
`POST /api/feedback` appends one JSON line to `data/feedback/feedback.jsonl` with the time, `report_id`,
engine, severity, domain, rating and comment: no image, device ID or IP address. `data/feedback/` is
ignored by git. Only the last 100 reports accept votes, at most 30 votes per minute; if the file cannot
be written the vote is answered with 503 and can be sent again.

### Known limitations

* **Voltage rule vs the KB:** the KB (unverified, pending re-verification) lists 8-24 V AC for
  battery-powered Doorbell 1/2/3/4 used with wiring; the
  project's 12 V undervoltage rule is a diagnostic heuristic of this project, not a Ring requirement
  (O5 / N38).
* **Live View duration:** Ring's API documentation limits streams to up to 30 s on battery-powered
  devices and up to 60 s on line-powered devices. The simulator does not model this: its Live View stays
  open until closed, and Ding / PIR sessions last 7 s (O6 / N22).
* **Sharpness is not comparable across resolutions:** `bad_internet.mp4` scores 536 at its native
  320x240 in the CLI, but the same frame upscaled to 1280x720 scores 34 (bilinear), 46 (bicubic) or 145
  (nearest neighbour). The threshold of 40 is calibrated for sources of 720 px or more.
* **Night mode + Dirty Lens is a false negative:** with both active, the measured sharpness was 219.6
  (above the threshold of 40) and the dirty lens was not detected. Night mode alone scored 601.71,
  629.78, 605.66 and 580.77, the flicker 615.31 – 626.32, and the daytime Dirty Lens 15.81, 15.72 and
  13.68: infrared noise and banding inflate the Laplacian variance. The metric and its threshold are
  unchanged by design.
* **Dropped frames:** the "210/450 frames dropped" Bedrock may cite comes from the simulated
  `wifi_degradation` scenario. The Twin sliders do not simulate dropped frames, so slider telemetry
  reports them as unavailable ("not simulated by the Digital Twin sliders").
* **Ring banding calibration:** the periodicity gate for Ring frames was checked on three Ring
  Playground frames and synthetic scenes only; flicker in real Ring media has not been observed yet.
* **Undecryptable Ring recordings:** Ring can answer the pre-signed image download with
  `422 GRECO_NO_VALID_KEY` (not in its documentation) for some recordings, including the one behind
  "latest image of the last 24 h". That is why "Analyze now" starts from the newest event and tries up to three.
* **Confidence** is self-assessed by the model (Bedrock) or fixed (0.86, fallback); see above.
* **Mechanical in-home chime (B5)** is simulator / CLI / API only (see the demo walkthrough).

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
│       ├── good_internet.mp4                 # 1080p 25 fps baseline test stream (9.8 s)
│       ├── bad_internet.mp4                  # Degraded low-bitrate test stream
│       └── optical_obstruction.mp4           # Obstructed-lens test stream
├── src/
│   ├── config/
│   │   ├── severity_thresholds.json          # Single severity table (GREEN / DEGRADED / CRITICAL)
│   │   └── severity.py                       # Classifies metrics with that table
│   ├── agent/
│   │   ├── concierge.py                      # Autonomous Bedrock LLM Orchestrator
│   │   └── knowledge_retriever.py            # Multimodal RAG Engine
│   ├── api/
│   │   ├── funnel.py                         # In-memory funnel counters (GET /api/metrics)
│   │   ├── server.py                         # FastAPI Web Server & REST endpoints
│   │   └── whep.py                           # Ring live view: SDP offer checks, WHEP sessions, 65 s timer
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
│   │   ├── telemetry.js                      # Live slider & badge synchronizer
│   │   └── whep.js                           # Ring live view (WHEP via this server) and WebRTC stats line
│   └── index.html                            # Digital Twin interactive interface
├── tests/
│   ├── conftest.py                           # Blanks Ring/AWS credentials, blocks real boto3 clients
│   ├── test_*.py                             # pytest suites (API, agent, KB, Partner client, B1–B5)
│   ├── frontend/ui.test.mjs                  # Node tests for the web UI (fake DOM)
│   └── frontend/whep.test.mjs                # Node tests for the Ring live view (stubbed RTCPeerConnection)
├── tools/
│   └── send_test_webhook.py                  # Sends one signed test webhook to the local server
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