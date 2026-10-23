"""
RingPartnerClient — Ring TelemetryOps Concierge Agent
======================================================
Read-only client for the official Ring API (JSON:API over HTTPS).

Endpoints used, as described in developer.amazon.com/docs/ring/api-documentation.html
(not re-verified against that page for this release):
  GET /v1/devices                            Device Discovery
  GET /v1/devices/{device_id}?include=status,capabilities,configurations,location
                                             One device with its related resources (JSON:API
                                             `included`); individual calls below are the fallback
  GET /v1/devices/{device_id}/status         Device Status
  GET /v1/devices/{device_id}/capabilities   Device Capabilities
  POST /v1/devices/{device_id}/media/image/download, then GET of the pre-signed Location
                                             Image snapshot (the token is never sent to that URL)
  POST /v1/devices/{device_id}/media/video/download
                                             Recorded clip: 200/206 with the MP4, or a redirect to a
                                             pre-signed URL fetched without the token
  GET /v1/history/devices/{device_id}/events  Event History (paginated by links.next)

REQUIRED HEADERS:
  - Every request sends `User-Agent: RingTelemetryOps/<version>` and `Hardware_Id: <UUID>`.
    Without them Ring's firewall can answer HTTP 406 (raised as RingBlockedError).

SECURITY:
  - The access token is read exclusively from RING_PARTNER_ACCESS_TOKEN.
  - It is only sent in the Authorization header. It is never logged,
    returned in normalized payloads, or included in exception messages.

DATA INTEGRITY:
  - `online` is passed through exactly as reported; a missing value stays None.
  - Battery is reported only when capabilities declare support, the device is
    online, and the value is within 0–100 (Ring documents 255 as "no battery").
  - RSSI and transformer voltage are not provided by the documented API, so
    they are always None and marked "not_provided_by_api".
  - list_devices() fetches status/capabilities per device with bounded
    concurrency. A per-device failure leaves that data unknown (None) and is
    recorded in `fetch_errors`; it is never reported as a successful read.
"""

import asyncio
import email.utils
import json
import logging
import math
import os
import re
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
import cv2
import numpy as np

logger = logging.getLogger(__name__)

RING_API_BASE_URL = "https://api.amazonvision.com"
BASE_URL_ENV_VAR = "RING_PARTNER_BASE_URL"
TOKEN_ENV_VAR = "RING_PARTNER_ACCESS_TOKEN"
HARDWARE_ID_ENV_VAR = "RING_PARTNER_HARDWARE_ID"
APP_VERSION = "1.0.0"
USER_AGENT = f"RingTelemetryOps/{APP_VERSION}"
NOT_PROVIDED_BY_API = "not_provided_by_api"


# ---------------------------------------------------------------------------
# Errors (messages never contain the access token)
# ---------------------------------------------------------------------------

class RingPartnerError(Exception):
    """Base error for Ring API failures. `title`/`detail` come from the JSON:API error object."""

    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None,
                 title: Optional[str] = None, detail: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail


class RingNotConfiguredError(RingPartnerError):
    """RING_PARTNER_ACCESS_TOKEN is not set."""


class RingTimeoutError(RingPartnerError):
    """The request did not complete within the configured timeout."""


class RingUnauthorizedError(RingPartnerError):
    """HTTP 401 — missing, invalid, or expired access token."""


class RingForbiddenError(RingPartnerError):
    """HTTP 403 — the app is not authorized for this resource."""


class RingNotFoundError(RingPartnerError):
    """HTTP 404 — unknown device or resource."""


class RingBlockedError(RingPartnerError):
    """
    HTTP 406 — Ring's firewall rejected the request: missing required headers, a datacenter IP, or an IP
    that Ring Security blocked for irregular traffic (not retryable).
    """


class RingTimeRangeNotAuthorizedError(RingPartnerError):
    """
    HTTP 403 with code TIME_RANGE_NOT_AUTHORIZED: the requested time is before the user's consent.
    Not a credential error: other (newer) media can still be read.
    """


