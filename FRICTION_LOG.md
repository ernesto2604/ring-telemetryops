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
- **Specific task attempted:** Send 1080p doorbell keyframes alongside JSON telemetry to Amazon Bedrock (Anthropic `claude-haiku-4-5`, model ID `us.anthropic.claude-haiku-4-5-20251001-v1:0`) for visual failure analysis.
- **Steps taken:** Encoded full-resolution video frames directly to Base64 and sent them via `boto3` `invoke_model`.
- **Expected vs. actual result:** Expected interactive response times. The full-resolution image payload made each round trip too slow for interactive triage.
- **Severity rating:** Major
- **Workaround used:** `VideoAnalyzer.get_frame_base64()` in `src/video_pipeline/analyzer.py` now downscales each keyframe in memory with OpenCV to 720 px wide and encodes it as JPEG at quality 75 before `SupportConcierge` sends it to Bedrock. Bedrock is also called only when the local pre-check finds an anomaly.
- **Actionable suggestion:** Provide native AWS SDK frame-buffer helpers or edge-optimized image preprocessors tailored for smart camera event triage.