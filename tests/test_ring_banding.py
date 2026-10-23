"""T32: Ring frames need periodic row brightness to be flagged as banding; the watermark crop is in pixels."""
import base64

import cv2
import numpy as np

from src.video_pipeline.analyzer import (
    BANDING_ROW_DIFF_STD_LIMIT,
    RING_BANDING_PERIODICITY_LIMIT,
    VideoAnalyzer,
    banding_periodicity,
)


def jpeg_base64(frame):
    return base64.b64encode(cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])[1]).decode()


def ring_metrics(frame):
    return VideoAnalyzer.analyze_frame_base64(jpeg_base64(frame), crop_watermark=True)[0]


def natural_scene(h=720, w=1280, seed=3):
    """Smooth noise plus hard-edged blocks: slow trends and edges, no periodic rows."""
    rng = np.random.default_rng(seed)
    noise = cv2.GaussianBlur(rng.normal(0, 1, (h, w)).astype(np.float32), (0, 0), 6)
    scene = cv2.normalize(noise, None, 60, 190, cv2.NORM_MINMAX)
    for _ in range(25):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        cv2.rectangle(scene, (x, y), (x + int(rng.integers(20, 200)), y + int(rng.integers(20, 200))),
                      float(rng.integers(30, 230)), -1)
    return cv2.cvtColor(scene.astype(np.uint8), cv2.COLOR_GRAY2BGR)


def with_bands(frame, amplitude=0.15, period=24):
    rows = 1 + amplitude * np.sin(2 * np.pi * np.arange(frame.shape[0]) / period)
    return (frame.astype(np.float32) * rows[:, None, None]).clip(0, 255).astype(np.uint8)


def detailed_noisy_scene(h=720, w=1280):
    """Busy scene: strong pixel noise and random (aperiodic) row-to-row brightness jumps."""
    rng = np.random.default_rng(11)
    scene = natural_scene(h, w).astype(np.float32)
    scene += rng.normal(0, 25, (h, w, 1))
    scene += np.repeat(rng.normal(0, 14, h // 4), 4)[:, None, None]
    return scene.clip(0, 255).astype(np.uint8)


def ring_watermark(frame):
    """White text with a dark outline in rows ~15-85, fixed in pixels like Ring's watermark."""
    marked = frame.copy()
    w = frame.shape[1]
    for text, org, scale in [("ring", (24, 52), 1.3),
                             ("Device-ID: ...ABC123", (w - 400, 33), 0.7),
                             ("Partner: RingDeveloperPortal", (w - 400, 58), 0.7),
                             ("Generated-At: 2026-10-23T10:00:00Z", (w - 400, 83), 0.7)]:
        cv2.putText(marked, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 5)
        cv2.putText(marked, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2)
    return marked


def test_natural_scene_without_bands_is_not_flagged():
    metrics = ring_metrics(natural_scene())
    assert metrics["optical_banding_detected"] is False


def test_natural_scene_with_periodic_bands_is_flagged():
    frame = with_bands(natural_scene())
    metrics = ring_metrics(frame)
    assert metrics["optical_banding_score"] > BANDING_ROW_DIFF_STD_LIMIT
    assert metrics["optical_banding_detected"] is True
    assert any("banding" in issue for issue in metrics["issues"])


def test_busy_noisy_scene_without_periodicity_is_not_flagged():
    frame = detailed_noisy_scene()
    metrics = ring_metrics(frame)
    assert metrics["optical_banding_score"] > BANDING_ROW_DIFF_STD_LIMIT   # the old test alone would flag it
    gray = cv2.cvtColor(cv2.resize(frame, (720, 405)), cv2.COLOR_BGR2GRAY)
    assert banding_periodicity(gray) < RING_BANDING_PERIODICITY_LIMIT
    assert metrics["optical_banding_detected"] is False


def test_simulator_path_does_not_need_periodicity():
    """Without crop_watermark the old variance-only rule is unchanged."""
    frame = detailed_noisy_scene()
    plain, _ = VideoAnalyzer.analyze_frame_base64(jpeg_base64(frame))
    assert plain["optical_banding_detected"] is True


def check_watermark_is_cropped(h, w):
    clean, marked = natural_scene(h, w), ring_watermark(natural_scene(h, w))
    clean_m, marked_m = ring_metrics(clean), ring_metrics(marked)
    uncropped = VideoAnalyzer.analyze_frame_base64(jpeg_base64(marked))[0]
    assert uncropped["sharpness_score"] > ring_metrics(clean)["sharpness_score"] * 1.1   # the text does matter
    assert abs(marked_m["sharpness_score"] - clean_m["sharpness_score"]) <= 0.02 * clean_m["sharpness_score"]
    assert abs(marked_m["optical_banding_score"] - clean_m["optical_banding_score"]) <= 0.1
    assert marked_m["optical_banding_detected"] is False


def test_watermark_is_cropped_on_a_small_frame():
    check_watermark_is_cropped(360, 640)   # watermark ~24 % of the height; a 10 % crop missed most of it


def test_watermark_is_cropped_on_a_large_frame():
    check_watermark_is_cropped(1080, 1920)
