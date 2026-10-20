import pytest

from src.utils.telemetry_simulator import TelemetrySimulator


def test_all_supported_scenarios_return_complete_telemetry():
    simulator = TelemetrySimulator()

    for scenario in simulator.SUPPORTED_SCENARIOS:
        telemetry = simulator.get_telemetry(scenario)

        assert telemetry["scenario"] == scenario
        assert telemetry["device_id"] == simulator.device_id
        assert "device_health" in telemetry
        assert "webrtc_stats" in telemetry


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError, match="not recognized"):
        TelemetrySimulator().get_telemetry("unknown_scenario")
