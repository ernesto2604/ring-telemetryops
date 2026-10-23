"""T44: RTSPS live stream stats (best effort). cv2.VideoCapture is always a test double: no RTSPS connection
is ever opened. The token and the stream URL must never reach a response, stdout or the logs."""
import logging
import os

import cv2
import numpy as np
import pytest

import src.api.server as server
from src.utils import ring_partner_client as rpc
from src.utils.ring_partner_client import RingPartnerClient
from tests.fake_ring_server import DEVICE_ID
from tests.test_partner_snapshots_events import TOKEN, api, partner, ring, sleeps  # noqa: F401  (fixtures)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeCapture:
    """cv2.VideoCapture double: `frames` frames, one every `interval` seconds of the fake clock."""
    instances = []

    def __init__(self, clock, frames=1000, interval=1 / 15, opened=True, open_seconds=0.0, read_error=False,
                 size=(1280, 720)):
        self.clock, self.frames, self.interval = clock, frames, interval
        self.opened, self.open_seconds, self.read_error, self.size = opened, open_seconds, read_error, size
        self.released = False
        self.url = None
        self.ffmpeg_options = None

    def __call__(self, url, api_pref, params):   # used as the factory
        self.url, self.api_pref, self.params = url, api_pref, params
        self.ffmpeg_options = os.environ.get(rpc.FFMPEG_OPTIONS_ENV_VAR)
        self.clock.now += self.open_seconds
        FakeCapture.instances.append(self)
        return self

    def isOpened(self):
        return self.opened

    def read(self):
        if self.read_error:
            raise cv2.error("decoder failed")
        if self.frames <= 0:
            self.clock.now += rpc.LIVE_READ_TIMEOUT_MS / 1000
            return False, None
        self.frames -= 1
        self.clock.now += self.interval
        return True, np.zeros((self.size[1], self.size[0], 3), np.uint8)

    def release(self):
        self.released = True


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_ACCESS_TOKEN", TOKEN)
    monkeypatch.delenv(rpc.FFMPEG_OPTIONS_ENV_VAR, raising=False)


def stats(capture, **kwargs):
    return RingPartnerClient().live_stream_stats(DEVICE_ID, capture_factory=capture, clock=capture.clock, **kwargs)


def test_known_cadence_gives_that_fps_and_releases_the_capture(token):
    capture = FakeCapture(Clock(), interval=1 / 15)
    result = stats(capture)
    assert result["status"] == "ok" and result["reason"] is None
    assert result["fps"] == pytest.approx(15, abs=0.05) and result["resolution"] == "1280x720"
    assert 119 <= result["frame_count"] <= 121 and result["seconds"] == pytest.approx(8, abs=0.1)
    assert "bitrate" not in str(result)
    assert capture.released


def test_url_carries_the_token_in_the_query_over_tcp_and_env_is_restored(token, monkeypatch):
    monkeypatch.setenv(rpc.FFMPEG_OPTIONS_ENV_VAR, "previous;value")
    capture = FakeCapture(Clock())
    stats(capture, component_id="cam-left")
    assert capture.url.startswith(f"rtsps://video.rtsp.amazonvision.com:322/v1/devices/{DEVICE_ID}")
    assert f"?token={TOKEN}" in capture.url and "&component_id=cam-left" in capture.url
    assert "@" not in capture.url.split("?")[0]   # never in the URL user part
    assert capture.api_pref == cv2.CAP_FFMPEG and cv2.CAP_PROP_OPEN_TIMEOUT_MSEC in capture.params
    assert capture.ffmpeg_options == "rtsp_transport;tcp"
    assert os.environ[rpc.FFMPEG_OPTIONS_ENV_VAR] == "previous;value"


def test_reading_never_exceeds_ten_seconds(token):
    capture = FakeCapture(Clock(), interval=0.5)
    assert stats(capture, read_seconds=60)["seconds"] <= rpc.LIVE_MAX_READ_SECONDS + 0.5


@pytest.mark.parametrize("capture_args, reason", [
    ({"opened": False}, "rtsps_unavailable"),
    ({"opened": False, "open_seconds": 9}, "rtsps_timeout"),
    ({"frames": 0}, "rtsps_timeout"),
    ({"read_error": True}, "rtsps_unavailable"),
], ids=["cannot-open", "open-timeout", "no-frames", "cv2-error"])
def test_failures_have_a_clear_reason_and_always_release(token, capture_args, reason):
    capture = FakeCapture(Clock(), **capture_args)
    result = stats(capture)
    assert result == {"status": "not_available", "reason": reason}
    assert capture.released


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture
def live(partner, monkeypatch):
    clock = Clock()
    capture = FakeCapture(clock, interval=1 / 12, size=(640, 360))
    real = RingPartnerClient.live_stream_stats

    def with_double(self, device_id, component_id=None):
        return real(self, device_id, component_id, capture_factory=capture, clock=clock)

    monkeypatch.setattr(RingPartnerClient, "live_stream_stats", with_double)
    return partner[0], capture


def test_live_stats_endpoint_labels_the_result_and_hides_the_url(live, capsys, caplog):
    ring, capture = live
    caplog.set_level(logging.DEBUG)

    response = api.post("/api/ring/live-stats")

    assert response.status_code == 200
    body = response.json()["live"]
    assert body["status"] == "ok" and body["label"] == "Live stream as received by this server"
    assert body["fps"] == pytest.approx(12, abs=0.05) and body["resolution"] == "640x360"
    assert capture.released
    out = capsys.readouterr()
    logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("aiohttp"))
    for secret in (TOKEN, "rtsps://", "amazonvision"):
        assert secret not in response.text and secret not in out.out + out.err and secret not in logs


@pytest.mark.parametrize("change, reason", [
    ({"status": {"online": False}}, "device_offline"),
    ({"configurations": {"e2e_encryption": {"enabled": True}}}, "media_encrypted"),
])
def test_not_started_for_offline_or_encrypted_devices(live, change, reason):
    ring, capture = live
    for name, value in change.items():
        setattr(ring, name, value)
    body = api.post("/api/ring/live-stats").json()["live"]
    assert body == {"status": "not_available", "reason": reason}
    assert capture.url is None   # never opened


def test_unavailable_stream_is_not_an_app_error(live):
    _, capture = live
    capture.opened = False
    response = api.post("/api/ring/live-stats")
    assert response.status_code == 200
    assert response.json()["live"]["reason"] == "rtsps_unavailable"


def test_live_stats_require_partner_mode():
    assert api.post("/api/ring/live-stats").status_code == 409
