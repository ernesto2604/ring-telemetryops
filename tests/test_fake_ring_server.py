"""Self-check of tests/fake_ring_server.py, the fake Ring reused by later rounds (live video next)."""
import base64
import urllib.request

from src.video_pipeline.analyzer import MIN_SHARPNESS_SCORE, VideoAnalyzer
from tests.fake_ring_server import FakeRing, blurry_frame, jpeg, pink_frame, sharp_frame


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
