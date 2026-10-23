"""T43: Ring recorded clips (M2). Every Ring call goes to the local fake server (tests/fake_ring_server.py);
test MP4s are written with cv2.VideoWriter in pytest's tmp_path. Bedrock is a double."""
import asyncio
import json
import logging
import os
import tempfile

import cv2
import pytest

import src.api.server as server
from src.api.funnel import funnel
from src.utils import ring_partner_client as rpc
from src.utils.ring_partner_client import (
    RingBlockedError, RingForbiddenError, RingPartnerClient, RingUnauthorizedError,
)
from src.video_pipeline.analyzer import analyze_clip
from tests.fake_ring_server import (
    CLIP_PRESIGNED_PATH, DEVICE_ID, blurry_frame, greco_no_valid_key, jpeg, sharp_frame, synthetic_mp4,
)
from tests.test_partner_snapshots_events import (  # noqa: F401  (fixtures)
    EVENT_STARTS, TOKEN, api, partner, ring, sleeps, with_events,
)

CLIP_POST = f"/v1/devices/{DEVICE_ID}/media/video/download"


@pytest.fixture
def clip(ring, tmp_path):
    ring.clip = synthetic_mp4(tmp_path, fps=15, frames=75, size=(640, 360))
    return ring


def download(**kwargs):
    async def no_sleep(_seconds):
        pass
    return asyncio.run(RingPartnerClient(sleep=no_sleep).download_clip(DEVICE_ID, EVENT_STARTS[0], **kwargs))


