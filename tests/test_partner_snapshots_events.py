"""Tanda 8b: device settings gated by capabilities, Ring snapshots, Event History, per-domain verdict.
Every Ring call goes to the local fake server in tests/fake_ring_server.py (127.0.0.1) or a test double."""
import pytest

from src.utils.ring_partner_client import build_device_context

# ---------------------------------------------------------------------------
# T26: only the settings the device supports
# ---------------------------------------------------------------------------

LIVE_CONFIG = {"e2e_encryption": {"enabled": False},
               "image_enhancements": {"color_night_vision": "on", "hdr": "off", "ir_led_night_vision": "off",
                                      "auto_zoom_track": "off", "privacy_zones": []}}


def caps(*names):
    return {"image_enhancements": {"configurations": list(names)}}


def test_settings_missing_from_capabilities_are_null_not_false():
    context = build_device_context(None, caps("color_night_vision", "privacy_zones", "snapshot"), LIVE_CONFIG, None)
    enh = context["image_enhancements"]
    assert enh == {"color_night_vision": True, "hdr": None, "ir_led_night_vision": None,
                   "auto_zoom_track": None, "privacy_zones": 0}
    assert context["unsupported_settings"] == ["hdr", "ir_led_night_vision", "auto_zoom_track"]
    assert context["snapshot_supported"] is True


@pytest.mark.parametrize("setting", ["color_night_vision", "hdr", "ir_led_night_vision", "auto_zoom_track",
                                     "privacy_zones"])
def test_each_setting_follows_its_capability(setting):
    supported = build_device_context(None, caps(setting), LIVE_CONFIG, None)
    unsupported = build_device_context(None, caps("snapshot"), LIVE_CONFIG, None)
    assert supported["image_enhancements"][setting] is not None and setting not in supported["unsupported_settings"]
    assert unsupported["image_enhancements"][setting] is None and setting in unsupported["unsupported_settings"]


def test_without_capability_list_values_pass_through_and_no_snapshot():
    context = build_device_context(None, None, LIVE_CONFIG, None)
    assert context["image_enhancements"]["hdr"] is False
    assert context["unsupported_settings"] == [] and context["snapshot_supported"] is False


@pytest.mark.parametrize("snapshot, e2e, available", [
    (True, False, True), (True, True, False), (False, False, False),
])
def test_status_feature_snapshot_reflects_support_and_readability(monkeypatch, snapshot, e2e, available):
    from fastapi.testclient import TestClient

    import src.api.server as server
    from tests.test_partner_api_server import FakePartnerClient

    fake = FakePartnerClient()
    config = {"e2e_encryption": {"enabled": e2e}}
    fake.devices[0]["device_context"] = build_device_context(
        None, caps("snapshot") if snapshot else caps(), config, None)
    monkeypatch.setattr(server, "ring_source", "partner")
    monkeypatch.setattr(server, "ring_source_error", None)
    monkeypatch.setattr(server, "partner_client", fake)
    monkeypatch.setattr(server, "_partner_known_devices", {})

    assert TestClient(server.app).get("/api/ring/status").json()["features"]["snapshot"] is available
