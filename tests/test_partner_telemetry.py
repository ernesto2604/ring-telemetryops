import json

import pytest

from src.utils.partner_telemetry import partner_device_to_telemetry
from src.utils.ring_partner_client import NOT_PROVIDED_BY_API, build_device_health, normalize_device

DEVICE_ID = "ava1.ring.device.ONLINE1"


def partner_device(status_attrs, capability_attrs, fetch_errors=None):
    resource = {"type": "devices", "id": DEVICE_ID, "attributes": {"name": "Front Door"}}
    status = {"attributes": status_attrs} if status_attrs is not None else None
    capabilities = {"attributes": capability_attrs} if capability_attrs is not None else None
    device = normalize_device(resource, status, capabilities)
    device["fetch_errors"] = fetch_errors or {}
    return device


@pytest.mark.parametrize("online", [True, False, None])
def test_online_state_is_passed_through_exactly(online):
    status = {"online": online} if online is not None else {}
    telemetry = partner_device_to_telemetry(partner_device(status, {}))

    assert telemetry["device_health"]["online"] is online
    expected = "reported_by_api" if online is not None else "not_reported"
    assert telemetry["measurement_status"]["device_health"]["online"] == expected


def test_source_and_identity():
    telemetry = partner_device_to_telemetry(partner_device({"online": True}, {"video": {"codecs": ["AVC"]}}))

    assert telemetry["_source"] == "ring_partner_api"
    assert telemetry["device_id"] == DEVICE_ID
    assert telemetry["device_name"] == "Front Door"
    assert telemetry["device_model"] is None
    assert telemetry["capabilities"] == {"video": {"codecs": ["AVC"]}}


def test_unavailable_metrics_are_none_and_marked():
    telemetry = partner_device_to_telemetry(partner_device({"online": True}, {}))
    dh = telemetry["device_health"]
    states = telemetry["measurement_status"]["device_health"]

    for field in ("rssi", "latest_signal_strength", "transformer_voltage_vac",
                  "battery_saver_low_voltage", "wifi_is_connected", "firmware"):
        assert dh[field] is None
        assert states[field] == NOT_PROVIDED_BY_API

    rtp = telemetry["webrtc_stats"]["inbound_rtp"]
    cp = telemetry["webrtc_stats"]["candidate_pair"]
    assert rtp["jitter"] is None
    assert rtp["packet_loss_fraction"] is None
    assert cp["current_round_trip_time"] is None
    assert telemetry["measurement_status"]["webrtc_stats"] == "not_collected"


def test_no_battery_or_wiring_is_declared_from_missing_data():
    telemetry = partner_device_to_telemetry(partner_device({"online": True}, None))
    dh = telemetry["device_health"]

    assert dh["battery_percentage"] is None
    assert telemetry["measurement_status"]["device_health"]["battery_percentage"] == "capabilities_unknown"
    assert "power_mode" not in dh
    assert "power_source" not in dh


def test_reported_battery_is_kept_with_its_provenance():
    telemetry = partner_device_to_telemetry(
        partner_device({"online": True, "battery_status": {"percentage": 42}}, {"battery_status": {"supported": True}})
    )

    assert telemetry["device_health"]["battery_percentage"] == 42
    assert telemetry["measurement_status"]["device_health"]["battery_percentage"] == "reported_by_api"


def test_fetch_errors_are_propagated_and_health_stays_unknown():
    errors = {"status": {"error": "RingServiceUnavailableError", "status": 503, "code": None, "message": "x"}}
    device = partner_device(None, {"battery_status": {"supported": True}}, fetch_errors=errors)
    device["device_health"]["measurement_status"]["online"] = "fetch_failed"

    telemetry = partner_device_to_telemetry(device)

    assert telemetry["device_health"]["online"] is None
    assert telemetry["measurement_status"]["device_health"]["online"] == "fetch_failed"
    assert telemetry["fetch_errors"] == errors


def test_malformed_device_does_not_crash_or_invent_values():
    telemetry = partner_device_to_telemetry({})

    assert telemetry["device_id"] is None
    assert telemetry["device_health"]["online"] is None
    assert telemetry["device_health"]["battery_percentage"] is None
    assert telemetry["measurement_status"]["device_health"]["online"] == "not_reported"
    assert telemetry["fetch_errors"] == {}
    json.dumps(telemetry)  # serializable


def test_reported_at_maps_to_updated_at_only_when_online():
    health = build_device_health({"online": True, "reported_at": "2026-10-19T10:00:00Z"}, {})
    telemetry = partner_device_to_telemetry({"device_id": DEVICE_ID, "device_health": health})

    assert telemetry["device_health"]["updated_at"] == "2026-10-19T10:00:00Z"
    assert telemetry["measurement_status"]["device_health"]["updated_at"] == "reported_by_api"

    offline = build_device_health({"online": False, "reported_at": "1970-01-01T00:00:00Z"}, {})
    telemetry = partner_device_to_telemetry({"device_id": DEVICE_ID, "device_health": offline})
    assert telemetry["device_health"]["updated_at"] is None
