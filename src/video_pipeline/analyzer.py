"""
analyzer.py — Ring TelemetryOps Concierge Agent
================================================
Computer vision stream telemetry engine.
Extracts quantitative metrics from video streams (MP4/WHEP/Canvas):
resolution, FPS, bitrate, Laplacian sharpness variance, mean brightness,
color channel chromatic balance (IR-cut stuck filter), and 50Hz/60Hz flicker banding.
"""

import base64
import cv2
import numpy as np
from pathlib import Path

from src.config import severity

# Local heuristic of this project, not a Ring specification. Laplacian variance depends on
# resolution (downscaling raises it), so both analysis paths measure at the same width and
# share one threshold. Calibrated on data/samples/good_internet.mp4 (720 px: sharp frames
# 235-274, simulator Dirty Lens 12-16); see README "Sharpness threshold".
SHARPNESS_ANALYSIS_WIDTH = 720
MIN_SHARPNESS_SCORE = 40.0

# Stream limits come from the severity table (src/config/severity_thresholds.json).
STREAM_MIN_FPS = severity.limit("fps", "Low")
STREAM_MIN_BITRATE_KBPS = severity.limit("bitrate", "Low")
# Issues about the stream rather than the image; the concierge judges them as stream metrics.
STREAM_ISSUE_PREFIXES = ("Deficient frame rate", "Critically low bitrate")

# Ring burns a mandatory watermark into all its media (logo top-left; device ID, app name and time
# top-right). Its text raises the Laplacian variance and the row differences, so Ring frames are
# measured without the top band. The watermark has a fixed size in pixels: on three Ring Playground
# snapshots (1280x720, 1280x720, 640x360) its white text spans rows 15-85 in all of them, so a 10 %
# band missed most of it on the 360-row frame. 100 px keeps a margin; the 35 % cap protects tiny frames.
# Heuristic of this project, not a Ring specification.
RING_WATERMARK_BAND_PX = 100
RING_WATERMARK_MAX_FRACTION = 0.35

# Banding limits. Row-difference std > 4.5 was calibrated on the simulator (flicker 15.8 in the browser,
# 17.2 in the Python emulation). Detailed real outdoor scenes also exceed it without any flicker (Ring
# Playground: parking 7.0, snowy porch 7.7 before cropping), so Ring frames also need a clear periodic
# row profile: peak / median of the detrended row-profile spectrum, periods 4 rows to 1/4 of the height.
# Measured peak / median (Ring frames: watermark cropped; at 720 px; JPEG q85):
#   (a) three real Ring Playground frames: 6.6 (parking, std 7.0), 6.2 (motion, 2.1), 5.3 (package, 1.0);
#   (b) synthetic natural scene (smooth noise + edges): clean 5.4; with sinusoidal bands of period
#       24 rows, amplitude 10-15 %: 55-80 (std 4.4-6.4);
#   (c) simulator 50/60Hz Flicker frames (old path, not gated): 35-107 (640-1920 px canvas), clean 7.4.
# Limitation: the real calibration used only three Ring frames.
BANDING_ROW_DIFF_STD_LIMIT = 4.5
RING_BANDING_PERIODICITY_LIMIT = 12.0


def crop_ring_watermark(frame: np.ndarray) -> np.ndarray:
    """Drops the top RING_WATERMARK_BAND_PX rows (at most RING_WATERMARK_MAX_FRACTION), Ring's watermark."""
    return frame[min(RING_WATERMARK_BAND_PX, int(frame.shape[0] * RING_WATERMARK_MAX_FRACTION)):]


