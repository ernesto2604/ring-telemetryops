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
   * `partner`: the official **Ring Partner API**, read-only: `GET /v1/devices`, `GET /v1/devices/{id}/status` and `GET /v1/devices/{id}/capabilities`, as listed in the [Ring Partner API documentation](https://developer.amazon.com/docs/ring/api-documentation.html) (endpoint paths checked against the documentation on 2026-10-21; the response fields the client reads were not re-verified). Ring's API documentation also lists the Event History (`GET /v1/history/devices/{device_id}/events`), Device Configuration (`GET /v1/devices/{device_id}/configurations`) and WHEP live video (`POST /v1/devices/{device_id}/media/streaming/whep/sessions`) endpoints; this project does not implement them. The KB marks those three paths as matching the documentation (checked 2026-10-21) while the fields it attaches to them, and the device attributes it names, stay `unverified`. It reports `online`, battery percentage (only when capabilities declare battery support) and the last check-in time (`reported_at`). It does **not** provide RSSI, transformer voltage or WebRTC statistics, so those stay unknown. Diagnoses are telemetry-only: Ring image retrieval, live video (WHEP), motion events and webhooks are **not implemented**, and frames or telemetry sent by the client are never presented as Ring data.
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

`.env.example` documents the Partner polling settings (`RING_PARTNER_POLL_SECONDS`,
`RING_PARTNER_OFFLINE_ALERT_READS`, `RING_PARTNER_OFFLINE_ALERT_MAX_GAP_SECONDS`); they are
prototype defaults, not Ring recommendations.

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
node --test tests/frontend/ui.test.mjs
```

The tests replace the Ring Partner API with local fakes or a server on 127.0.0.1 and replace
the Bedrock call with test doubles. `tests/conftest.py` blanks Ring and AWS credentials and makes
any real boto3 client creation fail, so the suite needs no credentials or network and incurs no
Bedrock charges. The Node tests run the page scripts against a fake DOM (no real browser); on
Windows pass the file path to `node --test`, not the directory. The Node output contains traces such
as HTTP 504, HTTP 429 and "Ring credentials are configured but the LIVE adapter failed to connect":
those errors are simulated on purpose by the tests (they check how the UI handles them), not failures.

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