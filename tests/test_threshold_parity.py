"""
A1: one severity table (src/config/severity_thresholds.json) for the backend pre-check, the local
fallback, the KB retriever, the video analyzer and the web UI (served at GET /api/thresholds).
The page keeps no copy of the numbers, so parity is checked by forbidding threshold literals in
the classification code and by comparing every level and boundary the API serves with the table.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.agent import concierge
from src.agent.concierge import SupportConcierge
from src.agent.knowledge_retriever import KnowledgeRetriever
from src.api.server import app
from src.config import severity
from src.video_pipeline import analyzer

ROOT = Path(__file__).resolve().parents[1]
TABLE = json.loads((ROOT / "src" / "config" / "severity_thresholds.json").read_text(encoding="utf-8"))

# Every threshold number of the table (and the KB-001 bands that the retriever ranks with).
THRESHOLD_NUMBERS = r"-88|-75|-60|0\.5|12|16|10|30|40|8|150|20"
# A comparison against one of them, e.g. "rssi < -75" or "battery >= 30".
COMPARISON = re.compile(rf"(<=|>=|<|>|===|==)\s*\(?\s*({THRESHOLD_NUMBERS})(\.0)?\b")


def code_lines(path: Path):
    """Source lines without comments and docstrings (the README-style text may quote numbers)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        text = re.sub(r'"""[\s\S]*?"""', "", text)
        text = re.sub(r"(?m)#.*$", "", text)
        # The Bedrock prompt is rendered from the table, so no literal survives there either.
    else:
        text = re.sub(r"/\*[\s\S]*?\*/", "", text)
        text = re.sub(r"(?m)//.*$", "", text)
    return text.splitlines()


CLASSIFICATION_CODE = [
    ROOT / "src" / "agent" / "concierge.py",
    ROOT / "src" / "agent" / "knowledge_retriever.py",
    ROOT / "src" / "video_pipeline" / "analyzer.py",
    ROOT / "src" / "api" / "server.py",
    ROOT / "web" / "js" / "app.js",
    ROOT / "web" / "js" / "telemetry.js",
    ROOT / "web" / "js" / "canvas-engine.js",
    ROOT / "web" / "js" / "chime-audio.js",
]


@pytest.mark.parametrize("path", CLASSIFICATION_CODE, ids=lambda p: p.name)
def test_no_threshold_literal_outside_the_table(path):
    hits = [line.strip() for line in code_lines(path) if COMPARISON.search(line)]
    assert hits == [], f"{path.name} compares against threshold literals: {hits}"


def test_frontend_reads_the_table_from_the_api():
    telemetry_js = (ROOT / "web" / "js" / "telemetry.js").read_text(encoding="utf-8")
    assert "fetch('/api/thresholds')" in telemetry_js
    assert not re.search(r"^const \w+_(PCT|VAC|DBM|MS)\s*=", telemetry_js, re.M)


def test_api_serves_every_level_and_boundary_of_the_table():
    served = TestClient(app).get("/api/thresholds").json()
    assert served == TABLE
    for name, rule in TABLE["metrics"].items():
        assert [(b["level"], b["op"], b["value"]) for b in served["metrics"][name]["bands"]] == \
               [(b["level"], b["op"], b["value"]) for b in rule["bands"]]


def test_reference_values():
    """The defaults of decisions D3 / O1 / O2 / O3 (change them only together with the README)."""
    lim = severity.limit
    assert (lim("rssi", "Weak"), lim("rssi", "Poor"), lim("rssi", "Critical")) == (-60, -75, -88)
    assert (lim("battery", "Low"), lim("battery", "Critical")) == (30, 10)
    assert (lim("transformer", "Undervoltage"), lim("transformer", "No power")) == (12.0, 0.5)
    assert lim("mechanical_chime", "Too low for the mechanical chime") == 16.0
    assert (lim("jitter_ms", "High"), lim("packet_loss_pct", "High")) == (40.0, 8.0)
    assert (lim("fps", "Low"), lim("bitrate", "Low")) == (20.0, 150.0)
    assert TABLE["heartbeat"] == {"period_seconds": 15, "degraded_pulses": 3}


def test_backend_modules_use_the_table_values():
    assert concierge.TRANSFORMER_MIN_VAC == KnowledgeRetriever.TRANSFORMER_MIN_VAC == severity.limit("transformer", "Undervoltage")
    assert concierge.MECHANICAL_CHIME_MIN_VAC == severity.limit("mechanical_chime", "Too low for the mechanical chime")
    assert KnowledgeRetriever.BATTERY_CRITICAL_PCT == severity.limit("battery", "Critical")
    assert KnowledgeRetriever.BATTERY_LOW_PCT == severity.limit("battery", "Low")
    assert analyzer.STREAM_MIN_FPS == severity.limit("fps", "Low")
    assert analyzer.STREAM_MIN_BITRATE_KBPS == severity.limit("bitrate", "Low")


