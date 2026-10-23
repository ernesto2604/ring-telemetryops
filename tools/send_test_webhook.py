"""
send_test_webhook.py — sends one signed Ring-style webhook (payload v1.1) to the local server.

    python -m tools.send_test_webhook --type motion_detected [--timestamp-ms MS] [--sub-type human]

The HMAC key is read from RING_PARTNER_HMAC_KEY (environment, then .env) and never printed. The
default timestamp is the start of the newest event the local server lists (GET /api/ring/events),
else the current time. Only the HTTP status and one summary line are printed.
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
SERVER = "http://127.0.0.1:8000"
KEY_ENV_VAR = "RING_PARTNER_HMAC_KEY"
TYPES = ("motion_detected", "button_press", "device_offline")


def setting(name: str) -> str:
    return (os.environ.get(name) or dotenv_values(ROOT / ".env").get(name) or "").strip()


def build_payload(kind: str, device_id: str, timestamp_ms: int, sub_type: Optional[str] = None) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {"source": device_id, "source_type": "device", "timestamp": timestamp_ms}
    if kind == "motion_detected":
        attributes["sub_type"] = sub_type or "motion"
    return {
        "meta": {"version": "1.1", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "request_id": str(uuid.uuid4()), "account_id": "test-account"},
        "data": {"id": str(uuid.uuid4()), "type": kind, "attributes": attributes},
    }


def sign(body: bytes, key: str) -> str:
    return "sha256=" + hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def newest_event_start(client: httpx.Client) -> Optional[int]:
    try:
        events = client.get(f"{SERVER}/api/ring/events").json().get("events") or []
    except (httpx.HTTPError, ValueError):
        return None
    starts = [e.get("start") for e in events if isinstance(e.get("start"), int)]
    return max(starts) if starts else None


def default_device(client: httpx.Client) -> Optional[str]:
    device = setting("RING_PARTNER_DEVICE_ID")
    if device:
        return device
    try:
        devices = client.get(f"{SERVER}/api/ring/devices").json().get("devices") or []
    except (httpx.HTTPError, ValueError):
        return None
    return devices[0].get("device_id") if devices else None


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Send one signed test webhook to the local server.")
    parser.add_argument("--type", choices=TYPES, default="motion_detected")
    parser.add_argument("--timestamp-ms", type=int)
    parser.add_argument("--sub-type", help="motion_detected only (default: motion)")
    parser.add_argument("--device-id", help="default: RING_PARTNER_DEVICE_ID, else the first Ring device")
    args = parser.parse_args(argv)

    key = setting(KEY_ENV_VAR)
    if not key:
        print(f"{KEY_ENV_VAR} is not set (environment or .env); nothing was sent.")
        return 2
    with httpx.Client(timeout=10) as client:
        device_id = args.device_id or default_device(client)
        if not device_id:
            print("No Ring device found: pass --device-id; nothing was sent.")
            return 2
        timestamp = args.timestamp_ms or newest_event_start(client) or int(time.time() * 1000)
        body = json.dumps(build_payload(args.type, device_id, timestamp, args.sub_type)).encode("utf-8")
        try:
            response = client.post(f"{SERVER}/webhooks/ring", content=body,
                                   headers={"Content-Type": "application/json", "X-Signature": sign(body, key)})
        except httpx.HTTPError as exc:
            print(f"Request failed ({type(exc).__name__}): is the server running on {SERVER}?")
            return 1
    try:
        status = response.json().get("status") or response.json().get("detail")
    except ValueError:
        status = None
    print(f"HTTP {response.status_code}")
    print(f"{args.type} at {timestamp} ms -> {status or 'no status'}")
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
