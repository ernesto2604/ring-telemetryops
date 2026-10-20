"""
Compatibility of the B4/B5 additions with existing consumers:
- `requires` on a KB signal is optional; signals without it behave as before.
- New KB keys (`source_verification`, per-entry `verification`) are additive; /api/kb keeps the
  legacy keys (`official_signals`, `official_api_mapping`, `official_power_requirements`).
- The pre-check still returns {"domain", "reason"}; the new chime reason is an extra value and the
  pre-existing reasons are unchanged. The reason is only used internally (log / fallback message).
"""
from fastapi.testclient import TestClient

import pytest

from src.agent.concierge import SupportConcierge
from src.agent.knowledge_retriever import KnowledgeRetriever
from src.api.server import app

retriever = KnowledgeRetriever()

LEGACY = {"metric": "transformer_voltage_vac", "operator": "lt", "value": 16.0, "diagnostic_weight": 0.85}
SCOPED = {**LEGACY, "requires": {"chime_type_setting": "mechanical"}}


@pytest.mark.parametrize("flat, expected", [
    ({"transformer_voltage_vac": 13.0}, 0.85),
    ({"transformer_voltage_vac": 13.0, "chime_type_setting": "digital"}, 0.85),
    ({"transformer_voltage_vac": 17.0}, None),
])
def test_legacy_signal_without_requires_is_unchanged(flat, expected):
    assert retriever._evaluate_signal(LEGACY, flat) == expected


@pytest.mark.parametrize("flat, expected", [
    ({"transformer_voltage_vac": 13.0, "chime_type_setting": "mechanical"}, 0.85),
    ({"transformer_voltage_vac": 13.0, "chime_type_setting": "digital"}, None),
    ({"transformer_voltage_vac": 13.0}, None),
    ({"transformer_voltage_vac": 17.0, "chime_type_setting": "mechanical"}, None),
])
def test_scoped_signal_applies_only_when_required_fields_match(flat, expected):
    assert retriever._evaluate_signal(SCOPED, flat) == expected


def test_kb_endpoint_keeps_legacy_keys():
    articles = {a["id"]: a for a in TestClient(app).get("/api/kb").json()["articles"]}
    assert articles["KB-RING-001"]["official_signals"]
    assert articles["KB-RING-004"]["official_api_mapping"][0]["endpoint"] == "GET /v1/devices/{device_id}/status"
    assert articles["KB-RING-009"]["official_power_requirements"]
    assert articles["KB-RING-013"]["official_api_mapping"]


VIDEO = {"status": "OPTIMAL", "issues": [], "fps": None, "bitrate_kbps": None,
         "sharpness_score": 200.0, "optical_banding_detected": False}


def tel(**device_health):
    base = {"rssi": -50, "battery_percentage": 100, "transformer_voltage_vac": None}
    return {"device_health": {**base, **device_health},
            "webrtc_stats": {"inbound_rtp": {"jitter": 0.002, "packet_loss_fraction": 0.0,
                                             "frames_per_second": 30, "bitrate_kbps": 1200}}}


@pytest.mark.parametrize("telemetry, reason", [
    (tel(battery_percentage=5), "Critical battery level"),
    (tel(transformer_voltage_vac=10.0), "Transformer undervoltage"),
    (tel(rssi=-90), "Degraded network metrics"),
    (tel(online=False), "Device reported offline"),
    (tel(transformer_voltage_vac=13.5, chime_type_setting="mechanical"),
     "Transformer voltage too low for a mechanical chime"),
])
def test_precheck_result_shape_and_reasons(telemetry, reason):
    anomaly = SupportConcierge()._precheck_anomaly(VIDEO, telemetry, True)
    assert set(anomaly) == {"domain", "reason"}
    assert anomaly["reason"] == reason
