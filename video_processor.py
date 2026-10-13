import os
import cv2
import base64

def extract_frame_as_base64(video_path: str, timestamp_sec: float = 1.0) -> str:
    """
    Opens an MP4 video with OpenCV, extracts the frame at target timestamp,
    optimizes it, and returns it as a Base64 string for Bedrock multimodal inference.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"File not found: {video_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Compute target frame
    target_frame = int(timestamp_sec * (fps if fps > 0 else 30))
    if target_frame >= total_frames:
        target_frame = max(0, total_frames - 1)

    cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
    success, frame = cap.read()
    cap.release()

    if not success or frame is None:
        raise RuntimeError(f"Could not read frame at second {timestamp_sec}")

    # Resize to max 1280px width to optimize latency and token cost
    height, width = frame.shape[:2]
    if width > 1280:
        scale = 1280 / width
        frame = cv2.resize(frame, (1280, int(height * scale)), interpolation=cv2.INTER_AREA)

    # Encode to JPEG (quality 80)
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
    result, buffer = cv2.imencode('.jpg', frame, encode_param)
    if not result:
        raise RuntimeError("Error encoding frame to JPEG")

    return base64.b64encode(buffer).decode('utf-8')

if __name__ == "__main__":
    video = "data/samples/bad_internet.mp4"
    print(f"Testing frame extraction on: {video}...")
    b64_data = extract_frame_as_base64(video, timestamp_sec=2.0)
    print(f"Frame extracted successfully. Base64 length: {len(b64_data)} characters.")