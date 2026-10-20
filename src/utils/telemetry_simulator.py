"""
TelemetrySimulator — Ring TelemetryOps Concierge Agent
=======================================================
Generates SIMULATED telemetry for the Digital Twin, in the project's telemetry schema,
combining two data tiers. None of these values come from a Ring device.

TIER 1 — Device health (project schema)
    Fields: rssi, battery_percentage, wifi_name, led_status, firmware,
            network_connection_type, wifi_is_connected, latest_signal_strength,
            transformer_voltage_vac, battery_saver_low_voltage
    Note: the official Ring Partner API status endpoint (GET /v1/devices/{id}/status) reports
    `online`, battery percentage and `reported_at`; it does not report RSSI, transformer voltage,
    LED, firmware or Wi-Fi fields (see src/utils/partner_telemetry.py).

TIER 2 — WebRTC W3C getStats() — RTCPeerConnection (WHEP live view)
    Type 'inbound-rtp' (video): jitter [s], packetsLost, framesDropped,
        framesReceived, bytesReceived, framesPerSecond, bitrate_kbps
    Type 'candidate-pair': currentRoundTripTime [s], availableOutgoingBitrate
    Reference: W3C WebRTC Statistics API — https://www.w3.org/TR/webrtc-stats/

Simulated fault scenarios (demo labels; the retriever never treats a label as evidence):
  - nominal                     -> All parameters optimal, healthy stream and hardware
  - wifi_degradation            -> Critical RSSI (< -80 dBm) + elevated packet loss + jitter
  - optical_physical            -> Optimal network transport, obstructed lens or inactive IR LEDs
  - power_issue                 -> Critical battery (<10%) or transformer subvoltage (<12V AC, project heuristic)
  - pink_purple_tint            -> Mechanical IR Cut filter stuck in daytime position
  - night_vision_glare          -> IR reflection off adjacent wall (white haze / fog halo)
  - cold_battery_drain          -> Freezing temperature (< 0°C) blocking charge with rapid drain
  - live_view_timeout           -> WebRTC connection timeout / ICE failure caused by firewall or VPN
  - audio_delay_jitter          -> Laggy, distorted two-way audio caused by high jitter
  - motion_delayed              -> Delayed PIR trigger or incorrect doorbell mounting height
  - solar_charging              -> Solar panel idle by design when battery is above 90%
  - poe_loss                    -> Ring Doorbell Elite: passive PoE mismatch or damaged data pair
  - in_home_chime               -> Mechanical chime failure due to transformer voltage sag (<16V AC)
  - whep_session_failure        -> WHEP Live View: SDP negotiation error or E2EE stream block
"""

import datetime
from typing import Dict, Any

# Diagnostic thresholds live in src/agent/concierge.py (project heuristics). The unused,
# conflicting copies that were here (transformer 14 V, labelled as Ring documentation) were removed.


