# Developer Friction Log – Ring APIs & Amazon Bedrock Integration

### Entry 1: WebRTC W3C Telemetry Extraction in Ring Video Pipelines
- **Specific task attempted:** Extract real-time WebRTC metrics (`jitter`, `packetsLost`, `roundTripTime`) directly from Ring WHEP streaming sessions for programmatic network diagnostics.
- **Steps taken:** Attempted to bind to the active WebRTC stream session generated via Ring Developer partner endpoints using automated backend consumers.
- **Expected vs. actual result:** Expected a low-overhead telemetry endpoint or exposed RTCPeerConnection stats block. Actual result: Partner streams enforce fixed session timeouts (30–60s) without an out-of-the-box telemetry socket.
- **Severity rating:** Major
- **Workaround used:** Designed a synthetic telemetry bridge in `src/utils/telemetry_simulator.py` that mirrors the exact W3C WebRTC `getStats()` dictionary and Ring device health schema.
- **Actionable suggestion:** Expose a standardized WebRTC telemetry webhook/event-stream in the Ring Developer Portal so diagnostics engines can inspect connection quality without active video decoding.

---

### Entry 2: Bedrock Multimodal Latency with High-Resolution Video Frames
- **Specific task attempted:** Send 1080p doorbell snapshot frames alongside JSON telemetry to Amazon Bedrock (Anthropic Claude 3.5 Sonnet / Claude 3 Sonnet) for visual failure analysis.
- **Steps taken:** Converted raw raw PNG video frames directly to Base64 and sent them via `boto3` `invoke_model`.
- **Expected vs. actual result:** Expected sub-second response times. Actual payload caused 3.8s–5.2s round-trip latency, too slow for interactive triage.
- **Severity rating:** Major
- **Workaround used:** Implemented in-memory dynamic frame preprocessing in `src/video_pipeline/analyzer.py` using OpenCV to downscale frames to 1280px width and compress to JPEG at 80% quality, reducing latency to ~1.1s.
- **Actionable suggestion:** Provide native AWS SDK frame-buffer helpers or edge-optimized image preprocessors tailored for smart camera event triage.

---

### Entry 3: Schema Synchronization in Local Agent MCP Development
- **Specific task attempted:** Expose local Ring troubleshooting knowledge bases to the agent orchestrator using MCP servers.
- **Steps taken:** Integrated custom JSON troubleshooting rules via dynamic MCP tools alongside local RAG retrievers.
- **Expected vs. actual result:** Minor schema divergence when streaming diagnostics back into structured UI components.
- **Severity rating:** Minor
- **Workaround used:** Built a deterministic JSON schema validator in `src/agent/knowledge_retriever.py` that normalizes recommendations before passing them to the agent context.
- **Actionable suggestion:** Offer an official Ring Diagnostic MCP Server template in the Amazon developer docs.