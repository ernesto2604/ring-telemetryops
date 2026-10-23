"""
funnel.py — in-memory analysis funnel counters (since server start; never persisted).

Ring's vision guidance recommends instrumenting the funnel: events in, frames kept on the server,
which engine answered, fallback rate. Only counts and latencies are kept: no device IDs, images or text.
"""

import statistics
from collections import deque
from typing import Any, Dict, Optional

SNAPSHOT_FAILURE_CAUSES = ("no_recent_media", "media_undecryptable", "corrupt_recording", "unreadable_image",
                           "time_range_not_authorized")
CLIP_FAILURE_CAUSES = (*SNAPSHOT_FAILURE_CAUSES, "not_ready", "clip_too_large", "unreadable_clip", "no_event")
LATENCY_ENGINES = ("bedrock", "local_fallback", "precheck")
LATENCY_SAMPLES = 100
COUNTERS = ("analyses_total", "precheck_nominal", "precheck_anomaly", "bedrock_calls", "bedrock_retries",
            "local_fallbacks", "insufficient_data", "snapshots_ok", "clips_requested", "clips_ok", "events_seen", "feedback_up", "feedback_down")


class Funnel:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counters = {name: 0 for name in COUNTERS}
        self.snapshots_failed = {cause: 0 for cause in (*SNAPSHOT_FAILURE_CAUSES, "other")}
        self.clips_failed = {cause: 0 for cause in (*CLIP_FAILURE_CAUSES, "other")}
        self.latency = {engine: deque(maxlen=LATENCY_SAMPLES) for engine in LATENCY_ENGINES}

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] += amount

    def record_analysis(self, result: Dict[str, Any], seconds: float) -> None:
        """One finished concierge result: which pre-check branch and engine produced it, and how long it took."""
        provider = result.get("_provider")
        status = (result.get("diagnostic_result") or {}).get("diagnostic_status")
        self.count("analyses_total")
        if status == "INSUFFICIENT_DATA" and provider == "precheck":
            self.count("insufficient_data")
        elif result.get("analysis_skipped"):
            self.count("precheck_nominal")   # healthy frame or no anomaly: nothing left the server
        else:
            self.count("precheck_anomaly")
        attempts = result.get("bedrock_attempts") or 0
        self.count("bedrock_calls", attempts)
        self.count("bedrock_retries", max(0, attempts - 1))
        if provider == "local_fallback":
            self.count("local_fallbacks")
        if provider in self.latency:
            self.latency[provider].append(seconds)

    def record_snapshot(self, status: str) -> None:
        if status == "ok":
            self.count("snapshots_ok")
        else:
            cause = status if status in SNAPSHOT_FAILURE_CAUSES else "other"
            self.snapshots_failed[cause] += 1

    def record_clip(self, status: str) -> None:
        """One clip request ("Analyze last recording") and how it ended."""
        self.count("clips_requested")
        if status == "ok":
            self.count("clips_ok")
        else:
            self.clips_failed[status if status in CLIP_FAILURE_CAUSES else "other"] += 1

    def snapshot(self) -> Dict[str, Any]:
        def stats(samples: deque) -> Dict[str, Optional[float]]:
            if not samples:
                return {"samples": 0, "median_ms": None, "p95_ms": None}
            ordered = sorted(samples)
            p95 = ordered[min(len(ordered) - 1, max(0, round(0.95 * len(ordered)) - 1))]   # nearest rank
            return {"samples": len(ordered), "median_ms": round(statistics.median(ordered) * 1000, 1),
                    "p95_ms": round(p95 * 1000, 1)}

        return {**self.counters, "snapshots_failed": dict(self.snapshots_failed),
                "clips_failed": dict(self.clips_failed),
                "latency": {engine: stats(samples) for engine, samples in self.latency.items()},
                "since": "server_start", "persisted": False}


funnel = Funnel()
