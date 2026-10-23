"""
Partner telemetry adapter — Ring TelemetryOps Concierge Agent
==============================================================
Maps one device normalized by RingPartnerClient.list_devices() onto the
telemetry schema produced by TelemetrySimulator / RingClient, so the agent
pipeline can consume it later.

DATA INTEGRITY:
  - Only values the Ring Partner API reported are filled in; everything else
    is None and listed in `measurement_status` with the reason.
  - `online` is passed through as-is (True, False or None).
  - No battery, power mode or wiring is declared from missing data.
"""

from typing import Any, Dict

from src.utils.ring_partner_client import NOT_PROVIDED_BY_API
from src.utils.telemetry_simulator import _utcnow

PARTNER_SOURCE = "ring_partner_api"

# device_health fields of the existing schema that the Partner API does not expose.
_UNAVAILABLE_HEALTH_FIELDS = (
    "rssi",
    "latest_signal_strength",
    "wifi_name",
    "wifi_is_connected",
    "network_connection_type",
    "battery_saver_low_voltage",
    "led_status",
    "firmware",
    "transformer_voltage_vac",
)


def partner_device_to_telemetry(device: Dict[str, Any]) -> Dict[str, Any]:
    """Builds a telemetry payload from a normalized Partner API device without inventing metrics."""
    health = device.get("device_health") or {}
    states = health.get("measurement_status") or {}

    device_health: Dict[str, Any] = {field: None for field in _UNAVAILABLE_HEALTH_FIELDS}
    device_health.update({
        "online": health.get("online"),
        "battery_percentage": health.get("battery_percentage"),
        "updated_at": health.get("reported_at"),
    })

    health_status = {field: NOT_PROVIDED_BY_API for field in _UNAVAILABLE_HEALTH_FIELDS}
    health_status.update({
        "online": states.get("online", "not_reported"),
        "battery_percentage": states.get("battery_percentage", "not_reported"),
        # Ring `reported_at`: last device check-in (uplink), not the current time.
        "updated_at": states.get(
            "reported_at", "reported_by_api" if health.get("reported_at") is not None else "not_reported"
        ),
    })

    return {
        # Status read, not a live stream: Ring returns the last reported reading.
        "scenario": "partner_status",
        "_source": PARTNER_SOURCE,
        "captured_at": _utcnow(),
        "device_id": device.get("device_id"),
        "device_name": device.get("name"),
        "device_model": None,
        "device_health": device_health,
        "capabilities": device.get("capabilities"),
        # Settings Ring reported (configurations, location, snooze); None when not fetched.
        "device_context": device.get("device_context"),
        "webrtc_stats": {
            "inbound_rtp": {
                "kind": "video",
                "jitter": None,
                "packets_lost": None,
                "packet_loss_fraction": None,
                "frames_dropped": None,
                "frames_received": None,
                "frames_per_second": None,
                "bytes_received": None,
                "bitrate_kbps": None,
            },
            "candidate_pair": {
                "current_round_trip_time": None,
                "available_outgoing_bitrate": None,
            },
        },
        "measurement_status": {
            "device_health": health_status,
            "webrtc_stats": "not_collected",
        },
        "fetch_errors": device.get("fetch_errors") or {},
    }
