"""
RingClient — Ring TelemetryOps Concierge Agent
================================================
Digital Twin adapter: serves simulated telemetry (TelemetrySimulator) to the API, the web UI
and the CLI. It never contacts Ring and never reads Ring credentials.

Official Ring data comes only from RingPartnerClient (RING_DATA_SOURCE=partner), which uses
the Ring Partner API endpoints listed in src/utils/ring_partner_client.py.

The former live adapter (account e-mail/password login, an OAuth token cache in
`.ring_token.cache`, snapshots and WHEP through the unofficial `ring_doorbell` library) was
removed: the project uses only the official Ring Partner API.
"""

import logging
from typing import Any, Dict, List, Optional

from src.utils.telemetry_simulator import TelemetrySimulator

logger = logging.getLogger(__name__)

SIMULATED_SOURCE = "mock"
SIMULATED_DEVICE_ID = "a1b2c3d4e5f6"


class RingClient:
    """
    Simulator-only client with the interface the Digital Twin routes use.

    Usage:
        client = RingClient(mock=True)
        telemetry = client.get_telemetry(scenario="wifi_degradation")
    """

    def __init__(self, mock: bool = True):
        if not mock:
            raise ValueError(
                "Live Ring access through this adapter was removed; "
                "use RING_DATA_SOURCE=partner (official Ring Partner API)."
            )
        self._simulator = TelemetrySimulator()

    @property
    def is_live(self) -> bool:
        """Always False: this adapter only simulates."""
        return False

    @property
    def mode(self) -> str:
        return "MOCK"

    @property
    def live_error(self) -> Optional[str]:
        return None

    def list_devices(self) -> List[Dict[str, Any]]:
        """The single simulated Digital Twin unit."""
        return [{
            "index": 0,
            "name": "Ring Video Doorbell 4 (Simulated)",
            "kind": "doorbell_v4",
            "id": SIMULATED_DEVICE_ID,
            "address": "123 Demo Street",
            "simulated": True,
        }]

    def get_telemetry(self, scenario: str = "nominal") -> Dict[str, Any]:
        """Simulated telemetry for a Digital Twin scenario, labelled `_source="mock"`."""
        logger.info("[RingClient] Digital Twin telemetry — scenario: %s", scenario)
        payload = self._simulator.get_telemetry(scenario)
        payload["_source"] = SIMULATED_SOURCE
        return payload

    async def create_whep_session(self, device_id: str, sdp_offer: str) -> Dict[str, Any]:
        """
        Simulated WHEP negotiation for the Digital Twin. No Ring endpoint is called and no
        stream exists; the response says so explicitly.
        """
        logger.info("[RingClient] Simulated WHEP session for %s (no Ring stream)", device_id)
        return {
            "status": "simulated",
            "simulated": True,
            "source": "digital_twin",
            "notice": "Simulated SDP answer for the Digital Twin; no Ring live stream exists.",
            "device_id": device_id,
            "sdp_answer": (
                "v=0\r\no=- 461173200 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\na=sendonly\r\n"
                "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 H264/90000\r\n"
            ),
            "e2ee_enabled": None,
        }


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    scenario = sys.argv[1] if len(sys.argv) > 1 else "wifi_degradation"
    print(json.dumps(RingClient().get_telemetry(scenario=scenario), indent=2, ensure_ascii=False))
