import base64
from pathlib import Path

import cv2
import numpy as np
import pytest

from src.video_pipeline.analyzer import MIN_SHARPNESS_SCORE, VideoAnalyzer


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"
SHARPNESS_ISSUE = "Deficient sharpness"


def sample_frame(second: float = 1.0) -> np.ndarray:
    """Full-resolution (1920x1080) frame of the Digital Twin base video."""
    cap = cv2.VideoCapture(str(SAMPLES / "good_internet.mp4"))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(second * cap.get(cv2.CAP_PROP_FPS)))
    ok, frame = cap.read()
    cap.release()
    assert ok
    return frame


def dirty_lens(frame: np.ndarray) -> np.ndarray:
    # Mirrors web/js/canvas-engine.js: Dirty Lens draws with ctx.filter = 'blur(3.8px)',
    # and a CSS blur radius is the Gaussian standard deviation.
    return cv2.GaussianBlur(frame, (0, 0), 3.8)


def as_canvas_jpeg(frame: np.ndarray) -> str:
    # The browser sends canvas.toDataURL('image/jpeg', 0.85).
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    assert ok
    return base64.b64encode(buf).decode()


def sharpness_issues(metrics: dict) -> list[str]:
    return [i for i in metrics["issues"] if SHARPNESS_ISSUE in i]


def test_sharp_frame_has_no_sharpness_issue():
    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(sample_frame()))

    assert metrics["sharpness_score"] > MIN_SHARPNESS_SCORE
    assert metrics["status"] == "OPTIMAL"
    assert metrics["issues"] == []


@pytest.mark.parametrize("second", [0.5, 1.0, 3.0, 6.0, 9.0])
def test_dirty_lens_frame_is_degraded(second):
    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(dirty_lens(sample_frame(second))))

    assert metrics["sharpness_score"] < MIN_SHARPNESS_SCORE
    assert metrics["status"] == "DEGRADED"
    assert len(sharpness_issues(metrics)) == 1


def test_strong_blur_is_degraded():
    blurred = cv2.GaussianBlur(sample_frame(), (0, 0), 8.0)

    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(blurred))

    assert metrics["status"] == "DEGRADED"
    assert sharpness_issues(metrics)


def test_frame_above_threshold_gets_no_sharpness_issue():
    # Mild blur lowers the score but keeps it above the threshold.
    mild = cv2.GaussianBlur(sample_frame(), (0, 0), 2.0)

    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(mild))

    assert MIN_SHARPNESS_SCORE < metrics["sharpness_score"] < 150
    assert sharpness_issues(metrics) == []


def test_sharpness_score_does_not_depend_on_input_resolution_or_max_width():
    frame = sample_frame()
    small = cv2.resize(frame, (720, 405), interpolation=cv2.INTER_AREA)

    full, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(frame))
    pre_scaled, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(small))
    narrow_output, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(frame), max_width=320)

    assert pre_scaled["sharpness_score"] == pytest.approx(full["sharpness_score"], rel=0.1)
    assert narrow_output["sharpness_score"] == full["sharpness_score"]


def test_black_frame_is_critical_not_a_blur_issue():
    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(np.zeros((1080, 1920, 3), np.uint8)))

    assert metrics["status"] == "CRITICAL"
    assert sharpness_issues(metrics) == []


def test_magenta_tint_is_reported_without_a_sharpness_issue():
    # Mirrors the pinkTint pixel shader in web/js/canvas-engine.js.
    f = sample_frame().astype(np.float32)
    f[:, :, 2] = np.minimum(255, f[:, :, 2] * 1.45 + 25)
    f[:, :, 1] = np.minimum(255, f[:, :, 1] * 0.55)
    f[:, :, 0] = np.minimum(255, f[:, :, 0] * 1.30 + 40)

    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(f.astype(np.uint8)))

    assert metrics["status"] == "DEGRADED"
    assert any("pink/purple tint" in i for i in metrics["issues"])
    assert sharpness_issues(metrics) == []


def test_obstructed_sample_video_is_degraded_by_sharpness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # inspect_stream writes its evidence frame under ./data/frames

    metrics = VideoAnalyzer(str(SAMPLES / "optical_obstruction.mp4")).inspect_stream()

    assert metrics["status"] == "DEGRADED"
    assert metrics["sharpness_score"] < MIN_SHARPNESS_SCORE
    assert sharpness_issues(metrics)


def test_file_and_frame_paths_score_the_same_video_on_one_scale():
    file_metrics = VideoAnalyzer(str(SAMPLES / "good_internet.mp4")).inspect_stream()
    frame_metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(sample_frame()))

    assert file_metrics["sharpness_score"] == pytest.approx(frame_metrics["sharpness_score"], rel=0.25)


def test_good_sample_video_has_valid_stream_metrics():
    analyzer = VideoAnalyzer(str(ROOT / "data" / "samples" / "good_internet.mp4"))

    metrics = analyzer.inspect_stream()

    assert metrics["status"] == "OPTIMAL"
    assert metrics["fps"] > 0
    assert metrics["frame_count"] > 0
    assert metrics["duration_seconds"] > 0
    assert metrics["resolution"]
    assert metrics["sharpness_score"] > MIN_SHARPNESS_SCORE
    assert metrics["issues"] == []


# ---------------------------------------------------------------------------
# RING-018: documented limits of the image heuristics (synthetic, deterministic frames)
# ---------------------------------------------------------------------------

def test_low_texture_scene_is_flagged_as_blur_known_limitation():
    """A sharp but featureless scene (plain wall) scores low: the heuristic cannot tell it from blur."""
    wall = np.full((720, 1280, 3), 140, dtype=np.uint8)

    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(wall))

    assert metrics["sharpness_score"] < MIN_SHARPNESS_SCORE
    assert sharpness_issues(metrics)


def test_strong_horizontal_bands_are_reported_as_banding():
    rows = np.where((np.arange(720) // 8) % 2 == 0, 90, 170).astype(np.uint8)
    bands = np.repeat(rows[:, None], 1280, axis=1)
    frame = np.dstack([bands] * 3)

    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(frame))

    assert metrics["optical_banding_detected"] is True


def test_sample_video_frame_has_no_banding_or_tint():
    metrics, _ = VideoAnalyzer.analyze_frame_base64(as_canvas_jpeg(sample_frame()))

    assert metrics["optical_banding_detected"] is False
    assert not any("tint" in issue for issue in metrics["issues"])