class RingRateLimitError(RingPartnerError):
    """HTTP 429 — rate limit exceeded; `retry_after` is in seconds, if provided."""

    def __init__(self, message: str, retry_after: Optional[float] = None, **kwargs: Any):
        kwargs.setdefault("status", 429)
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class RingServiceUnavailableError(RingPartnerError):
    """
    HTTP 503 — Ring documents several causes (device offline, server busy).
    Read `title`/`detail` for the reason; never infer the device's `online` state from it.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None, **kwargs: Any):
        kwargs.setdefault("status", 503)
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


TIME_RANGE_NOT_AUTHORIZED = "TIME_RANGE_NOT_AUTHORIZED"
TIME_RANGE_NOT_AUTHORIZED_TEXT = (
    "Ring only allows access to recordings made after you connected this app; the requested time is earlier."
)
BLOCKED_406_TEXT = (
    "Ring blocked the request: required headers are missing, the request came from a datacenter IP, "
    "or Ring Security blocked this IP (not retryable; contact Ring developer support)"
)

# Ring documents exponential backoff starting at 1 second for transient server errors. 503 is not
# retried: Ring documents it mostly as "device offline", which a quick retry does not change.
TRANSIENT_STATUSES = (500, 502, 504)
TRANSIENT_BACKOFF_BASE_SECONDS = 1.0

_STATUS_ERRORS = {
    401: RingUnauthorizedError,
    403: RingForbiddenError,
    404: RingNotFoundError,
    406: RingBlockedError,
    429: RingRateLimitError,
    503: RingServiceUnavailableError,
}


def hardware_id() -> str:
    """
    Hardware_Id header: RING_PARTNER_HARDWARE_ID when set, else a UUID5 of this host's name and
    RING_PARTNER_DEVICE_ID, so it stays the same between runs on the same machine.
    """
    configured = os.getenv(HARDWARE_ID_ENV_VAR, "").strip()
    if configured:
        return configured
    seed = f"{socket.gethostname()}|{os.getenv('RING_PARTNER_DEVICE_ID', '').strip()}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"ring-telemetryops.{seed}"))


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Parses a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = when.timestamp() - (time.time() if now is None else now)
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


# ---------------------------------------------------------------------------
# JSON:API normalization (pure functions)
# ---------------------------------------------------------------------------

def _attributes(resource: Any) -> Dict[str, Any]:
    if not isinstance(resource, dict):
        return {}
    attrs = resource.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _battery_supported(capability_attrs: Optional[Dict[str, Any]]) -> Optional[bool]:
    """True/False from capabilities; None when capabilities are unknown."""
    if capability_attrs is None:
        return None
    # shortcut: docs state "battery_status.supported" but show no example payload;
    # confirm the exact shape against the Playground and widen this check if needed.
    battery = capability_attrs.get("battery_status")
    return isinstance(battery, dict) and battery.get("supported") is True


def _reported_at(value: Any) -> tuple:
    """
    Validates `reported_at` (Ring: last check-in, ISO 8601, updated on every device uplink).
    It is NOT the current time. Ring returns null or the Unix epoch as a placeholder for a
    device that has not checked in, so those are treated as not reported.
    Returns (value or None, measurement state).
    """
    if value is None:
        return None, "not_reported"
    if not isinstance(value, str):
        return None, "invalid"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, "invalid"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.timestamp() <= 0:
        return None, "not_reported"  # documented epoch placeholder
    return value, "reported_by_api"


def build_device_health(
    status_attrs: Optional[Dict[str, Any]],
    capability_attrs: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Builds device health from documented attributes without inventing metrics."""
    status_attrs = status_attrs or {}
    online = status_attrs.get("online")
    online = online if isinstance(online, bool) else None

    battery: Optional[int] = None
    supported = _battery_supported(capability_attrs)
    if supported is None:
        battery_state = "capabilities_unknown"
    elif not supported:
        battery_state = "not_supported"
    elif online is not True:
        # Ring: gate on `online` before reading any other attribute.
        battery_state = "not_reported"
    else:
        pct = (status_attrs.get("battery_status") or {}).get("percentage")
        if isinstance(pct, int) and not isinstance(pct, bool) and 0 <= pct <= 100:
            battery = pct
            battery_state = "reported_by_api"
        else:
            battery_state = "not_reported"

    # Ring: gate on `online` before reading any other attribute.
    reported_at, reported_at_state = (
        _reported_at(status_attrs.get("reported_at")) if online is True else (None, "not_reported")
    )

    return {
        "online": online,
        "reported_at": reported_at,
        "battery_percentage": battery,
        "rssi": None,
        "transformer_voltage_vac": None,
        "measurement_status": {
            "online": "reported_by_api" if online is not None else "not_reported",
            "reported_at": reported_at_state,
            "battery_percentage": battery_state,
            "rssi": NOT_PROVIDED_BY_API,
            "transformer_voltage_vac": NOT_PROVIDED_BY_API,
        },
    }


