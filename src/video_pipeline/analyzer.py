import cv2
import numpy as np
from pathlib import Path


class VideoAnalyzer:
    def __init__(self, video_path: str):
        self.video_path = Path(video_path)
        if not self.video_path.exists():
            raise FileNotFoundError(f"No se encontró el archivo: {self.video_path}")

    def inspect_stream(self) -> dict:
        cap = cv2.VideoCapture(str(self.video_path))
        if not cap.isOpened():
            raise ValueError(f"No se pudo abrir el stream de vídeo: {self.video_path}")

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

        # Cálculo de bitrate aproximado en kbps
        file_size_bytes = self.video_path.stat().st_size
        bitrate_kbps = round((file_size_bytes * 8) / (duration * 1000), 2) if duration > 0 else 0.0

        # Diagnóstico heurístico de calidad
        issues = []
        if height < 720:
            issues.append(f"Resolución baja ({width}x{height} < 720p)")
        if fps < 20.0:
            issues.append(f"Tasa de cuadros deficiente ({fps} fps < 20 fps)")
        if bitrate_kbps < 150.0:
            issues.append(f"Bitrate excesivamente bajo ({bitrate_kbps} kbps)")

        status = "OPTIMAL" if not issues else "DEGRADED"

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
        }


if __name__ == "__main__":
    import json
    import sys

    sample_path = sys.argv[1] if len(sys.argv) > 1 else "data/samples/test_stream_good.mp4"
    analyzer = VideoAnalyzer(sample_path)
    metrics = analyzer.inspect_stream()
    print(json.dumps(metrics, indent=2))