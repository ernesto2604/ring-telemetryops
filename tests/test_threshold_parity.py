"""
B3: diagnostic alert thresholds have the same values in the backend (src/agent/concierge.py)
and in the Digital Twin heartbeat (web/js/telemetry.js, used by web/js/app.js).
Badge and metric colour bands (e.g. battery 30/10 %, RSSI -60/-75 dBm, jitter 15/45 ms) are
display bands, not diagnostic rules, and are not part of this parity.
FPS (20) and bitrate (150 kbps) thresholds exist only in the backend: the heartbeat does not
evaluate them, so there is no JavaScript counterpart to compare.
"""
import re
from pathlib import Path

import pytest

from src.agent import concierge
from src.agent.concierge import SupportConcierge

ROOT = Path(__file__).resolve().parents[1]
TELEMETRY_JS = (ROOT / "web" / "js" / "telemetry.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")

SHARED = [
    "BATTERY_CRITICAL_PCT",
    "TRANSFORMER_MIN_VAC",
    "RSSI_DEGRADED_DBM",
    "PACKET_LOSS_DEGRADED_PCT",
    "JITTER_DEGRADED_MS",
]


def js_constant(name):
    match = re.search(rf"^const {name} = (-?\d+(?:\.\d+)?);", TELEMETRY_JS, re.M)
    assert match, f"{name} is not defined in web/js/telemetry.js"
    return float(match.group(1))


@pytest.mark.parametrize("name", SHARED)
def test_frontend_and_backend_thresholds_match(name):
    assert js_constant(name) == float(getattr(concierge, name))


def test_reference_values():
    assert concierge.BATTERY_CRITICAL_PCT == 10
    assert concierge.TRANSFORMER_MIN_VAC == 12.0
    assert concierge.RSSI_DEGRADED_DBM == -75
    assert concierge.PACKET_LOSS_DEGRADED_PCT == 8.0
    assert concierge.JITTER_DEGRADED_MS == 40.0
    assert concierge.STREAM_MIN_FPS == 20.0
    assert concierge.STREAM_MIN_BITRATE_KBPS == 150.0


def test_heartbeat_uses_the_shared_constants_instead_of_literals():
    body = re.search(r"function getHeartbeatAnomaly\(\) \{(.*?)\n\}", APP_JS, re.S).group(1)
    for name in SHARED:
        assert name in body, name
    for literal in ("< 12", "-75", "> 8", "> 40", "<= 5"):
        assert literal not in body, literal


# Battery-only installs: the alert fires below 10 %, never at exactly 10 %.

def battery_only(battery):
    return {"device_health": {"power_mode": "battery", "rssi": -50, "battery_percentage": battery,
                              "transformer_voltage_vac": None},
            "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0.0,
                                             "frames_per_second": 30, "bitrate_kbps": 1200}}}


VIDEO = {"status": "OPTIMAL", "issues": [], "fps": None, "bitrate_kbps": None,
         "sharpness_score": 200.0, "optical_banding_detected": False}


@pytest.mark.parametrize("battery, anomalous", [(9, True), (10, False), (11, False)])
def test_battery_only_alert_boundary(battery, anomalous):
    agent = SupportConcierge()
    precheck = agent._precheck_anomaly(VIDEO, battery_only(battery), True)
    report = agent._build_local_diagnostic(VIDEO, battery_only(battery), [], True)
    if anomalous:
        assert precheck["domain"] == "HARDWARE_POWER"
        assert (report["diagnostic_status"], report["root_cause_domain"]) == ("CRITICAL", "HARDWARE_POWER")
    else:
        assert precheck is None
        assert report["diagnostic_status"] == "HEALTHY"