# ---------------------------------------------------------------------------
# Boundaries (inclusive / exclusive) of every band, as the pre-check sees them
# ---------------------------------------------------------------------------

def level(metric, value, **context):
    item = severity.classify_metric(metric, value, context)
    return item and item["level"]


@pytest.mark.parametrize("rssi, expected", [
    (-60, "GREEN"), (-61, "DEGRADED"), (-75, "DEGRADED"), (-76, "DEGRADED"), (-88, "DEGRADED"), (-89, "CRITICAL"),
])
def test_rssi_bands(rssi, expected):
    assert level("rssi", rssi) == expected


@pytest.mark.parametrize("rssi, label", [(-60, "Excellent"), (-61, "Weak"), (-75, "Weak"), (-76, "Poor"),
                                         (-88, "Poor"), (-89, "Critical")])
def test_rssi_band_names(rssi, label):
    assert severity.classify_metric("rssi", rssi)["label"] == label


@pytest.mark.parametrize("battery, battery_mode, wired", [
    (9, "CRITICAL", "CRITICAL"), (10, "DEGRADED", "GREEN"), (29, "DEGRADED", "GREEN"), (30, "GREEN", "GREEN"),
])
def test_battery_bands_keep_the_power_mode_semantics(battery, battery_mode, wired):
    """O1: the low band applies to battery-only installs; the critical band to any measured battery."""
    assert level("battery", battery, power_mode="battery") == battery_mode
    assert level("battery", battery, power_mode="wired") == wired
    assert level("battery", battery) == wired   # power setup not reported (e.g. Ring Partner API)


@pytest.mark.parametrize("volts, expected", [
    (0, "CRITICAL"), (0.5, "CRITICAL"), (0.6, "DEGRADED"), (11.9, "DEGRADED"), (12.0, "GREEN"), (16.0, "GREEN"),
])
def test_transformer_bands(volts, expected):
    assert level("transformer", volts) == expected
    assert level("transformer", volts, power_mode="battery") is None   # no transformer on battery-only


@pytest.mark.parametrize("metric, green, degraded", [
    ("packet_loss_pct", 8.0, 8.1), ("jitter_ms", 40.0, 40.1), ("fps", 20.0, 19.9), ("bitrate", 150.0, 149.0),
])
def test_transport_bands_have_no_critical_level(metric, green, degraded):
    assert level(metric, green) == "GREEN"
    assert level(metric, degraded) == "DEGRADED"
    assert level(metric, -1e9 if metric in ("fps", "bitrate") else 1e9) == "DEGRADED"


def test_unmeasured_values_are_never_green():
    assert severity.classify_metric("rssi", None) is None
    assert severity.classify_telemetry({"rssi": None, "battery": None}) == []


# ---------------------------------------------------------------------------
# A3: the pre-check level is the worst metric level
# ---------------------------------------------------------------------------

VIDEO = {"status": "OPTIMAL", "issues": [], "fps": None, "bitrate_kbps": None,
         "sharpness_score": 200.0, "optical_banding_detected": False}


def tel(power_mode=None, battery=100, transformer=None, rssi=-50, jitter=0.002, loss=0.0):
    return {"device_health": {"power_mode": power_mode, "rssi": rssi, "battery_percentage": battery,
                              "transformer_voltage_vac": transformer},
            "webrtc_stats": {"inbound_rtp": {"jitter": jitter, "packet_loss_fraction": loss,
                                             "frames_per_second": 30, "bitrate_kbps": 1200}}}


@pytest.mark.parametrize("telemetry, expected", [
    (tel(transformer=11.0), "DEGRADED"),
    (tel(transformer=0.0), "CRITICAL"),
    (tel(power_mode="battery", battery=20), "DEGRADED"),
    (tel(power_mode="battery", battery=8), "CRITICAL"),
    (tel(rssi=-70), "DEGRADED"),
    (tel(rssi=-90), "CRITICAL"),
    (tel(rssi=-70, transformer=0.0), "CRITICAL"),   # worst metric wins
])
def test_precheck_level_is_the_worst_metric_level(telemetry, expected):
    assert SupportConcierge()._precheck_anomaly(VIDEO, telemetry, True)["level"] == expected


@pytest.mark.parametrize("battery, anomalous", [(9, True), (10, True), (29, True), (30, False)])
def test_battery_only_alert_boundary(battery, anomalous):
    agent = SupportConcierge()
    telemetry = tel(power_mode="battery", battery=battery)
    precheck = agent._precheck_anomaly(VIDEO, telemetry, True)
    report = agent._build_local_diagnostic(VIDEO, telemetry, [], True)
    if anomalous:
        assert precheck["domain"] == "HARDWARE_POWER"
        expected = "CRITICAL" if battery < severity.limit("battery", "Critical") else "DEGRADED"
        assert (report["diagnostic_status"], report["root_cause_domain"]) == (expected, "HARDWARE_POWER")
    else:
        assert precheck is None
        assert report["diagnostic_status"] == "HEALTHY"