def _utcnow() -> str:
    """Current UTC time as ISO 8601 (when this server built the payload)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TelemetrySimulator:
    """
    Generates Ring+WebRTC telemetry packages for all official diagnostic scenarios.
    """

    SUPPORTED_SCENARIOS = (
        "nominal",
        "wifi_degradation",
        "optical_physical",
        "power_issue",
        "pink_purple_tint",
        "night_vision_glare",
        "cold_battery_drain",
        "live_view_timeout",
        "audio_delay_jitter",
        "motion_delayed",
        "solar_charging",
        "poe_loss",
        "in_home_chime",
        "whep_session_failure",
    )

    def __init__(
        self,
        device_id: str = "a1b2c3d4e5f6",
        device_model: str = "Ring Video Doorbell 4",
        wifi_ssid: str = "HomeNetwork_2G",
        firmware_version: str = "2.4.18",
    ):
        self.device_id = device_id
        self.device_model = device_model
        self.wifi_ssid = wifi_ssid
        self.firmware_version = firmware_version

    def get_telemetry(self, scenario: str) -> Dict[str, Any]:
        """Returns a complete telemetry payload for the specified scenario."""
        if scenario not in self.SUPPORTED_SCENARIOS:
            raise ValueError(
                f"Scenario '{scenario}' not recognized. "
                f"Valid options: {self.SUPPORTED_SCENARIOS}"
            )

        builders = {
            "nominal":              self._nominal,
            "wifi_degradation":     self._wifi_degradation,
            "optical_physical":     self._optical_physical,
            "power_issue":          self._power_issue,
            "pink_purple_tint":     self._pink_purple_tint,
            "night_vision_glare":   self._night_vision_glare,
            "cold_battery_drain":   self._cold_battery_drain,
            "live_view_timeout":    self._live_view_timeout,
            "audio_delay_jitter":   self._audio_delay_jitter,
            "motion_delayed":       self._motion_delayed,
            "solar_charging":       self._solar_charging,
            "poe_loss":             self._poe_loss,
            "in_home_chime":         self._in_home_chime,
            "whep_session_failure": self._whep_session_failure,
        }
        return builders[scenario]()

    # ------------------------------------------------------------------
    # Scenario constructors
    # ------------------------------------------------------------------

    def _nominal(self) -> Dict[str, Any]:
        return {
            "scenario": "nominal",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -48,
                "latest_signal_strength": -48,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 100,
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.002,
                    "packets_lost": 0,
                    "packet_loss_fraction": 0.0,
                    "frames_dropped": 0,
                    "frames_received": 900,
                    "frames_per_second": 30.0,
                    "bytes_received": 4_500_000,
                    "bitrate_kbps": 1200,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.018,
                    "available_outgoing_bitrate": 1_500_000,
                },
            },
        }

    def _wifi_degradation(self) -> Dict[str, Any]:
        return {
            "scenario": "wifi_degradation",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -84,
                "latest_signal_strength": -84,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 78,
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.085,
                    "packets_lost": 540,
                    "packet_loss_fraction": 0.18,
                    "frames_dropped": 210,
                    "frames_received": 450,
                    "frames_per_second": 15.0,
                    "bytes_received": 675_000,
                    "bitrate_kbps": 180,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.320,
                    "available_outgoing_bitrate": 250_000,
                },
            },
        }

    def _optical_physical(self) -> Dict[str, Any]:
        return {
            "scenario": "optical_physical",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -46,
                "latest_signal_strength": -46,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 91,
                "battery_saver_low_voltage": False,
                "led_status": "off",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.001,
                    "packets_lost": 0,
                    "packet_loss_fraction": 0.0,
                    "frames_dropped": 0,
                    "frames_received": 900,
                    "frames_per_second": 30.0,
                    "bytes_received": 4_200_000,
                    "bitrate_kbps": 1100,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.016,
                    "available_outgoing_bitrate": 1_400_000,
                },
            },
        }

    def _power_issue(self) -> Dict[str, Any]:
        return {
            "scenario": "power_issue",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -55,
                "latest_signal_strength": -55,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 4,
                "battery_saver_low_voltage": True,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": 10.2,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.024,
                    "packets_lost": 15,
                    "packet_loss_fraction": 0.03,
                    "frames_dropped": 45,
                    "frames_received": 840,
                    "frames_per_second": 28.0,
                    "bytes_received": 3_800_000,
                    "bitrate_kbps": 980,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.042,
                    "available_outgoing_bitrate": 1_200_000,
                },
            },
        }

    def _pink_purple_tint(self) -> Dict[str, Any]:
        """IR-cut mechanical filter jammed in daytime position."""
        return {
            "scenario": "pink_purple_tint",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -52,
                "latest_signal_strength": -52,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 88,
                "battery_saver_low_voltage": False,
                "led_status": "off",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": 16.5,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.003,
                    "packets_lost": 0,
                    "packet_loss_fraction": 0.0,
                    "frames_dropped": 0,
                    "frames_received": 900,
                    "frames_per_second": 30.0,
                    "bytes_received": 4_600_000,
                    "bitrate_kbps": 1250,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.019,
                    "available_outgoing_bitrate": 1_600_000,
                },
            },
        }

    def _night_vision_glare(self) -> Dict[str, Any]:
        """Infrared LED reflection off adjacent wall or doorframe."""
        return {
            "scenario": "night_vision_glare",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -50,
                "latest_signal_strength": -50,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 82,
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.002,
                    "packets_lost": 0,
                    "packet_loss_fraction": 0.0,
                    "frames_dropped": 0,
                    "frames_received": 900,
                    "frames_per_second": 30.0,
                    "bytes_received": 4_100_000,
                    "bitrate_kbps": 1050,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.020,
                    "available_outgoing_bitrate": 1_300_000,
                },
            },
        }

    def _cold_battery_drain(self) -> Dict[str, Any]:
        """Freezing temperatures (< 0°C) causing rapid battery drain and charge prevention."""
        return {
            "scenario": "cold_battery_drain",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -58,
                "latest_signal_strength": -58,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 8,
                "battery_saver_low_voltage": True,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": 16.0,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.012,
                    "packets_lost": 5,
                    "packet_loss_fraction": 0.01,
                    "frames_dropped": 15,
                    "frames_received": 870,
                    "frames_per_second": 29.0,
                    "bytes_received": 3_900_000,
                    "bitrate_kbps": 1020,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.035,
                    "available_outgoing_bitrate": 1_100_000,
                },
            },
        }

    def _live_view_timeout(self) -> Dict[str, Any]:
        """WebRTC negotiation failure / timeout caused by firewall or VPN."""
        return {
            "scenario": "live_view_timeout",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -65,
                "latest_signal_strength": -65,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 75,
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.120,
                    "packets_lost": 800,
                    "packet_loss_fraction": 0.89,
                    "frames_dropped": 850,
                    "frames_received": 50,
                    "frames_per_second": 1.5,
                    "bytes_received": 50_000,
                    "bitrate_kbps": 20,
                },
                "candidate_pair": {
                    "state": "failed",
                    "current_round_trip_time": 0.950,
                    "available_outgoing_bitrate": 50_000,
                },
            },
        }

    def _audio_delay_jitter(self) -> Dict[str, Any]:
        """Bidirectional audio lag and robotic distortion caused by excessive jitter."""
        return {
            "scenario": "audio_delay_jitter",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -72,
                "latest_signal_strength": -72,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 68,
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.055,
                    "packets_lost": 120,
                    "packet_loss_fraction": 0.08,
                    "frames_dropped": 80,
                    "frames_received": 720,
                    "frames_per_second": 24.0,
                    "bytes_received": 2_800_000,
                    "bitrate_kbps": 650,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.220,
                    "available_outgoing_bitrate": 700_000,
                },
            },
        }

    def _motion_delayed(self) -> Dict[str, Any]:
        """Delayed motion detection or inappropriate mounting height/angle."""
        return {
            "scenario": "motion_delayed",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": self.device_model,
            "device_health": {
                "rssi": -55,
                "latest_signal_strength": -55,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 85,
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": 18.0,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.003,
                    "packets_lost": 0,
                    "packet_loss_fraction": 0.0,
                    "frames_dropped": 0,
                    "frames_received": 900,
                    "frames_per_second": 30.0,
                    "bytes_received": 4_500_000,
                    "bitrate_kbps": 1200,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.022,
                    "available_outgoing_bitrate": 1_400_000,
                },
            },
        }

    def _solar_charging(self) -> Dict[str, Any]:
        """Connected solar panel with battery at 94% (idle by design, Ring threshold >90%)."""
        return {
            "scenario": "solar_charging",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": "Ring Video Doorbell 4 (Solar)",
            "device_health": {
                "rssi": -58,
                "latest_signal_strength": -58,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 94,
                "power_source": "Solar",
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.003,
                    "packets_lost": 0,
                    "packet_loss_fraction": 0.0,
                    "frames_dropped": 0,
                    "frames_received": 900,
                    "frames_per_second": 30.0,
                    "bytes_received": 4_500_000,
                    "bitrate_kbps": 1200,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.024,
                    "available_outgoing_bitrate": 1_450_000,
                },
            },
        }

    def _poe_loss(self) -> Dict[str, Any]:
        """Ring Doorbell Elite with PoE data pair failure or incompatible passive injector."""
        return {
            "scenario": "poe_loss",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": "Ring Video Doorbell Elite",
            "device_health": {
                "rssi": 0,
                "latest_signal_strength": 0,
                "wifi_name": None,
                "wifi_is_connected": False,
                "network_connection_type": "ethernet_poe",
                "poe_standard": "passive_24v",
                "battery_percentage": 100,
                "battery_saver_low_voltage": True,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.095,
                    "packets_lost": 450,
                    "packet_loss_fraction": 0.22,
                    "frames_dropped": 320,
                    "frames_received": 180,
                    "frames_per_second": 6.0,
                    "bytes_received": 450_000,
                    "bitrate_kbps": 140,
                },
                "candidate_pair": {
                    "state": "failed",
                    "current_round_trip_time": 0.950,
                    "available_outgoing_bitrate": 150_000,
                },
            },
        }

    def _in_home_chime(self) -> Dict[str, Any]:
        """Transformer voltage sag (<16V AC) preventing in-home mechanical chime trigger."""
        return {
            "scenario": "in_home_chime",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": "Ring Video Doorbell Pro 2",
            "device_health": {
                "rssi": -52,
                "latest_signal_strength": -52,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 100,
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": 13.5,
                "chime_type_setting": "mechanical",
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.003,
                    "packets_lost": 0,
                    "packet_loss_fraction": 0.0,
                    "frames_dropped": 0,
                    "frames_received": 900,
                    "frames_per_second": 30.0,
                    "bytes_received": 4_500_000,
                    "bitrate_kbps": 1200,
                },
                "candidate_pair": {
                    "state": "succeeded",
                    "current_round_trip_time": 0.021,
                    "available_outgoing_bitrate": 1_500_000,
                },
            },
        }

    def _whep_session_failure(self) -> Dict[str, Any]:
        """WHEP negotiation failure or stream blocked by active End-to-End Encryption (E2EE)."""
        return {
            "scenario": "whep_session_failure",
            "captured_at": _utcnow(),
            "device_id": self.device_id,
            "device_model": "Ring Stick Up Cam Pro",
            "device_health": {
                "rssi": -55,
                "latest_signal_strength": -55,
                "wifi_name": self.wifi_ssid,
                "wifi_is_connected": True,
                "network_connection_type": "wifi",
                "battery_percentage": 88,
                "battery_saver_low_voltage": False,
                "led_status": "on",
                "firmware": self.firmware_version,
                "transformer_voltage_vac": None,
                "e2e_encryption_enabled": True,
                "updated_at": _utcnow(),
            },
            "webrtc_stats": {
                "inbound_rtp": {
                    "kind": "video",
                    "jitter": 0.080,
                    "packets_lost": 0,
                    "packet_loss_fraction": 0.0,
                    "frames_dropped": 0,
                    "frames_received": 0,
                    "frames_per_second": 0.0,
                    "bytes_received": 0,
                    "bitrate_kbps": 0,
                },
                "candidate_pair": {
                    "state": "checking",
                    "current_round_trip_time": 0.890,
                    "available_outgoing_bitrate": 0,
                },
            },
        }


if __name__ == "__main__":
    import json

    sim = TelemetrySimulator(
        device_id="fe80::1a2b:3c4d:5e6f",
        device_model="Ring Video Doorbell Pro 2",
        wifi_ssid="RingTestNet_2G",
    )

    for scenario in TelemetrySimulator.SUPPORTED_SCENARIOS:
        print(f"\n{'='*60}")
        print(f"  SCENARIO: {scenario.upper()}")
        print(f"{'='*60}")
        payload = sim.get_telemetry(scenario)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