def normalize_device(
    resource: Dict[str, Any],
    status_resource: Optional[Dict[str, Any]] = None,
    capabilities_resource: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalizes one `devices` resource plus its related status/capabilities."""
    capability_attrs = _attributes(capabilities_resource) if capabilities_resource else None
    status_attrs = _attributes(status_resource) if status_resource else None
    return {
        "source": "ring_partner_api",
        "device_id": resource.get("id"),
        "name": _attributes(resource).get("name"),
        "device_health": build_device_health(status_attrs, capability_attrs),
        "capabilities": capability_attrs,
    }


DEVICE_INCLUDE = "status,capabilities,configurations,location"

# shortcut: included resources are matched by a keyword in their JSON:API `type` (the docs show no
# example type names); match on `relationships` instead if Ring's types turn out ambiguous.
_INCLUDED_KINDS = (("status", "status"), ("capabilit", "capabilities"),
                   ("configuration", "configurations"), ("location", "location"))


def _included_by_kind(document: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Maps the `included` array of a single-device document to status/capabilities/configurations/location."""
    found: Dict[str, Dict[str, Any]] = {}
    included = document.get("included")
    for resource in included if isinstance(included, list) else []:
        res_type = resource.get("type") if isinstance(resource, dict) else None
        if not isinstance(res_type, str):
            continue
        for keyword, kind in _INCLUDED_KINDS:
            if keyword in res_type.lower():
                found.setdefault(kind, resource)
                break
    return found


def _on_off(value: Any) -> Optional[bool]:
    """Ring reports these settings as the strings "on"/"off"; anything else is unknown."""
    return {"on": True, "off": False}.get(value) if isinstance(value, str) else None


def _sub(attrs: Optional[Dict[str, Any]], key: str) -> Dict[str, Any]:
    value = (attrs or {}).get(key)
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> Optional[int]:
    return len(value) if isinstance(value, list) else None


def is_video_readable(config_attrs: Optional[Dict[str, Any]]) -> bool:
    """
    False only when Ring reports `e2e_encryption.enabled` as the boolean true: media requests then
    succeed but return encrypted content (an expected outcome, never retried).
    """
    return _sub(config_attrs, "e2e_encryption").get("enabled") is not True


_ENHANCEMENT_SETTINGS = ("color_night_vision", "hdr", "ir_led_night_vision", "auto_zoom_track", "privacy_zones")


def build_device_context(
    status_attrs: Optional[Dict[str, Any]],
    capability_attrs: Optional[Dict[str, Any]],
    config_attrs: Optional[Dict[str, Any]],
    location_attrs: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Device settings exactly as Ring reports them; anything missing or malformed is None. When the
    capabilities list image enhancements, a setting the device does not support is None and is named
    in `unsupported_settings`, whatever value the configurations carry.
    """
    e2e = _sub(config_attrs, "e2e_encryption").get("enabled")
    motion = _sub(config_attrs, "motion_detection")
    enhancements = dict(_sub(config_attrs, "image_enhancements"))
    supported = _sub(capability_attrs, "image_enhancements").get("configurations")
    supported = supported if isinstance(supported, list) else None
    unsupported = [name for name in _ENHANCEMENT_SETTINGS if supported is not None and name not in supported]
    for name in unsupported:
        enhancements.pop(name, None)
    video = _sub(capability_attrs, "video")
    snooze = _sub(_sub(status_attrs, "audio"), "snooze")
    location = location_attrs or {}
    resolutions = video.get("supported_resolutions")
    text = lambda v: v if isinstance(v, str) else None  # noqa: E731
    return {
        "e2e_encryption_enabled": e2e if isinstance(e2e, bool) else None,
        # None when the configurations were not fetched (fallback path): readability is unknown.
        "media_readable": is_video_readable(config_attrs) if config_attrs is not None else None,
        # Snapshots are requested only when the capabilities list them.
        "snapshot_supported": supported is not None and "snapshot" in supported,
        "unsupported_settings": unsupported,
        "motion_detection": {
            "enabled": _on_off(motion.get("enabled")),
            "zones": _count(motion.get("motion_zones")),
        },
        "image_enhancements": {
            "color_night_vision": _on_off(enhancements.get("color_night_vision")),
            "hdr": _on_off(enhancements.get("hdr")),
            "ir_led_night_vision": _on_off(enhancements.get("ir_led_night_vision")),
            "auto_zoom_track": _on_off(enhancements.get("auto_zoom_track")),
            "privacy_zones": _count(enhancements.get("privacy_zones")),
        },
        "video": {
            "max_resolution": video.get("max_resolution"),
            "supported_resolutions": resolutions if isinstance(resolutions, list) else None,
        },
        "location": {"country": text(location.get("country")), "state": text(location.get("state"))},
        "snooze": {
            "active": snooze.get("active") if isinstance(snooze.get("active"), bool) else None,
            "until": text(snooze.get("until")),
        },
    }


SNAPSHOT_WINDOW_MS = 24 * 3600 * 1000      # Ring's maximum latest_in_range window
SNAPSHOT_WAIT_BUDGET_SECONDS = 15.0        # total wait for a recording that is not ready (Analyze now)
SNAPSHOT_MAX_BYTES = 10 * 1024 * 1024      # design limit for one image, not a Ring value
SNAPSHOT_ERROR_MAX_BYTES = 4096            # JSON error body read from a failed media download
# Undocumented 422 seen live on the pre-signed GET: "No valid Greco key available for decryption".
UNDECRYPTABLE_CODES = ("GRECO_NO_VALID_KEY",)


CLIP_DURATION_MS = 5000
CLIP_WAIT_BUDGET_SECONDS = 20.0            # total wait for 416/425 on a clip
CLIP_MAX_BYTES = 20 * 1024 * 1024          # design limit for one 5 s clip, not a Ring value
_REDIRECTS = (301, 302, 303)


def _clip_result(status: str) -> Dict[str, Any]:
    return {"status": status, "video": None, "partial": False, "media_length": None}


def _is_mp4(data: bytes) -> bool:
    """ISO base media file: the first box is `ftyp` (bytes 4-8)."""
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _snapshot_result(status: str) -> Dict[str, Any]:
    return {"status": status, "image": None, "content_type": None, "media_timestamp": None, "media_origin": None}


def _image_type(data: bytes) -> Optional[str]:
    """'image/jpeg' or 'image/png' when the bytes carry that signature and OpenCV decodes them."""
    if data[:2] == b"\xff\xd8":
        content_type = "image/jpeg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        content_type = "image/png"
    else:
        return None
    decoded = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return content_type if decoded is not None else None


def classify_media_error(body: Any) -> str:
    """
    Snapshot status for a 422 media reply from its JSON:API error body (`status`, `code`, `detail`):
    "media_undecryptable" for a decryption-key error, else "corrupt_recording". Never raises.
    """
    try:
        errors = json.loads(body).get("errors") if body else None
        error = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    except (ValueError, TypeError, AttributeError):
        error = {}
    code = error.get("code") if isinstance(error.get("code"), str) else ""
    detail = error.get("detail").lower() if isinstance(error.get("detail"), str) else ""
    if code in UNDECRYPTABLE_CODES or re.search(r"decrypt|\bkeys?\b", detail):
        return "media_undecryptable"
    return "corrupt_recording"


def first_component_id(capability_attrs: Optional[Dict[str, Any]]) -> Optional[str]:
    """First camera of a multi-camera device (`components.items[]`); None for a single camera."""
    items = _sub(capability_attrs, "components").get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    # shortcut: the docs show no item example; accept `component_id` or `id` until confirmed.
    value = items[0].get("component_id") or items[0].get("id")
    return value if isinstance(value, str) and value else None


DEFAULT_EVENT_TYPES = "ding,motion,on_demand"
EVENT_TYPES_ENV_VAR = "RING_PARTNER_EVENT_TYPES"


def normalize_event(item: Dict[str, Any]) -> Dict[str, Any]:
    """One `history-events` item; `motion.human` style types are split into event_type and sub_type."""
    attrs = _attributes(item)
    raw_type = attrs.get("event_type")
    event_type, _, sub_type = raw_type.partition(".") if isinstance(raw_type, str) else (None, "", "")
    if not sub_type and isinstance(attrs.get("sub_type"), str):
        sub_type = attrs["sub_type"]
    epoch = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else None  # noqa: E731
    reviewed = attrs.get("is_third_party_reviewed")
    return {
        "id": item.get("id") if isinstance(item.get("id"), str) else None,
        "event_type": event_type or None,
        "sub_type": sub_type or None,
        "start": epoch(attrs.get("start")),
        "end": epoch(attrs.get("end")),
        "is_third_party_reviewed": reviewed if isinstance(reviewed, bool) else None,
    }


def _fetch_error(exc: Exception) -> Dict[str, Any]:
    """Serializable record of a failed per-device request (messages are already token-redacted)."""
    return {
        "error": type(exc).__name__,
        "status": getattr(exc, "status", None),
        "code": getattr(exc, "code", None),
        "message": str(exc),
    }


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class RingPartnerClient:
    """
    Read-only Ring API client.

    Args:
        base_url:                Ring API host; default RING_PARTNER_BASE_URL, else the official
                                 host (tests point this at a local server).
        timeout_seconds:         Total timeout per HTTP request.
        max_rate_limit_retries:  Max retries after HTTP 429 (bounded; default 1).
        max_retry_after_seconds: A 429 whose Retry-After exceeds this is raised, not waited.
        max_concurrency:         Max in-flight per-device requests during list_devices().
        max_transient_retries:   Max retries after HTTP 500/502/504 or a connection error
                                 (bounded; default 1; backoff 1 s doubling, or Retry-After).
        sleep:                   Awaitable sleep, injectable for tests.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout_seconds: float = 10.0,
        max_rate_limit_retries: int = 1,
        max_retry_after_seconds: float = 10.0,
        max_concurrency: int = 4,
        max_transient_retries: int = 1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        base_url = base_url or os.getenv(BASE_URL_ENV_VAR, "").strip() or RING_API_BASE_URL
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_rate_limit_retries = max(0, int(max_rate_limit_retries))
        self.max_retry_after_seconds = max_retry_after_seconds
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_transient_retries = max(0, int(max_transient_retries))
        self._sleep = sleep
        self._token = os.getenv(TOKEN_ENV_VAR, "").strip()
        self.hardware_id = hardware_id()
        # Last X-RateLimit-Remaining Ring sent (None until a response carries it).
        self.rate_limit_remaining: Optional[int] = None

    def __repr__(self) -> str:
        return f"RingPartnerClient(base_url={self.base_url!r}, configured={self.is_configured})"

    @property
    def is_configured(self) -> bool:
        return bool(self._token)

    # -- public API ---------------------------------------------------------

    async def list_devices(self) -> List[Dict[str, Any]]:
        """
        Device Discovery, then one GET /v1/devices/{id}?include=... per device for status,
        capabilities, configurations and location. If that call fails or its `included` lacks
        status or capabilities, those come from the individual endpoints (previous behaviour).

        Discovery failures raise. Per-device failures do not: that device keeps
        unknown (None) health/capabilities and lists the failure in `fetch_errors`.
        HTTP 401 is account-wide and always raises.
        """
        path = "/v1/devices"
        document = await self._get_json(path)
        if not isinstance(document.get("data"), list):
            raise RingPartnerError(f"Ring API GET {path} returned no 'data' array")

        resources = [
            res for res in document["data"]
            if isinstance(res, dict) and res.get("type") == "devices"
        ]
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def fetch(device_id: Any, kind: str) -> Dict[str, Any]:
            async with semaphore:
                path = f"{self._device_path(device_id)}/{kind}"
                return self._single_resource(await self._get_json(path), path)

        async def fetch_included(device_id: Any) -> Dict[str, Dict[str, Any]]:
            try:
                async with semaphore:
                    path = self._device_path(device_id)
                    return _included_by_kind(await self._get_json(path, params={"include": DEVICE_INCLUDE}))
            except RingUnauthorizedError:
                raise
            except (RingPartnerError, ValueError):
                return {}  # fall back to the individual calls

        async def collect(resource: Dict[str, Any]) -> Dict[str, Any]:
            included = await fetch_included(resource.get("id"))
            kinds = [kind for kind in ("status", "capabilities") if kind not in included]
            results = await asyncio.gather(
                *(fetch(resource.get("id"), kind) for kind in kinds),
                return_exceptions=True,
            )
            found: Dict[str, Optional[Dict[str, Any]]] = {
                kind: included[kind] for kind in ("status", "capabilities") if kind in included
            }
            errors: Dict[str, Dict[str, Any]] = {}
            for kind, result in zip(kinds, results):
                if isinstance(result, RingUnauthorizedError):
                    raise result
                if isinstance(result, (RingPartnerError, ValueError)):
                    errors[kind] = _fetch_error(result)
                    found[kind] = None
                elif isinstance(result, BaseException):
                    raise result
                else:
                    found[kind] = result
            device = normalize_device(resource, found["status"], found["capabilities"])
            if "status" in errors:
                states = device["device_health"]["measurement_status"]
                states["online"] = "fetch_failed"
                if states["battery_percentage"] == "not_reported":
                    states["battery_percentage"] = "fetch_failed"
            device["fetch_errors"] = errors
            device["device_context"] = build_device_context(
                _attributes(found["status"]) if found["status"] else None,
                device["capabilities"],
                _attributes(included["configurations"]) if "configurations" in included else None,
                _attributes(included["location"]) if "location" in included else None,
            )
            return device

        return list(await asyncio.gather(*(collect(res) for res in resources)))

    async def get_status(
        self, device_id: str, capabilities: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Device status (one request). Pass capabilities already obtained at discovery to
        interpret battery; without them battery stays unknown ("capabilities_unknown").
        """
        path = f"{self._device_path(device_id)}/status"
        resource = self._single_resource(await self._get_json(path), path)
        return {"device_id": device_id, **build_device_health(_attributes(resource), capabilities)}

    async def get_capabilities(self, device_id: str) -> Dict[str, Any]:
        """Device capabilities, passed through as documented attributes."""
        path = f"{self._device_path(device_id)}/capabilities"
        resource = self._single_resource(await self._get_json(path), path)
        return {"device_id": device_id, "capabilities": _attributes(resource)}

    async def download_snapshot(
        self,
        device_id: str,
        at_timestamp_ms: Optional[int] = None,
        component_id: Optional[str] = None,
        wait_for_recording: bool = False,
        wait_budget_seconds: float = SNAPSHOT_WAIT_BUDGET_SECONDS,
    ) -> Dict[str, Any]:
        """
        Ring image snapshot, server to server in two steps: POST .../media/image/download answers
        303 with a pre-signed Location, then a GET of that URL WITHOUT the access token.

        Latest image of the last 24 h, or the image at `at_timestamp_ms` (epoch ms, for an event).
        No `resolution` is requested, so the fixed-size watermark covers the least of the image.
        425 (recording not ready) is retried within `wait_budget_seconds`; with
        `wait_for_recording`, 416 is too (an event's recording may not exist yet). 500/503 are
        retried once; 422 never.

        Returns {"status", "image", "content_type", "media_timestamp", "media_origin"}; `status` is
        "ok", "no_recent_media" (416), "not_ready", "media_undecryptable" (422 decryption-key error,
        on the POST or the pre-signed GET), "corrupt_recording" (other 422), "time_range_not_authorized"
        (403 TIME_RANGE_NOT_AUTHORIZED: before the user's consent) or "unreadable_image" (not a
        decodable JPEG/PNG within SNAPSHOT_MAX_BYTES). The image stays in memory: it is never logged.
        Other HTTP errors raise the typed errors of `_get_json`.
        """
        if not self._token:
            raise RingNotConfiguredError(f"{TOKEN_ENV_VAR} is not set; Ring API is unavailable.")
        now_ms = int(time.time() * 1000)
        if at_timestamp_ms is None:
            body: Dict[str, Any] = {"type": "latest_in_range", "start_timestamp": now_ms - SNAPSHOT_WINDOW_MS,
                                    "end_timestamp": now_ms}
        else:
            body = {"type": "at_timestamp", "timestamp": int(at_timestamp_ms)}
        body["image_options"] = {"format": "jpeg"}
        if component_id:
            body["components"] = [{"component_id": component_id}]
        path = f"{self._device_path(device_id)}/media/image/download"

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            outcome = await self._media_post(session, path, body, wait_for_recording, wait_budget_seconds)
            if outcome[0] == "failed":
                return _snapshot_result(outcome[1])
            return await self._fetch_presigned(session, outcome[1])

    async def download_clip(
        self,
        device_id: str,
        timestamp_ms: int,
        component_id: Optional[str] = None,
        duration_ms: int = CLIP_DURATION_MS,
        wait_budget_seconds: float = CLIP_WAIT_BUDGET_SECONDS,
    ) -> Dict[str, Any]:
        """
        Recorded clip of `duration_ms` from `timestamp_ms` (epoch ms, an event start), without audio.
        Ring's docs describe two replies, so both are handled: 200 (full) / 206 (partial, X-Media-Length)
        with the MP4 body, or a 301/302/303 redirect whose Location is fetched WITHOUT the access token.
        416/425 are retried within `wait_budget_seconds`; 422 never; 500/503 once.

        Returns {"status", "video", "partial", "media_length"}; `status` is "ok", "no_recent_media",
        "not_ready", "media_undecryptable", "corrupt_recording", "time_range_not_authorized",
        "clip_too_large" (over CLIP_MAX_BYTES) or "unreadable_clip" (not an MP4). The bytes stay in memory
        here and are never logged. 401/403/406 and other HTTP errors raise the typed errors.
        """
        if not self._token:
            raise RingNotConfiguredError(f"{TOKEN_ENV_VAR} is not set; Ring API is unavailable.")
        body: Dict[str, Any] = {"timestamp": int(timestamp_ms), "duration": int(duration_ms),
                                "audio_options": {"audio_enabled": False}}
        if component_id:
            body["components"] = [{"component_id": component_id}]
        path = f"{self._device_path(device_id)}/media/video/download"

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            outcome = await self._media_post(session, path, body, True, wait_budget_seconds,
                                             accept=(200, 206, *_REDIRECTS), max_bytes=CLIP_MAX_BYTES)
            if outcome[0] == "redirect":
                outcome = await self._get_presigned(session, outcome[1], CLIP_MAX_BYTES)
        if outcome[0] == "failed":
            cause = outcome[1]
            return _clip_result({"too_large": "clip_too_large", "unreadable": "unreadable_clip"}.get(cause, cause))
        _, data, headers, status = outcome
        if not _is_mp4(data):
            return _clip_result("unreadable_clip")
        length = (headers.get("X-Media-Length") or "").strip()
        return {"status": "ok", "video": data, "partial": status == 206,
                "media_length": int(length) if length.isdigit() else None}

    async def _media_post(self, session: aiohttp.ClientSession, path: str, body: Dict[str, Any],
                          wait_on_416: bool, wait_budget_seconds: float,
                          accept: tuple = (303,), max_bytes: int = 0) -> tuple:
        """
        POST of a media download. Returns ("redirect", absolute Location), ("body", bytes, headers, status)
        for an accepted 200/206, or ("failed", cause) with a result status ("too_large" for an oversized
        body). 425 (and 416 with `wait_on_416`) is retried within `wait_budget_seconds`; 500/503 once.
        """
        waited = 0.0
        attempt = 0
        retried_server_error = False
        while True:
            try:
                async with session.post(f"{self.base_url}{path}", json=body, headers=self._headers(),
                                        allow_redirects=False) as response:
                    status = response.status
                    if status in accept and status in (200, 206):
                        data = await self._read_limited(response, max_bytes)
                        if data is None:
                            return "failed", "too_large"
                        return "body", data, response.headers, status
                    text = await response.text()
                    location = response.headers.get("Location")
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
            except asyncio.TimeoutError:
                raise RingTimeoutError(
                    f"Ring API POST {path} timed out after {self.timeout_seconds}s") from None
            except aiohttp.ClientError as exc:
                raise RingPartnerError(f"Ring API POST {path} connection error ({type(exc).__name__})") from None

            if status in accept and status in _REDIRECTS and location:
                return "redirect", urljoin(f"{self.base_url}/", location)
            if status == 422:
                return "failed", classify_media_error(text)
            if status == 425 or (status == 416 and wait_on_416):
                attempt += 1
                delay = retry_after if retry_after is not None else TRANSIENT_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
                if waited + delay > wait_budget_seconds:
                    return "failed", "not_ready" if status == 425 else "no_recent_media"
                waited += delay
                await self._sleep(delay)
                continue
            if status == 416:
                return "failed", "no_recent_media"
            if status in (500, 503) and not retried_server_error and (
                    retry_after is None or retry_after <= self.max_retry_after_seconds):
                retried_server_error = True
                await self._backoff(path, f"HTTP {status}", 1, retry_after)
                continue
            if status in _REDIRECTS:
                raise RingPartnerError(f"Ring API POST {path} answered {status} without a Location", status=status)
            error = self._error(status, text, path, retry_after, method="POST")
            if isinstance(error, RingTimeRangeNotAuthorizedError):
                return "failed", "time_range_not_authorized"
            raise error

    @staticmethod
    async def _read_limited(response: aiohttp.ClientResponse, max_bytes: int) -> Optional[bytes]:
        """The body, or None when Content-Length or the bytes read exceed `max_bytes`."""
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            return None
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > max_bytes:
                return None
        return bytes(data)

    async def _get_presigned(self, session: aiohttp.ClientSession, url: str, max_bytes: int) -> tuple:
        """
        GET of a pre-signed media URL with no Authorization header (the token never goes there).
        Returns ("body", bytes, headers, 200) or ("failed", cause); the caller maps the causes
        "unreadable" and "too_large". The URL itself is never logged or returned.
        """
        if urlsplit(url).scheme not in ("http", "https"):
            return "failed", "unreadable"
        try:
            async with session.get(url, headers={"User-Agent": USER_AGENT}, allow_redirects=False) as response:
                if response.status != 200:
                    # Small JSON:API error body only.
                    body = (await response.content.read(SNAPSHOT_ERROR_MAX_BYTES)).decode("utf-8", "replace")
                    if response.status == 422:
                        return "failed", classify_media_error(body)
                    return "failed", "no_recent_media" if response.status == 416 else "unreadable"
                data = await self._read_limited(response, max_bytes)
                if data is None:
                    return "failed", "too_large"
                return "body", data, response.headers, 200
        except asyncio.TimeoutError:
            raise RingTimeoutError(f"Ring media download timed out after {self.timeout_seconds}s") from None
        except aiohttp.ClientError as exc:
            # The pre-signed URL is a credential too: it never appears in messages.
            raise RingPartnerError(f"Ring media download connection error ({type(exc).__name__})") from None

    async def _fetch_presigned(self, session: aiohttp.ClientSession, url: str) -> Dict[str, Any]:
        """Image from the pre-signed URL (see _get_presigned); oversized or undecodable bytes are unreadable."""
        outcome = await self._get_presigned(session, url, SNAPSHOT_MAX_BYTES)
        if outcome[0] == "failed":
            return _snapshot_result("unreadable_image" if outcome[1] in ("unreadable", "too_large") else outcome[1])
        _, data, headers, _status = outcome
        content_type = _image_type(data)
        if content_type is None:
            return _snapshot_result("unreadable_image")
        media_timestamp = headers.get("X-Media-Timestamp", "").strip()
        media_origin = headers.get("X-Media-Origin")
        return {**_snapshot_result("ok"), "image": data, "content_type": content_type,
                "media_timestamp": int(media_timestamp) if media_timestamp.isdigit() else None,
                "media_origin": media_origin if isinstance(media_origin, str) else None}

    async def list_events(self, device_id: str, event_types: str = DEFAULT_EVENT_TYPES,
                          max_pages: int = 2) -> List[Dict[str, Any]]:
        """
        Event History (polling alternative to webhooks), newest first, following `links.next` for at
        most `max_pages` pages. Ring returns only events after the user's consent, so [] is normal.
        """
        path = f"/v1/history/devices/{quote(device_id, safe='')}/events"
        self._device_path(device_id)  # same validation as the device routes
        events: List[Dict[str, Any]] = []
        params: Optional[Dict[str, str]] = {"event_types": event_types}
        for _ in range(max(1, max_pages)):
            document = await self._get_json(path, params)
            if not isinstance(document.get("data"), list):
                raise RingPartnerError(f"Ring API GET {path} returned no 'data' array")
            events += [normalize_event(item) for item in document["data"] if isinstance(item, dict)]
            next_link = (document.get("links") or {}).get("next") if isinstance(document.get("links"), dict) else None
            if not isinstance(next_link, str) or not next_link.startswith("/"):
                break  # no next page (or not the documented relative URL)
            path, params = next_link, None
        return events

    # -- internals ----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Hardware_Id": self.hardware_id,
        }

    @staticmethod
    def _device_path(device_id: str) -> str:
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must be a non-empty string.")
        # Device IDs are opaque; encode so they cannot alter the request path.
        return f"/v1/devices/{quote(device_id, safe='')}"

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "[redacted]") if self._token else text

    @staticmethod
    def _single_resource(document: Dict[str, Any], path: str) -> Dict[str, Any]:
        resource = document.get("data")
        if not isinstance(resource, dict):
            raise RingPartnerError(f"Ring API GET {path} returned no 'data' object")
        return resource

    def _error(self, status: int, body: str, path: str, retry_after: Optional[float],
               method: str = "GET") -> RingPartnerError:
        """Builds a typed error from the documented JSON:API format errors[{status, title, detail}]."""
        fields: Dict[str, Optional[str]] = {"code": None, "title": None, "detail": None}
        try:
            errors = json.loads(body).get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                for key in fields:
                    value = errors[0].get(key)
                    if value is not None:
                        fields[key] = self._redact(str(value))[:200]
        except (ValueError, AttributeError):
            pass  # non-JSON body (e.g. a proxy HTML page): keep only the HTTP status

        message = f"Ring API {method} {path} failed with HTTP {status}"
        time_range = status == 403 and fields["code"] == TIME_RANGE_NOT_AUTHORIZED
        if status == 406:
            message = f"{BLOCKED_406_TEXT} ({method} {path}, HTTP 406)"
        elif time_range:
            message = f"{TIME_RANGE_NOT_AUTHORIZED_TEXT} ({method} {path}, HTTP 403)"
        reason = " — ".join(v for v in (fields["title"], fields["detail"]) if v)
        if reason:
            message += f": {reason}"
        if fields["code"]:
            message += f" [{fields['code']}]"
        if retry_after is not None and status in (429, 503):
            message += f"; Retry-After {retry_after:.1f}s"
        message = self._redact(message)

        error_cls = RingTimeRangeNotAuthorizedError if time_range else _STATUS_ERRORS.get(status, RingPartnerError)
        if error_cls in (RingRateLimitError, RingServiceUnavailableError):
            return error_cls(message, retry_after=retry_after, status=status, **fields)
        return error_cls(message, status=status, **fields)

    async def _get_json(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if not self._token:
            raise RingNotConfiguredError(f"{TOKEN_ENV_VAR} is not set; Ring API is unavailable.")

        url = f"{self.base_url}{path}"
        headers = self._headers()
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        retries = 0
        transient_retries = 0

        async with aiohttp.ClientSession(timeout=timeout) as session:
            while True:
                try:
                    async with session.get(url, params=params, headers=headers) as response:
                        status = response.status
                        body = await response.text()
                        retry_after = parse_retry_after(response.headers.get("Retry-After"))
                        remaining = response.headers.get("X-RateLimit-Remaining", "").strip()
                        if remaining.isdigit():
                            self.rate_limit_remaining = int(remaining)
                except asyncio.TimeoutError:
                    # Not retried: a second full timeout would double the caller's wait.
                    raise RingTimeoutError(
                        f"Ring API GET {path} timed out after {self.timeout_seconds}s"
                    ) from None
                except aiohttp.ClientConnectionError as exc:
                    if transient_retries < self.max_transient_retries:
                        transient_retries += 1
                        await self._backoff(path, f"connection error ({type(exc).__name__})",
                                            transient_retries, None)
                        continue
                    raise RingPartnerError(
                        f"Ring API GET {path} connection error ({type(exc).__name__})"
                    ) from None
                except aiohttp.ClientError as exc:
                    raise RingPartnerError(
                        f"Ring API GET {path} connection error ({type(exc).__name__})"
                    ) from None

                if 200 <= status < 300:
                    try:
                        document = json.loads(body)
                    except ValueError:
                        raise RingPartnerError(f"Ring API GET {path} returned invalid JSON", status=status) from None
                    if not isinstance(document, dict):
                        raise RingPartnerError(f"Ring API GET {path} returned an unexpected document", status=status)
                    return document

                if (
                    status == 429
                    and retries < self.max_rate_limit_retries
                    and retry_after is not None
                    and retry_after <= self.max_retry_after_seconds
                ):
                    retries += 1
                    logger.warning(
                        "[RingPartnerClient] HTTP 429 on GET %s; retrying in %.1fs (%d/%d)",
                        path, retry_after, retries, self.max_rate_limit_retries,
                    )
                    await self._sleep(retry_after)
                    continue

                if (
                    status in TRANSIENT_STATUSES
                    and transient_retries < self.max_transient_retries
                    and (retry_after is None or retry_after <= self.max_retry_after_seconds)
                ):
                    transient_retries += 1
                    await self._backoff(path, f"HTTP {status}", transient_retries, retry_after)
                    continue

                raise self._error(status, body, path, retry_after)

    async def _backoff(self, path: str, reason: str, attempt: int, retry_after: Optional[float]) -> None:
        """Waits Retry-After when given, else exponential backoff from 1 s (no token in the log)."""
        delay = retry_after if retry_after is not None else TRANSIENT_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
        logger.warning(
            "[RingPartnerClient] %s on GET %s; retrying in %.1fs (%d/%d)",
            reason, path, delay, attempt, self.max_transient_retries,
        )
        await self._sleep(delay)
