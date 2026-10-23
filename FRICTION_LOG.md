# Friction Log: Ring Partner API, Ring Developer Playground and Ring MCP

Everything below was observed while building Ring TelemetryOps.


### 1. Camera status has no health data

- **Task:** diagnose Wi-Fi and power problems of a Ring camera/doorbell through the Partner API.
- **Steps:** GET /v1/devices/{id}/status with ?include=status,capabilities,configurations,location on the Playground device.
- **Expected:** signal strength and battery. Actual: status returns only online, reported_at, audio.snooze and state. The sensor documentation shows signal_strength and battery_status in status, so the platform supports them for sensors but not for cameras.
- **Severity:** High for health-monitoring apps.
- **Workaround:** a Digital Twin simulator for the full diagnosis, and an honest "Partial check" with real data.
- **Suggestion:** expose signal strength, battery and the power source in camera status, and health events such as low battery or weak signal.

### 2. latest_in_range snapshot fails with an undocumented error

- **Task:** download "the most recent image".
- **Steps:** POST .../media/image/download with latest_in_range, follow the 303 Location, GET the pre-signed URL without the token.
- **Expected:** a JPEG. Actual: 422 with code GRECO_NO_VALID_KEY ("No valid Greco key available for decryption") on the pre-signed GET, while at_timestamp with an event's start time returns 200 image/jpeg. The documentation lists only 422 CORRUPT_RECORDING.
- **Severity:** High for "latest image" use cases.
- **Workaround:** start from the newest event's timestamp and try up to three events. We searched Ring's documentation through the Ring MCP: the image-download error table lists only CORRUPT_RECORDING for 422. Ring's support article on TAKE (Throw Away the Key Encryption) says third-party integrations require pausing TAKE and that such videos are viewable only from enrolled devices, which is consistent with this error, but we could not confirm the cause.
- **Suggestion:** document the code and its meaning, and let latest_in_range skip recordings that cannot be decrypted.

### 3. The Playground token lifetime

- **Task:** develop and demo against the Playground.
- **Steps:** generate a temporary token, put it in the server configuration, restart.
- **Expected:** a token that lasts through a development session. Actual: the Playground states a 30-minute lifetime (the real linking flow gives about 4 hours), so we generated a new token for each test session and restarted the server; in one case a token still worked well after 30 minutes.
- **Severity:** Medium.
- **Workaround:** generate the token right before each test.
- **Suggestion:** a longer-lived development token or a refresh option in the Playground.

### 4. The watermark has a fixed pixel size, so its relative size varies a lot

- **Task:** analyze image quality (sharpness, banding) on snapshots.
- **Steps:** measure Laplacian sharpness and row banding on downloaded snapshots at 1280x720 and 640x360.
- **Expected:** metrics that reflect the scene. Actual: the documentation says the watermark has a fixed pixel size; in our samples it covered the top 15 to 85 pixel rows in both sizes, which is under 12% of the height in one case and about 24% in the other. The high-contrast text inflated our sharpness and banding metrics until we cropped it.
- **Severity:** Medium.
- **Workaround:** crop a fixed pixel band before measuring.
- **Suggestion:** return the watermark's bounding box (or a header) with the image, or offer a watermark-free mode for the Playground.

### 5. Playground events are all recorded as live-view sessions

- **Task:** test event-driven logic.
- **Steps:** press Vehicle, Motion or Package in "Simulate live view event", then poll Event History.
- **Expected:** typed events. Actual: all new entries show as live view (on_demand). Our server analyzed a new event roughly 45 to 48 seconds after the click (polling every 15 s plus the time Ring takes to list the event; we could not separate the two).
- **Severity:** Low.
- **Workaround:** treat any new event as a trigger.
- **Suggestion:** simulate typed events (for example motion.vehicle, ding) so partners can test sub-type filtering.

### 6. A capability value that is not in the documentation

- **Task:** read device capabilities.
- **Steps:** GET the device with ?include=capabilities.
- **Expected:** only documented values. Actual: image_enhancements.configurations lists "snapshot", which the documentation does not list. It was useful as an indication that snapshots are supported.
- **Severity:** Low.
- **Suggestion:** document it.

### 7. Required headers vs the Playground example

- **Task:** make the first successful call.
- **Steps:** copy the curl snippet shown in the Playground.
- **Expected:** it works. Actual: the documentation requires User-Agent and Hardware_Id on every request (otherwise a 406 can occur), but the snippet includes only Authorization.
- **Severity:** Low.
- **Suggestion:** add both headers to the Playground examples.

### 8. No official SDK

- **Task:** build a client.
- **Expected:** a reference client. Actual: the documentation states there is no official Ring Partner SDK, so every project writes its own HTTP client (pre-signed redirects, retries, error mapping).
- **Severity:** Low.
- **Workaround:** a small read-only client with tests against a fake Ring server (included in our repository).
- **Suggestion:** a minimal reference client or a Bruno/Postman collection.

### 9. Two different explanations for a 406

- **Task:** handle a 406 response correctly.
- **Expected:** one explanation. Actual: the API reference says a 406 means Ring's web application firewall blocked the request, typically for missing User-Agent/Hardware_Id headers or a datacenter IP; the error-handling document found through the Ring MCP says a 406 means the IP address was blocked by Ring Security for irregular traffic and is not retryable.
- **Severity:** Low.
- **Workaround:** our error message names both causes.
- **Suggestion:** unify the explanation and say how to tell the two cases apart.

### 10. Webhook types described inconsistently

- **Task:** choose which webhooks to support.
- **Expected:** one list. Actual: the development guide says the API supports three webhook types (motion, device added, device removed), while the API reference lists ten (including button press, device online and offline, app integration and subscription events).
- **Severity:** Low.
- **Suggestion:** update the guide.

### 11. The Playground is hard to find from the testing documentation

- **Task:** find how to test without owning hardware.
- **Steps:** search Ring's documentation through the Ring MCP for testing guidance.
- **Expected:** a mention of the Playground. Actual: the testing guidance describes testing with a personal Ring account, staging users and a Ring Protect plan; it did not mention the Playground, which gave us a synthetic device, simulated events and a temporary token.
- **Severity:** Low.
- **Suggestion:** link the Playground from the Get Started and Test pages.

---

**Lesson:** thresholds calibrated on a simulator did not transfer to real Ring images (a banding detector gave false alarms on real frames until we cropped the watermark and required periodicity), so we calibrated on real Playground frames.
