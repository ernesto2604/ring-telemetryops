"""T24: Ring watermark band is excluded from the image metrics only when asked."""
import base64
import inspect

import cv2
import numpy as np

from src.video_pipeline.analyzer import (
    RING_WATERMARK_BAND_PX,
    RING_WATERMARK_MAX_FRACTION,
    VideoAnalyzer,
    crop_ring_watermark,
    sharpness_score,
)

H, W = 720, 1280   # 16:9; measured after downscaling to SHARPNESS_ANALYSIS_WIDTH


def clean_frame():
    """Stationary mild texture, so any band of it has about the same Laplacian variance."""
    noise = np.random.default_rng(7).integers(0, 256, (H, W), dtype=np.uint8)
    gray = cv2.GaussianBlur(noise, (0, 0), 2.0)
    gray = cv2.normalize(gray, None, 60, 180, cv2.NORM_MINMAX)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def watermarked(frame):
    """White text in both top corners, inside the top band, like Ring's logo and ID/app/time text."""
    marked = frame.copy()
    cv2.putText(marked, "RING", (16, 48), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 3)
    cv2.putText(marked, "ava1.ring.device.X  App  2026-10-23 10:00:00", (560, 44),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    return marked


def as_png_base64(frame):
    return base64.b64encode(cv2.imencode(".png", frame)[1]).decode()


def sharpness(frame, crop):
    return VideoAnalyzer.analyze_frame_base64(as_png_base64(frame), crop_watermark=crop)[0]["sharpness_score"]


def test_fixed_pixel_band_and_crop():
    assert (RING_WATERMARK_BAND_PX, RING_WATERMARK_MAX_FRACTION) == (100, 0.35)
    assert crop_ring_watermark(np.zeros((720, 10))).shape == (620, 10)
    assert crop_ring_watermark(np.zeros((360, 10))).shape == (260, 10)
    assert crop_ring_watermark(np.zeros((200, 10))).shape == (130, 10)   # capped at 35 % of the height


def test_watermark_inflates_sharpness_without_crop():
    clean = sharpness(clean_frame(), crop=False)
    marked = sharpness(watermarked(clean_frame()), crop=False)
    assert marked > clean * 1.25, (clean, marked)


def test_crop_brings_sharpness_back_to_the_clean_frame():
    clean = sharpness(clean_frame(), crop=False)
    marked_cropped = sharpness(watermarked(clean_frame()), crop=True)
    assert abs(marked_cropped - clean) <= 0.05 * clean, (clean, marked_cropped)


def test_crop_is_off_by_default_so_the_simulator_is_unaffected():
    assert inspect.signature(VideoAnalyzer.analyze_frame_base64).parameters["crop_watermark"].default is False
    assert inspect.signature(VideoAnalyzer.__init__).parameters["crop_watermark"].default is False
    frame = as_png_base64(watermarked(clean_frame()))
    default, _ = VideoAnalyzer.analyze_frame_base64(frame)
    explicit_off, _ = VideoAnalyzer.analyze_frame_base64(frame, crop_watermark=False)
    assert default == explicit_off
    assert default["resolution"] == "720x405"              # full frame (not cropped), resized to 720 px
    assert sharpness_score(cv2.cvtColor(watermarked(clean_frame()), cv2.COLOR_BGR2GRAY)) == default["sharpness_score"]


def test_sample_video_metrics_unchanged_by_default():
    analyzer = VideoAnalyzer("data/samples/good_internet.mp4")
    assert analyzer.crop_watermark is False