def banding_periodicity(gray: np.ndarray) -> float:
    """Peak / median of the detrended row-brightness spectrum, over periods of 4 rows to 1/4 of the height."""
    profile = gray.mean(axis=1).astype(float)
    n = len(profile)
    if n < 32:
        return 0.0
    k = max(9, n // 16) | 1   # odd moving-average window that removes the slow scene trend
    trend = np.convolve(np.pad(profile, k // 2, mode="edge"), np.ones(k) / k, mode="valid")
    spectrum = np.abs(np.fft.rfft((profile - trend) * np.hanning(n)))
    freqs = np.fft.rfftfreq(n)
    in_range = spectrum[(freqs >= 4 / n) & (freqs <= 1 / 4)]
    median = float(np.median(in_range))
    return round(float(in_range.max()) / median, 2) if median > 0 else 0.0


def sharpness_score(gray: np.ndarray) -> float:
    """Laplacian variance of a grayscale frame, downscaled to SHARPNESS_ANALYSIS_WIDTH if wider."""
    h, w = gray.shape[:2]
    if w > SHARPNESS_ANALYSIS_WIDTH:
        new_h = int(h * SHARPNESS_ANALYSIS_WIDTH / w)
        gray = cv2.resize(gray, (SHARPNESS_ANALYSIS_WIDTH, new_h), interpolation=cv2.INTER_AREA)
    return round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2)


CLIP_FRAMES_CHECKED = 3
CLIP_BLACK_BRIGHTNESS = 4.0   # same black-frame limit as analyze_frame_base64


def analyze_clip(data: bytes) -> dict:
    """
    Recording stats of a Ring MP4 clip and its CLIP_FRAMES_CHECKED best frames (sharpest non-black, measured
    without the watermark band) as in-memory JPEGs, best first. OpenCV cannot read an MP4 from memory, so the
    bytes go to a temporary file in the system temp directory (owner-only permissions where the OS supports
    them); it is deleted in `finally`, also when decoding fails, and its path is never logged.
    Raises ValueError when the clip cannot be decoded.
    """
    import os
    import tempfile

    fd, path = tempfile.mkstemp(prefix="ring-clip-", suffix=".mp4")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                raise ValueError("Could not open the Ring clip")
            nominal_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            declared_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            frames_read, width, height, candidates = 0, 0, 0, []
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                frames_read += 1
                height, width = frame.shape[:2]
                gray = cv2.cvtColor(crop_ring_watermark(frame), cv2.COLOR_BGR2GRAY)
                if float(np.mean(gray)) >= CLIP_BLACK_BRIGHTNESS:
                    candidates.append((sharpness_score(gray), frames_read, frame))
                    candidates = sorted(candidates, key=lambda c: c[0], reverse=True)[:CLIP_FRAMES_CHECKED]
        finally:
            cap.release()
    finally:
        os.unlink(path)
    if frames_read == 0 or nominal_fps <= 0:
        raise ValueError("The Ring clip has no decodable frames")
    # Duration from the container's frame count; effective fps = frames actually decoded over that duration.
    duration = (declared_frames if declared_frames > 0 else frames_read) / nominal_fps
    if not candidates:   # every frame black: check them anyway, the image check reports it
        candidates = [(0.0, 0, np.zeros((height, width, 3), np.uint8))]
    frames = [cv2.imencode(".jpg", c[2], [int(cv2.IMWRITE_JPEG_QUALITY), 92])[1].tobytes() for c in candidates]
    return {
        "duration_seconds": round(duration, 2),
        "fps": round(frames_read / duration, 2),
        "frame_count": frames_read,
        "width": width,
        "height": height,
        "resolution": f"{width}x{height}",
        "bitrate_mbps": round(len(data) * 8 / duration / 1e6, 2),
        "frames": frames,
    }


class VideoAnalyzer:
    def __init__(self, video_path: str, crop_watermark: bool = False):
        """`crop_watermark`: measure Ring media without the watermark band (off for the simulator and samples)."""
        self.video_path = Path(video_path)
        self.crop_watermark = crop_watermark
        if not self.video_path.exists():
            raise FileNotFoundError(f"File not found: {self.video_path}")

    def extract_keyframe(self, output_path: str, target_second: float = 1.0) -> str:
        """Extracts a frame at specific timestamp and saves it to disk."""
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise ValueError(f"Could not open video stream: {self.video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        target_frame = int(target_second * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)

        ret, frame = cap.read()
        cap.release()

        if not ret or frame is None:
            raise RuntimeError(f"Could not extract frame at second {target_second}")

        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_file), frame)
        return str(out_file)

    def get_frame_base64(
        self,
        target_second: float = 1.0,
        max_width: int = 720,
        jpeg_quality: int = 75
    ) -> str:
        """
        Extracts a keyframe at target second and returns an optimized Base64 JPEG string.
        - Does not write to disk.
        - Resizes to max_width if dimensions exceed threshold.
        - Compresses to target JPEG quality to minimize token cost and latency.
        """
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise ValueError(f"Could not open video stream: {self.video_path}")

        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        target_frame = int(target_second * fps)

        if target_frame >= total_frames:
            target_frame = max(0, total_frames - 1)

        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
        ret, frame = cap.read()
        cap.release()

        if not ret or frame is None:
            raise RuntimeError(
                f"Could not extract frame at second {target_second} "
                f"(frame {target_frame}/{total_frames})"
            )

        h, w = frame.shape[:2]
        if w > max_width:
            scale = max_width / w
            frame = cv2.resize(
                frame,
                (max_width, int(h * scale)),
                interpolation=cv2.INTER_AREA
            )

        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
        success, buffer = cv2.imencode(".jpg", frame, encode_params)
        if not success:
            raise RuntimeError("Error encoding frame to JPEG")

        return base64.b64encode(buffer).decode("utf-8")

    @staticmethod
    def mask_privacy_zones(frame: np.ndarray, privacy_zones: list) -> np.ndarray:
        """
        Applies opaque black masks over user-configured Privacy Zones
        (per Ring Privacy Guidelines and Device Configuration API).
        Coordinates are normalized (0.0 to 1.0).
        """
        if not privacy_zones or frame is None:
            return frame
        h, w = frame.shape[:2]
        masked = frame.copy()
        for zone in privacy_zones:
            points = zone.get("points") or zone.get("polygon") or []
            if points:
                pts = np.array([[int(p[0] * w), int(p[1] * h)] for p in points], np.int32)
                cv2.fillPoly(masked, [pts], (0, 0, 0))
        return masked

    @classmethod
    def analyze_frame_base64(
        cls,
        frame_base64: str,
        telemetry: dict = None,
        max_width: int = 720,
        jpeg_quality: int = 75,
        crop_watermark: bool = False,
    ) -> tuple[dict, str]:
        """
        Analyzes a Base64 frame in-memory (extracted directly from live WebRTC Canvas).
        Computes sharpness (Laplacian variance), mean brightness, chromatic balance (magenta tint),
        and rolling-shutter 50Hz/60Hz AC flicker banding.

        `crop_watermark`: drop the Ring watermark band before measuring (the returned image is
        cropped too, so the model sees the measured pixels). Off by default.

        Returns: (video_metrics: dict, clean_image_base64: str)
        """
        image_bytes = base64.b64decode(frame_base64)
        np_arr = np.frombuffer(image_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("Could not decode Base64 frame")
        if crop_watermark:
            frame = crop_ring_watermark(frame)

        # Measured before the max_width resize so the score does not depend on the caller's max_width.
        laplacian_var = sharpness_score(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))

        h, w = frame.shape[:2]
        if w > max_width:
            scale = max_width / w
            frame = cv2.resize(frame, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)
            h, w = frame.shape[:2]

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(np.mean(gray))

        # BGR color channels
        b_mean = float(np.mean(frame[:, :, 0]))
        g_mean = float(np.mean(frame[:, :, 1]))
        r_mean = float(np.mean(frame[:, :, 2]))

        issues = []
        status = "OPTIMAL"

        # The black-frame (4.0), magenta-tint (1.35 / 1.15) and banding (4.5) limits below are local
        # heuristics checked only on a few synthetic frames: they are not calibrated and not Ring specs.
        # Black frame / complete power loss / 0 VAC
        if brightness < 4.0:
            issues.append("Completely black screen / no optical signal (0 lux, power loss or sensor offline)")
            status = "CRITICAL"
            laplacian_var = 0.0
        else:
            # Magenta / pink tint in daylight: a mechanical IR-cut filter may be stuck (it should switch back to
            # colour day mode; KB-RING-005 / KB-RING-007).
            if r_mean > 1.35 * (g_mean + 5) and b_mean > 1.15 * (g_mean + 5):
                issues.append("Severe chromatic imbalance: possible stuck mechanical IR-cut filter "
                              "(pink/purple tint in daylight)")
                status = "DEGRADED"

            # Optical blur / dirty or obstructed lens
            if laplacian_var < MIN_SHARPNESS_SCORE:
                issues.append(f"Deficient sharpness / optical blur or obstructed lens (Laplacian score {laplacian_var} < {MIN_SHARPNESS_SCORE})")
                status = "DEGRADED"

            # Mathematical detection of horizontal rolling shutter banding (50Hz/60Hz AC light interference)
            row_profile = np.mean(gray, axis=1)
            row_diff_std = float(np.std(np.diff(row_profile)))
            optical_banding_detected = row_diff_std > BANDING_ROW_DIFF_STD_LIMIT
            # Ring frames: real outdoor detail also varies a lot between rows, so require periodicity too.
            if crop_watermark:
                periodicity = banding_periodicity(gray)
                optical_banding_detected = optical_banding_detected and periodicity > RING_BANDING_PERIODICITY_LIMIT

            if optical_banding_detected:
                issues.append(f"Periodic horizontal video banding from 50Hz/60Hz LED frequency (rolling shutter variance {row_diff_std:.1f} > 4.5)")
                status = "DEGRADED"

        # WebRTC metrics correlation if telemetry is attached
        rtp = (telemetry or {}).get("webrtc_stats", {}).get("inbound_rtp", {})
        fps_value = rtp.get("frames_per_second")
        bitrate_value = rtp.get("bitrate_kbps")
        fps = float(fps_value) if fps_value is not None else None
        bitrate = float(bitrate_value) if bitrate_value is not None else None

        if fps is not None and fps < STREAM_MIN_FPS and status != "CRITICAL":
            issues.append(f"Deficient frame rate ({fps} fps < {STREAM_MIN_FPS:g} fps)")
            status = "DEGRADED"
        if bitrate is not None and bitrate < STREAM_MIN_BITRATE_KBPS and status != "CRITICAL":
            issues.append(f"Critically low bitrate ({bitrate} kbps < {STREAM_MIN_BITRATE_KBPS:g} kbps)")
            status = "DEGRADED"

        metrics = {
            "file": "live_canvas_stream",
            "resolution": f"{w}x{h}",
            "fps": round(fps, 2) if fps is not None else None,
            "frame_count": 1,
            "duration_seconds": 1.0,
            "bitrate_kbps": round(bitrate, 2) if bitrate is not None else None,
            "sharpness_score": laplacian_var,
            "mean_brightness": round(brightness, 2),
            "optical_banding_score": round(row_diff_std, 2) if 'row_diff_std' in locals() else 0.0,
            "optical_banding_detected": optical_banding_detected if 'optical_banding_detected' in locals() else False,
            "status": status,
            "issues": issues,
            "evidence_frame": None,
        }

        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
        _, buffer = cv2.imencode(".jpg", frame, encode_params)
        clean_base64 = base64.b64encode(buffer).decode("utf-8")

        return metrics, clean_base64

    def inspect_stream(self) -> dict:
        """Inspects an MP4 stream from disk computing video telemetry."""
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise ValueError(f"Could not open video stream: {self.video_path}")

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = round(frame_count / fps, 2) if fps > 0 else 0.0

        blur_scores = []
        frames_read = 0

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            frames_read += 1
            if frames_read % 5 == 0:
                if self.crop_watermark:
                    frame = crop_ring_watermark(frame)
                blur_scores.append(sharpness_score(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))

        cap.release()

        avg_blur = round(float(np.mean(blur_scores)), 2) if blur_scores else 0.0
        file_size_bytes = self.video_path.stat().st_size
        bitrate_kbps = round((file_size_bytes * 8) / (duration * 1000), 2) if duration > 0 else 0.0

        issues = []
        if height < 720:
            issues.append(f"Low resolution ({width}x{height} < 720p)")
        if fps < STREAM_MIN_FPS:
            issues.append(f"Deficient frame rate ({fps} fps < {STREAM_MIN_FPS:g} fps)")
        if bitrate_kbps < STREAM_MIN_BITRATE_KBPS:
            issues.append(f"Critically low bitrate ({bitrate_kbps} kbps < {STREAM_MIN_BITRATE_KBPS:g} kbps)")
        if avg_blur < MIN_SHARPNESS_SCORE:
            issues.append(f"Deficient sharpness / optical blur or obstructed lens (Laplacian score: {avg_blur} < {MIN_SHARPNESS_SCORE})")

        status = "OPTIMAL" if not issues else "DEGRADED"

        extracted_frame = None
        if status == "DEGRADED":
            frame_dest = f"data/frames/{self.video_path.stem}_evidence.jpg"
            extracted_frame = self.extract_keyframe(frame_dest, target_second=1.0)

        return {
            "file": self.video_path.name,
            "resolution": f"{width}x{height}",
            "fps": round(fps, 2),
            "frame_count": frame_count,
            "duration_seconds": duration,
            "bitrate_kbps": bitrate_kbps,
            "sharpness_score": avg_blur,
            "status": status,
            "issues": issues,
            "evidence_frame": extracted_frame,
        }


if __name__ == "__main__":
    import json
    import sys

    sample_path = sys.argv[1] if len(sys.argv) > 1 else "data/samples/good_internet.mp4"
    analyzer = VideoAnalyzer(sample_path)
    metrics = analyzer.inspect_stream()
    print(json.dumps(metrics, indent=2, ensure_ascii=False))