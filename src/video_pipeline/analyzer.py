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


class VideoAnalyzer:
    def __init__(self, video_path: str):
        self.video_path = Path(video_path)
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
        jpeg_quality: int = 75
    ) -> tuple[dict, str]:
        """
        Analyzes a Base64 frame in-memory (extracted directly from live WebRTC Canvas).
        Computes sharpness (Laplacian variance), mean brightness, chromatic balance (magenta tint),
        and rolling-shutter 50Hz/60Hz AC flicker banding.

        Returns: (video_metrics: dict, clean_image_base64: str)
        """
        image_bytes = base64.b64decode(frame_base64)
        np_arr = np.frombuffer(image_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("Could not decode Base64 frame")

        h, w = frame.shape[:2]
        if w > max_width:
            scale = max_width / w
            frame = cv2.resize(frame, (max_width, int(h * scale)), interpolation=cv2.INTER_AREA)
            h, w = frame.shape[:2]

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        brightness = float(np.mean(gray))
        laplacian_var = round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2)

        # BGR color channels
        b_mean = float(np.mean(frame[:, :, 0]))
        g_mean = float(np.mean(frame[:, :, 1]))
        r_mean = float(np.mean(frame[:, :, 2]))

        issues = []
        status = "OPTIMAL"

        # Black frame / complete power loss / 0 VAC
        if brightness < 4.0:
            issues.append("Completely black screen / no optical signal (0 lux, power loss or sensor offline)")
            status = "CRITICAL"
            laplacian_var = 0.0
        else:
            # Magenta / pink tint (IR-cut mechanical filter stuck in day mode)
            if r_mean > 1.35 * (g_mean + 5) and b_mean > 1.15 * (g_mean + 5):
                issues.append("Severe chromatic imbalance: pink/purple tint (mechanical IR-cut filter stuck in day position)")
                status = "DEGRADED"

            # Optical blur / dirty or obstructed lens
            if laplacian_var < 15.0:
                issues.append(f"Deficient sharpness / optical blur or obstructed lens (Laplacian score {laplacian_var} < 20.0)")
                status = "DEGRADED"

            # Mathematical detection of horizontal rolling shutter banding (50Hz/60Hz AC light interference)
            row_profile = np.mean(gray, axis=1)
            row_diff_std = float(np.std(np.diff(row_profile)))
            optical_banding_detected = row_diff_std > 4.5

            if optical_banding_detected:
                issues.append(f"Periodic horizontal video banding from 50Hz/60Hz LED frequency (rolling shutter variance {row_diff_std:.1f} > 4.5)")
                status = "DEGRADED"

        # WebRTC metrics correlation if telemetry is attached
        rtp = (telemetry or {}).get("webrtc_stats", {}).get("inbound_rtp", {})
        fps = float(rtp.get("frames_per_second", 30.0))
        bitrate = float(rtp.get("bitrate_kbps", 1200.0))

        if fps < 20.0 and status != "CRITICAL":
            issues.append(f"Deficient frame rate ({fps} fps < 20 fps)")
            status = "DEGRADED"
        if bitrate < 150.0 and status != "CRITICAL":
            issues.append(f"Critically low bitrate ({bitrate} kbps)")
            status = "DEGRADED"

        metrics = {
            "file": "live_canvas_stream",
            "resolution": f"{w}x{h}",
            "fps": round(fps, 2),
            "frame_count": 1,
            "duration_seconds": 1.0,
            "bitrate_kbps": round(bitrate, 2),
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
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
                blur_scores.append(laplacian_var)

        cap.release()

        avg_blur = round(float(np.mean(blur_scores)), 2) if blur_scores else 0.0
        file_size_bytes = self.video_path.stat().st_size
        bitrate_kbps = round((file_size_bytes * 8) / (duration * 1000), 2) if duration > 0 else 0.0

        issues = []
        if height < 720:
            issues.append(f"Low resolution ({width}x{height} < 720p)")
        if fps < 20.0:
            issues.append(f"Deficient frame rate ({fps} fps < 20 fps)")
        if bitrate_kbps < 150.0:
            issues.append(f"Critically low bitrate ({bitrate_kbps} kbps)")
        if avg_blur < 20.0:
            issues.append(f"Deficient sharpness or obstructed lens (Laplacian score: {avg_blur} < 20.0)")

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