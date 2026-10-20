"""
RingPartnerClient — Ring TelemetryOps Concierge Agent
======================================================
Read-only client for the official Ring API (JSON:API over HTTPS).

Endpoints used, as described in developer.amazon.com/docs/ring/api-documentation.html
(not re-verified against that page for this release):
  GET /v1/devices                            Device Discovery
  GET /v1/devices/{device_id}/status         Device Status
  GET /v1/devices/{device_id}/capabilities   Device Capabilities

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
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

RING_API_BASE_URL = "https://api.amazonvision.com"
TOKEN_ENV_VAR = "RING_PARTNER_ACCESS_TOKEN"
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


# Ring documents exponential backoff starting at 1 second for transient server errors. 503 is not
# retried: Ring documents it mostly as "device offline", which a quick retry does not change.
TRANSIENT_STATUSES = (500, 502, 504)
TRANSIENT_BACKOFF_BASE_SECONDS = 1.0

_STATUS_ERRORS = {
    401: RingUnauthorizedError,
    403: RingForbiddenError,
    404: RingNotFoundError,
    429: RingRateLimitError,
    503: RingServiceUnavailableError,
}


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
        base_url:                Ring API host (tests point this at a local server).
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
        base_url: str = RING_API_BASE_URL,
        timeout_seconds: float = 10.0,
        max_rate_limit_retries: int = 1,
        max_retry_after_seconds: float = 10.0,
        max_concurrency: int = 4,
        max_transient_retries: int = 1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_rate_limit_retries = max(0, int(max_rate_limit_retries))
        self.max_retry_after_seconds = max_retry_after_seconds
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_transient_retries = max(0, int(max_transient_retries))
        self._sleep = sleep
        self._token = os.getenv(TOKEN_ENV_VAR, "").strip()

    def __repr__(self) -> str:
        return f"RingPartnerClient(base_url={self.base_url!r}, configured={self.is_configured})"

    @property
    def is_configured(self) -> bool:
        return bool(self._token)

    # -- public API ---------------------------------------------------------

    async def list_devices(self) -> List[Dict[str, Any]]:
        """
        Device Discovery, then status and capabilities per device.

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

        async def collect(resource: Dict[str, Any]) -> Dict[str, Any]:
            results = await asyncio.gather(
                fetch(resource.get("id"), "status"),
                fetch(resource.get("id"), "capabilities"),
                return_exceptions=True,
            )
            found: Dict[str, Optional[Dict[str, Any]]] = {}
            errors: Dict[str, Dict[str, Any]] = {}
            for kind, result in zip(("status", "capabilities"), results):
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

    # -- internals ----------------------------------------------------------

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

    def _error(self, status: int, body: str, path: str, retry_after: Optional[float]) -> RingPartnerError:
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

        message = f"Ring API GET {path} failed with HTTP {status}"
        reason = " — ".join(v for v in (fields["title"], fields["detail"]) if v)
        if reason:
            message += f": {reason}"
        if fields["code"]:
            message += f" [{fields['code']}]"
        if retry_after is not None and status in (429, 503):
            message += f"; Retry-After {retry_after:.1f}s"
        message = self._redact(message)

        error_cls = _STATUS_ERRORS.get(status, RingPartnerError)
        if error_cls in (RingRateLimitError, RingServiceUnavailableError):
            return error_cls(message, retry_after=retry_after, status=status, **fields)
        return error_cls(message, status=status, **fields)

    async def _get_json(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if not self._token:
            raise RingNotConfiguredError(f"{TOKEN_ENV_VAR} is not set; Ring API is unavailable.")

        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
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