def clip_posts(ring):
    return [r for r in ring.requests if r["method"] == "POST" and r["path"] == CLIP_POST]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def test_post_body_asks_for_five_seconds_without_audio_for_one_component(clip):
    download(component_id="cam-left")
    body = clip_posts(clip)[0]["json"]
    assert body == {"timestamp": EVENT_STARTS[0], "duration": 5000, "audio_options": {"audio_enabled": False},
                    "components": [{"component_id": "cam-left"}]}
    assert clip_posts(clip)[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_200_returns_the_mp4_directly(clip):
    clip.clip_replies = [("body", 200, None)]
    result = download()
    assert result["status"] == "ok" and result["video"] == clip.clip and result["partial"] is False
    assert len(clip.requests) == 1


def test_206_is_a_partial_clip_with_its_media_length(clip):
    clip.clip_replies = [("body", 206, {"X-Media-Length": "3200"})]
    result = download()
    assert result["status"] == "ok" and result["partial"] is True and result["media_length"] == 3200


@pytest.mark.parametrize("reply", [None, ("redirect", 301), ("redirect", 302)], ids=["303", "301", "302"])
def test_redirect_is_followed_without_the_token(clip, reply):
    clip.clip_replies = [reply]
    result = download()
    assert result["status"] == "ok" and result["video"] == clip.clip
    post, presigned = clip.requests
    assert presigned["path"] == CLIP_PRESIGNED_PATH and presigned["method"] == "GET"
    assert "Authorization" not in presigned["headers"] and TOKEN not in json.dumps(presigned["headers"])


def test_425_then_200_waits_and_succeeds(clip):
    clip.clip_replies = [(425, {"Retry-After": "2"}, "RECORDING_NOT_READY"), ("body", 200, None)]
    assert download()["status"] == "ok"
    assert len(clip_posts(clip)) == 2


@pytest.mark.parametrize("reply, status", [
    ((416, None, "MEDIA_NOT_FOUND"), "no_recent_media"),
    ((425, None, "RECORDING_NOT_READY"), "not_ready"),
])
def test_416_and_425_stop_within_the_wait_budget(clip, reply, status):
    clip.clip_replies = [reply]
    result = download(wait_budget_seconds=20)
    assert result["status"] == status and result["video"] is None
    assert 2 <= len(clip_posts(clip)) <= 6   # 1 + 2 + 4 + 8 s fits in 20 s; 16 more does not


def test_422_greco_no_valid_key_is_undecryptable(clip):
    clip.clip_replies = [(422, None, "GRECO_NO_VALID_KEY")]
    assert download()["status"] == "media_undecryptable"
    clip.clip_replies, clip.presigned_replies = [None], [greco_no_valid_key()]
    assert download()["status"] == "media_undecryptable"


def test_403_time_range_is_a_result(clip):
    clip.clip_replies = [(403, None, "TIME_RANGE_NOT_AUTHORIZED")]
    assert download()["status"] == "time_range_not_authorized"


def test_bytes_that_are_not_mp4_are_unreadable(clip):
    clip.clip = jpeg(sharp_frame())
    clip.clip_replies = [("body", 200, None)]
    assert download()["status"] == "unreadable_clip"


@pytest.mark.parametrize("reply", [("body", 200, None), None], ids=["direct", "presigned"])
def test_too_large_clip_is_refused(clip, monkeypatch, reply):
    monkeypatch.setattr(rpc, "CLIP_MAX_BYTES", 1000)
    clip.clip_replies = [reply]
    assert download()["status"] == "clip_too_large"


@pytest.mark.parametrize("status, error", [(401, RingUnauthorizedError), (403, RingForbiddenError),
                                           (406, RingBlockedError)])
def test_credential_errors_raise(clip, status, error):
    clip.clip_replies = [(status, None, None)]
    with pytest.raises(error) as info:
        download()
    assert TOKEN not in str(info.value) and "http://" not in str(info.value)


# ---------------------------------------------------------------------------
# Analysis (temporary file)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_paths(monkeypatch):
    created = []
    real = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real(*args, **kwargs)
        created.append(path)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    return created


def test_stats_match_the_synthetic_mp4_and_the_temp_file_is_deleted(tmp_path, temp_paths):
    stats = analyze_clip(synthetic_mp4(tmp_path, fps=15, frames=75, size=(640, 360)))
    assert (stats["width"], stats["height"], stats["resolution"]) == (640, 360, "640x360")
    assert stats["fps"] == pytest.approx(15, abs=0.1) and stats["frame_count"] == 75
    assert stats["duration_seconds"] == pytest.approx(5.0, abs=0.1) and stats["bitrate_mbps"] > 0
    assert len(stats["frames"]) == 3 and all(f[:2] == b"\xff\xd8" for f in stats["frames"])
    assert len(temp_paths) == 1 and not os.path.exists(temp_paths[0])
    assert temp_paths[0].startswith(tempfile.gettempdir())


def test_temp_file_is_deleted_when_the_analysis_raises(tmp_path, temp_paths, monkeypatch):
    data = synthetic_mp4(tmp_path)

    def broken(*_args, **_kwargs):
        raise RuntimeError("decoder crashed")

    monkeypatch.setattr(cv2, "VideoCapture", broken)
    with pytest.raises(RuntimeError):
        analyze_clip(data)
    assert len(temp_paths) == 1 and not os.path.exists(temp_paths[0])


def test_undecodable_clip_raises_value_error_and_is_deleted(temp_paths):
    with pytest.raises(ValueError):
        analyze_clip(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
    assert not os.path.exists(temp_paths[0])


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture
def clip_partner(with_events, tmp_path):
    funnel.reset()
    with_events.clip = synthetic_mp4(tmp_path, fps=15, frames=75, size=(640, 360))
    return with_events


def clip_analysis():
    return api.post("/api/ring/clip-analysis")


def app_log(caplog):
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("aiohttp"))


def test_healthy_clip_reports_recording_stats_and_keeps_frames_on_server(clip_partner, partner, temp_paths,
                                                                         capsys, caplog):
    _, llm_calls = partner
    caplog.set_level(logging.DEBUG)

    response = clip_analysis()

    assert response.status_code == 200
    body = response.json()
    stats = body["clip"]
    assert stats["status"] == "ok" and stats["label"] == "Recording stats (from Ring recording, not live network)"
    assert stats["resolution"] == "640x360" and stats["fps"] == pytest.approx(15, abs=0.1)
    assert stats["duration_seconds"] == pytest.approx(5.0, abs=0.1) and stats["frames_checked"] == 3
    assert "frames" not in stats
    assert clip_posts(clip_partner)[0]["json"]["timestamp"] == EVENT_STARTS[0]
    image = next(d for d in body["checked_domains"] if d["domain"] == "image")
    assert image["status"] == "ok"
    network = next(d for d in body["checked_domains"] if d["domain"] == "network")
    assert network["status"] == "not_available"   # recording stats never fill network values
    assert llm_calls == []   # healthy frames never leave the server
    metrics = api.get("/api/metrics").json()
    # Partner never shares Wi-Fi or power: a healthy image ends as INSUFFICIENT_DATA from the pre-check.
    assert metrics["clips_requested"] == 1 and metrics["clips_ok"] == 1 and metrics["insufficient_data"] == 1
    assert metrics["bedrock_calls"] == 0
    assert not os.path.exists(temp_paths[0])
    out = capsys.readouterr()
    for secret in (TOKEN, CLIP_PRESIGNED_PATH, temp_paths[0]):
        assert secret not in response.text and secret not in out.out + out.err and secret not in app_log(caplog)


def test_blurry_clip_gets_the_image_report(clip_partner, partner, tmp_path):
    _, llm_calls = partner
    clip_partner.clip = synthetic_mp4(tmp_path, frame=blurry_frame(), fps=10, frames=30)

    body = clip_analysis().json()

    image = next(d for d in body["checked_domains"] if d["domain"] == "image")
    assert image["status"] == "issue" and "sharpness" in image["detail"].lower()
    assert llm_calls and all(call["image"] for call in llm_calls)   # the same report path as a snapshot
    assert body["clip"]["fps"] == pytest.approx(10, abs=0.1)


@pytest.mark.parametrize("reply, status", [
    ((416, None, "MEDIA_NOT_FOUND"), "no_recent_media"),
    ((422, None, "GRECO_NO_VALID_KEY"), "media_undecryptable"),
    ((403, None, "TIME_RANGE_NOT_AUTHORIZED"), "time_range_not_authorized"),
])
def test_ring_failures_are_explained_and_counted(clip_partner, reply, status):
    clip_partner.clip_replies = [reply]

    body = clip_analysis().json()

    assert body["clip"]["status"] == status and body["clip"]["reason"] == server.CLIP_REASONS[status]
    assert "diagnostic_result" not in body
    metrics = api.get("/api/metrics").json()
    assert metrics["clips_requested"] == 1 and metrics["clips_failed"][status] == 1


def test_analysis_crash_is_a_500_and_deletes_the_temp_file(clip_partner, temp_paths, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("decoder crashed")

    monkeypatch.setattr(cv2, "VideoCapture", broken)
    response = clip_analysis()
    assert response.status_code == 500 and "decoder" not in response.text
    assert not os.path.exists(temp_paths[0])


def test_no_event_means_no_clip_request(partner):
    ring, _ = partner
    funnel.reset()
    body = clip_analysis().json()
    assert body["clip"]["status"] == "no_event" and clip_posts(ring) == []


def test_credential_error_keeps_its_reason(clip_partner):
    clip_partner.clip_replies = [(401, None, None)]
    response = clip_analysis()
    assert response.status_code == 502 and response.json()["detail"]["reason"] == "token_rejected"
    assert TOKEN not in response.text


def test_encrypted_video_is_never_requested(clip_partner):
    clip_partner.configurations = {"e2e_encryption": {"enabled": True}}
    body = clip_analysis().json()
    assert body["clip"]["status"] == "media_encrypted" and clip_posts(clip_partner) == []


def test_clip_requires_partner_mode():
    assert api.post("/api/ring/clip-analysis").status_code == 409
