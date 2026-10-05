# Ring TelemetryOps: Autonomous Diagnostic & Remediation Agent

An autonomous diagnostic agent designed for smart home video doorbells (Amazon Ring). It integrates deterministic computer vision telemetry extraction with Retrieval-Augmented Generation (RAG) and Multimodal LLMs via Amazon Bedrock to identify and remediate video streaming degradation in real time.

---

## Architecture Overview

```
+------------------+      Deterministic Metrics
|   Video Stream   |  ===========================>  +-------------------------+
|  (RTSP / MP4)    |  (Resolution, FPS, Bitrate,    | Video Telemetry Engine  |
+------------------+         Laplacian Blur)        |        (OpenCV)         |
         |                                          +-------------------------+
         |                                                       |
         v                                                       v
+------------------+                                +-------------------------+
| Evidence Capture |                                |  State Classifier       |
|  (Degraded Frame)|                                |  (OPTIMAL vs DEGRADED)  |
+------------------+                                +-------------------------+
         |                                                       |
         |         +-----------------------------+               |
         |         | Knowledge Base (Ring SOPs)  | <-------------+ (If Degraded)
         |         +-----------------------------+
         |                        |
         |                 Retrieved Guides
         v                        v
+-----------------------------------------------------------------------------+
| Amazon Bedrock (Anthropic Claude 3 Haiku / Multimodal Inference)            |
| Correlates visual frame artifacts with telemetry metrics and technical SOPs |
+-----------------------------------------------------------------------------+
                                       |
                                       v
                     +-----------------------------------+
                     | Actionable Diagnostic Report      |
                     | (Root cause & remediation steps)  |
                     +-----------------------------------+
```

---

## Key Features

* **Deterministic Telemetry Extraction:** Real-time computation of video resolution, effective FPS, bitrate, and sharpness score via Laplacian variance.
* **Efficient Branching:** Healthy streams (`OPTIMAL`) exit immediately without unnecessary LLM or retrieval overhead.
* **Context-Grounding (RAG):** Evaluates degradation against official standard operating procedures (SOPs) to prevent hallucinations.
* **Local Fallback Mode:** Operates with deterministic rule matching and offline structured reporting when AWS credentials are not configured.

---

## Diagnostic Benchmarks

| Metric | Healthy Stream Threshold | Degraded Sample | Optimal Sample |
| :--- | :--- | :--- | :--- |
| **Resolution** | $\ge$ 1280x720 (720p) | 320x240 | 1920x1080 |
| **Frame Rate** | $\ge$ 20.0 FPS | 5.0 FPS | 25.0 – 30.0 FPS |
| **Bitrate** | $\ge$ 500 kbps | ~103 kbps | > 5000 kbps |
| **Status** | **OPTIMAL** | **DEGRADED** | **OPTIMAL** |

---

## Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/](https://github.com/)<your-username>/ring-concierge-agent.git
   cd ring-concierge-agent
   ```

2. **Set up the virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate   # Linux/macOS
   # On Windows:
   venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Configuration:**
   Copy the example environment file:
   ```bash
   cp .env.example .env
   ```
   Add your AWS credentials to `.env` if testing Amazon Bedrock inference. If left empty, the agent automatically executes in **Local Fallback Mode**.

---

## Usage

Run the agent on any video stream using the `--video` parameter:

### Test Healthy Stream
```bash
python -m src.agent.concierge --video data/samples/good_internet.mp4
```

### Test Degraded Stream
```bash
python -m src.agent.concierge --video data/samples/bad_internet.mp4
```

---

## Project Structure

```text
ring-concierge-agent/
├── data/
│   ├── knowledge_base/
│   │   └── ring_video_troubleshooting.json
│   └── samples/
│       ├── test_stream_good.mp4
│       └── test_stream_bad.mp4
├── src/
│   ├── agent/
│   │   ├── concierge.py
│   │   └── knowledge_retriever.py
│   └── video_pipeline/
│       └── analyzer.py
├── .gitignore
├── .env.example
├── README.md
└── requirements.txt
```