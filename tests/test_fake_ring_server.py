"""Self-check of tests/fake_ring_server.py, the fake Ring reused by later rounds (live video next)."""
import base64
import urllib.error
import urllib.request

from src.video_pipeline.analyzer import MIN_SHARPNESS_SCORE, VideoAnalyzer
from tests.fake_ring_server import (
    PRESIGNED_PATH, FakeRing, blurry_frame, greco_no_valid_key, jpeg, pink_frame, sharp_frame,
)


def metrics(frame):
    return VideoAnalyzer.analyze_frame_base64(base64.b64encode(jpeg(frame)).decode(), crop_watermark=True)[0]


def test_synthetic_frames_have_the_intended_image_verdicts():
    sharp, blurry, pink = metrics(sharp_frame()), metrics(blurry_frame()), metrics(pink_frame())
    assert sharp["status"] == "OPTIMAL" and sharp["sharpness_score"] > MIN_SHARPNESS_SCORE
    assert blurry["sharpness_score"] < MIN_SHARPNESS_SCORE
    assert any("chromatic" in issue.lower() for issue in pink["issues"])


def test_server_listens_on_loopback_only_and_records_requests():
    ring = FakeRing().start()
    try:
        assert ring.url.startswith("http://127.0.0.1:")
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # loopback, never a proxy
        with no_proxy.open(f"{ring.url}/v1/devices", timeout=5) as response:
            assert response.status == 200
        assert ring.paths() == ["/v1/devices"]
    finally:
        ring.stop()


def test_presigned_get_can_reply_with_the_live_422_json_error():
    ring = FakeRing().start()
    ring.presigned_replies = [greco_no_valid_key(), None]
    try:
        no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            no_proxy.open(f"{ring.url}{PRESIGNED_PATH}", timeout=5)
            raise AssertionError("expected HTTP 422")
        except urllib.error.HTTPError as exc:
            assert exc.code == 422 and b"GRECO_NO_VALID_KEY" in exc.read()
        with no_proxy.open(f"{ring.url}{PRESIGNED_PATH}", timeout=5) as response:
            assert response.status == 200 and response.read() == ring.image
    finally:
        ring.stop()


def test_clip_route_serves_scripted_replies_and_the_presigned_clip(tmp_path):
    from tests.fake_ring_server import CLIP_PRESIGNED_PATH, DEVICE_ID, synthetic_mp4

    ring = FakeRing().start()
    ring.clip = synthetic_mp4(tmp_path, fps=10, frames=20)
    ring.clip_replies = [(425, None, "RECORDING_NOT_READY"), ("body", 206, {"X-Media-Length": "9"}), None]
    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"{ring.url}/v1/devices/{DEVICE_ID}/media/video/download"
    try:
        try:
            no_proxy.open(urllib.request.Request(url, data=b"{}", method="POST"), timeout=5)
            raise AssertionError("expected HTTP 425")
        except urllib.error.HTTPError as exc:
            assert exc.code == 425 and b"RECORDING_NOT_READY" in exc.read()
        with no_proxy.open(urllib.request.Request(url, data=b"{}", method="POST"), timeout=5) as response:
            assert response.status == 206 and response.headers["X-Media-Length"] == "9"
            assert response.read()[4:8] == b"ftyp"
        with no_proxy.open(f"{ring.url}{CLIP_PRESIGNED_PATH}", timeout=5) as response:
            assert response.headers["Content-Type"] == "video/mp4" and response.read() == ring.clip
    finally:
        ring.stop()
