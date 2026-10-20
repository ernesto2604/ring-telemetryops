import asyncio
import email.utils
import json
import logging
import socket
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp import test_utils

from src.utils.ring_partner_client import (
    NOT_PROVIDED_BY_API,
    RingForbiddenError,
    RingNotConfiguredError,
    RingNotFoundError,
    RingPartnerClient,
    RingPartnerError,
    RingRateLimitError,
    RingServiceUnavailableError,
    RingTimeoutError,
    RingUnauthorizedError,
    build_device_health,
    parse_retry_after,
)

FAKE_TOKEN = "fake-test-token-0123456789"

ONLINE_ID = "ava1.ring.device.ONLINE1"
OFFLINE_ID = "ava1.ring.device.OFFLINE1"

# Fictitious payloads shaped like the documented JSON:API examples.
DISCOVERY_DOCUMENT = {
    "meta": {"time": "2026-02-14T22:57:16Z"},
    "data": [
        {"type": "devices", "id": ONLINE_ID, "attributes": {"name": "Front Door"}},
        {"type": "devices", "id": OFFLINE_ID, "attributes": {"name": "Back Yard"}},
    ],
}

STATUS_ATTRIBUTES = {
    ONLINE_ID: {"online": True},
    # Placeholder values on an offline device must not be trusted.
    OFFLINE_ID: {"online": False, "reported_at": "1970-01-01T00:00:00Z", "battery_status": {"percentage": 80}},
}

CAPABILITY_ATTRIBUTES = {
    ONLINE_ID: {
        "video": {"codecs": ["HEVC"], "ratio": "16:9", "max_resolution": 2160, "supported_resolutions": [2160]},
        "motion_detection": {"configurations": ["enabled", "motion_zones"]},
    },
    OFFLINE_ID: {"battery_status": {"supported": True}},
}


def resource_handler(resource_type, attributes):
    async def handler(_request):
        return web.json_response({"data": {"type": resource_type, "id": "x", "attributes": attributes}})
    return handler


def error_handler(status, **kwargs):
    async def handler(_request):
        return jsonapi_error(status, **kwargs)
    return handler


def discovery_routes(document=DISCOVERY_DOCUMENT, statuses=STATUS_ATTRIBUTES, capabilities=CAPABILITY_ATTRIBUTES):
    """Discovery plus per-device status/capabilities routes (overridable per test)."""
    async def devices(_request):
        return web.json_response(document)

    routes = {"/v1/devices": devices}
    for device_id, attrs in statuses.items():
        routes[f"/v1/devices/{device_id}/status"] = resource_handler("device-status", attrs)
    for device_id, attrs in capabilities.items():
        routes[f"/v1/devices/{device_id}/capabilities"] = resource_handler("device-capabilities", attrs)
    return routes


CAPABILITIES_DOCUMENT = {
    "meta": {"time": "2025-07-07T10:30:00Z"},
    "data": {
        "id": "xxxyyy.capabilities",
        "type": "device-capabilities",
        "attributes": {
            "video": {"codecs": ["AVC"], "ratio": "16:9", "max_resolution": 1080},
            "image_enhancements": {"configurations": ["hdr", "privacy_zones"]},
        },
    },
}


def jsonapi_error(status, title=None, detail=None, code=None, headers=None):
    """Documented error format: errors[{status, title, detail}]; `code` only if given."""
    error = {"status": str(status)}
    for key, value in (("title", title), ("detail", detail), ("code", code)):
        if value is not None:
            error[key] = value
    return web.json_response({"errors": [error]}, status=status, headers=headers)


def free_local_port():
    """A localhost port with nothing listening (connection will be refused)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_against(routes, client_call, **client_kwargs):
    """Runs `client_call(client)` against a local aiohttp server on 127.0.0.1."""
    requests = []

    async def main():
        app = web.Application()

        @web.middleware
        async def record(request, handler):
            requests.append(request)
            return await handler(request)

        app.middlewares.append(record)
        for path, handler in routes.items():
            app.router.add_get(path, handler)
        server = test_utils.TestServer(app)
        await server.start_server()
        try:
            client = RingPartnerClient(base_url=str(server.make_url("")), **client_kwargs)
            return await client_call(client)
        finally:
            await server.close()

    return asyncio.run(main()), requests


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setenv("RING_PARTNER_ACCESS_TOKEN", FAKE_TOKEN)


# ---------------------------------------------------------------------------
# Device Discovery / Status / Capabilities
# ---------------------------------------------------------------------------

def test_discovery_sends_bearer_without_include_and_normalizes_devices():
    result, requests = run_against(discovery_routes(), lambda c: c.list_devices())

    discovery = next(r for r in requests if r.path == "/v1/devices")
    assert "include" not in discovery.query
    assert all(r.headers["Authorization"] == f"Bearer {FAKE_TOKEN}" for r in requests)
    assert {r.path for r in requests} == {
        "/v1/devices",
        f"/v1/devices/{ONLINE_ID}/status", f"/v1/devices/{ONLINE_ID}/capabilities",
        f"/v1/devices/{OFFLINE_ID}/status", f"/v1/devices/{OFFLINE_ID}/capabilities",
    }

    assert [d["device_id"] for d in result] == [ONLINE_ID, OFFLINE_ID]  # discovery order kept
    online = result[0]
    assert online["source"] == "ring_partner_api"
    assert online["name"] == "Front Door"
    assert online["device_health"]["online"] is True
    assert online["capabilities"]["video"]["max_resolution"] == 2160
    assert online["device_health"]["measurement_status"]["battery_percentage"] == "not_supported"
    assert online["fetch_errors"] == {}
    assert FAKE_TOKEN not in json.dumps(result)


def test_offline_device_stays_offline_without_invented_metrics():
    result, _ = run_against(discovery_routes(), lambda c: c.list_devices())
    offline = next(d for d in result if d["device_id"] == OFFLINE_ID)["device_health"]

    assert offline["online"] is False
    assert offline["battery_percentage"] is None  # placeholder 80 ignored while offline
    assert offline["reported_at"] is None
    assert offline["rssi"] is None
    assert offline["transformer_voltage_vac"] is None
    assert offline["measurement_status"]["rssi"] == NOT_PROVIDED_BY_API
    assert offline["measurement_status"]["transformer_voltage_vac"] == NOT_PROVIDED_BY_API


def test_get_status_reports_offline_device():
    async def status(_request):
        return web.json_response(
            {"data": {"type": "device-status", "id": "x.status", "attributes": {"online": False}}}
        )

    result, requests = run_against(
        {f"/v1/devices/{OFFLINE_ID}/status": status}, lambda c: c.get_status(OFFLINE_ID)
    )

    assert requests[0].path == f"/v1/devices/{OFFLINE_ID}/status"
    assert result["device_id"] == OFFLINE_ID
    assert result["online"] is False
    assert result["battery_percentage"] is None
    assert result["measurement_status"]["battery_percentage"] == "capabilities_unknown"


def test_get_capabilities_passes_documented_attributes_through():
    async def capabilities(_request):
        return web.json_response(CAPABILITIES_DOCUMENT)

    result, _ = run_against(
        {f"/v1/devices/{ONLINE_ID}/capabilities": capabilities}, lambda c: c.get_capabilities(ONLINE_ID)
    )

    assert result == {"device_id": ONLINE_ID, "capabilities": CAPABILITIES_DOCUMENT["data"]["attributes"]}


def test_discovery_with_empty_data_returns_no_devices():
    async def devices(_request):
        return web.json_response({"meta": {"time": "2026-02-14T22:57:16Z"}, "data": []})

    result, requests = run_against({"/v1/devices": devices}, lambda c: c.list_devices())

    assert result == []
    assert [r.path for r in requests] == ["/v1/devices"]


def test_non_device_resources_are_skipped():
    document = {
        "data": [
            {"type": "devices", "id": ONLINE_ID, "attributes": {"name": "Front Door"}},
            {"type": "locations", "id": "not-a-device", "attributes": {}},
        ],
    }

    result, requests = run_against(discovery_routes(document=document), lambda c: c.list_devices())

    assert [d["device_id"] for d in result] == [ONLINE_ID]
    assert not any("not-a-device" in r.path for r in requests)


def test_multi_camera_components_are_preserved():
    components = {
        "items": [
            {"component_id": "1", "component_type": "lens", "component_name": "View 1"},
            {"component_id": "2", "component_type": "lens", "component_name": "View 2"},
        ]
    }
    routes = discovery_routes(
        document={"data": [{"type": "devices", "id": ONLINE_ID, "attributes": {}}]},
        statuses={ONLINE_ID: {"online": True}},
        capabilities={ONLINE_ID: {"video": {"codecs": ["AVC"]}, "components": components}},
    )
    listed, _ = run_against(routes, lambda c: c.list_devices())
    single, _ = run_against(routes, lambda c: c.get_capabilities(ONLINE_ID))

    assert listed[0]["capabilities"]["components"] == components
    assert single["capabilities"]["components"] == components


# ---------------------------------------------------------------------------
# list_devices: partial failures and bounded concurrency
# ---------------------------------------------------------------------------

def test_status_failure_on_one_device_is_reported_not_invented():
    routes = discovery_routes()
    routes[f"/v1/devices/{OFFLINE_ID}/status"] = error_handler(
        503, title="Device Unavailable", detail="Device is currently offline")

    result, _ = run_against(routes, lambda c: c.list_devices())
    healthy, failed = result

    assert healthy["device_health"]["online"] is True
    assert healthy["fetch_errors"] == {}

    health = failed["device_health"]
    assert health["online"] is None  # a 503 is never translated into online=False
    assert health["battery_percentage"] is None
    assert health["measurement_status"]["online"] == "fetch_failed"
    assert health["measurement_status"]["battery_percentage"] == "fetch_failed"
    assert failed["capabilities"] == CAPABILITY_ATTRIBUTES[OFFLINE_ID]  # the successful half is kept
    error = failed["fetch_errors"]["status"]
    assert error["error"] == "RingServiceUnavailableError"
    assert error["status"] == 503
    assert "Device is currently offline" in error["message"]
    assert "capabilities" not in failed["fetch_errors"]


def test_capabilities_failure_leaves_capabilities_unknown():
    routes = discovery_routes(capabilities={OFFLINE_ID: CAPABILITY_ATTRIBUTES[OFFLINE_ID]})  # ONLINE_ID -> 404

    result, _ = run_against(routes, lambda c: c.list_devices())
    device = result[0]

    assert device["capabilities"] is None
    assert device["device_health"]["online"] is True
    assert device["device_health"]["measurement_status"]["battery_percentage"] == "capabilities_unknown"
    assert device["fetch_errors"]["capabilities"]["error"] == "RingNotFoundError"
    assert device["fetch_errors"]["capabilities"]["status"] == 404


def test_per_device_timeout_is_recorded_as_fetch_error():
    async def slow(_request):
        await asyncio.sleep(1.0)
        return web.json_response({"data": {"type": "device-status", "id": "x", "attributes": {"online": True}}})

    routes = discovery_routes()
    routes[f"/v1/devices/{ONLINE_ID}/status"] = slow

    result, _ = run_against(routes, lambda c: c.list_devices(), timeout_seconds=0.3)

    assert result[0]["device_health"]["online"] is None
    assert result[0]["fetch_errors"]["status"]["error"] == "RingTimeoutError"
    assert result[1]["device_health"]["online"] is False


def test_invalid_device_id_is_recorded_without_requests():
    document = {"data": [{"type": "devices", "attributes": {"name": "No id"}}]}

    result, requests = run_against(discovery_routes(document=document), lambda c: c.list_devices())

    assert [r.path for r in requests] == ["/v1/devices"]
    assert result[0]["device_id"] is None
    assert result[0]["device_health"]["online"] is None
    assert set(result[0]["fetch_errors"]) == {"status", "capabilities"}
    assert result[0]["fetch_errors"]["status"]["error"] == "ValueError"


def test_unauthorized_per_device_request_aborts_listing():
    routes = discovery_routes()
    routes[f"/v1/devices/{OFFLINE_ID}/capabilities"] = error_handler(401, title="Invalid Client")

    with pytest.raises(RingUnauthorizedError):
        run_against(routes, lambda c: c.list_devices())


def test_per_device_errors_never_leak_token():
    routes = discovery_routes()
    routes[f"/v1/devices/{ONLINE_ID}/status"] = error_handler(
        403, title="Out of Scope Access", detail=f"token {FAKE_TOKEN}")

    result, _ = run_against(routes, lambda c: c.list_devices())

    assert result[0]["fetch_errors"]["status"]["error"] == "RingForbiddenError"
    assert FAKE_TOKEN not in json.dumps(result)


def test_per_device_requests_respect_max_concurrency():
    device_ids = [f"ava1.ring.device.D{i}" for i in range(6)]
    state = {"in_flight": 0, "peak": 0}

    def tracked(resource_type):
        async def handler(_request):
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            await asyncio.sleep(0.05)
            state["in_flight"] -= 1
            return web.json_response({"data": {"type": resource_type, "id": "x", "attributes": {"online": True}}})
        return handler

    async def devices(_request):
        return web.json_response({"data": [{"type": "devices", "id": d, "attributes": {}} for d in device_ids]})

    routes = {"/v1/devices": devices}
    for d in device_ids:
        routes[f"/v1/devices/{d}/status"] = tracked("device-status")
        routes[f"/v1/devices/{d}/capabilities"] = tracked("device-capabilities")

    result, _ = run_against(routes, lambda c: c.list_devices(), max_concurrency=2)

    assert [d["device_id"] for d in result] == device_ids
    assert all(d["fetch_errors"] == {} for d in result)
    assert 1 <= state["peak"] <= 2


def test_missing_status_attribute_is_not_coerced():
    health = build_device_health({}, None)

    assert health["online"] is None
    assert health["measurement_status"]["online"] == "not_reported"


@pytest.mark.parametrize(
    "online, capabilities, percentage, expected",
    [
        (True, {"battery_status": {"supported": True}}, 85, 85),
        (True, {"battery_status": None}, 85, None),          # wired device
        (True, {"battery_status": {"supported": True}}, 255, None),  # documented "no battery" value
        (False, {"battery_status": {"supported": True}}, 85, None),  # gate on online
        (True, None, 85, None),                               # capabilities unknown
        (True, {"battery_status": {"supported": True}}, True, None),
    ],
)
def test_battery_is_only_reported_when_documented_conditions_hold(online, capabilities, percentage, expected):
    health = build_device_health({"online": online, "battery_status": {"percentage": percentage}}, capabilities)

    assert health["battery_percentage"] == expected


def test_device_id_is_path_encoded():
    assert RingPartnerClient._device_path("a/b?c") == "/v1/devices/a%2Fb%3Fc"
    with pytest.raises(ValueError):
        RingPartnerClient._device_path("")


# ---------------------------------------------------------------------------
# Errors, timeout, rate limiting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status, title, detail, error_cls",
    [
        # Documented examples (Error Handling section): no `code` field.
        (401, "Invalid Client", None, RingUnauthorizedError),
        (403, "Out of Scope Access", None, RingForbiddenError),
        (404, "Not Found", "The requested resource could not be found", RingNotFoundError),
    ],
)
def test_documented_errors_keep_title_and_detail(status, title, detail, error_cls):
    async def failing(_request):
        return jsonapi_error(status, title=title, detail=detail)

    with pytest.raises(error_cls) as excinfo:
        run_against({"/v1/devices": failing}, lambda c: c.list_devices())

    error = excinfo.value
    assert error.status == status
    assert error.title == title
    assert error.detail == detail
    assert error.code is None
    assert title in str(error)
    if detail:
        assert detail in str(error)


def test_optional_code_field_is_kept_when_present():
    async def failing(_request):
        return jsonapi_error(404, detail=f"Unknown device provided: {ONLINE_ID}", code="DEVICE_NOT_FOUND")

    with pytest.raises(RingNotFoundError) as excinfo:
        run_against({f"/v1/devices/{ONLINE_ID}/status": failing}, lambda c: c.get_status(ONLINE_ID))

    assert excinfo.value.code == "DEVICE_NOT_FOUND"
    assert "DEVICE_NOT_FOUND" in str(excinfo.value)
    assert ONLINE_ID in excinfo.value.detail


def test_errors_never_leak_token_in_message_fields_or_logs(caplog):
    async def failing(_request):
        return jsonapi_error(401, title=f"echo {FAKE_TOKEN}", detail=f"Bearer {FAKE_TOKEN}")

    caplog.set_level(logging.DEBUG)
    with pytest.raises(RingUnauthorizedError) as excinfo:
        run_against({"/v1/devices": failing}, lambda c: c.list_devices())

    error = excinfo.value
    for text in (str(error), error.title, error.detail, caplog.text):
        assert FAKE_TOKEN not in text
    assert "[redacted]" in error.title


@pytest.mark.parametrize(
    "error_kwargs",
    [
        {"title": "Device Unavailable", "detail": "Device is currently offline"},
        {"title": "Service Unavailable", "detail": "Device is currently offline"},
        {"detail": "The server is too busy to handle the request; please try again", "code": "SERVER_BUSY"},
    ],
)
def test_503_is_typed_preserves_reason_and_is_not_retried(error_kwargs):
    calls = {"n": 0}
    sleeps = []

    async def unavailable(_request):
        calls["n"] += 1
        return jsonapi_error(503, headers={"Retry-After": "1"}, **error_kwargs)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    with pytest.raises(RingServiceUnavailableError) as excinfo:
        run_against({f"/v1/devices/{ONLINE_ID}/status": unavailable}, lambda c: c.get_status(ONLINE_ID),
                    sleep=fake_sleep)

    error = excinfo.value
    assert isinstance(error, RingPartnerError)
    assert error.status == 503
    assert error.detail == error_kwargs["detail"]
    assert error.title == error_kwargs.get("title")
    assert error.retry_after == 1.0
    assert calls["n"] == 1 and sleeps == []


def test_503_is_never_translated_into_offline_status():
    async def unavailable(_request):
        return jsonapi_error(503, title="Device Unavailable", detail="Device is currently offline")

    outcome = {}

    async def call(client):
        try:
            outcome["result"] = await client.get_status(ONLINE_ID)
        except RingServiceUnavailableError as exc:
            outcome["error"] = exc

    run_against({f"/v1/devices/{ONLINE_ID}/status": unavailable}, call)

    assert "result" not in outcome  # no {"online": False} payload is produced
    assert not hasattr(outcome["error"], "online")


def test_503_during_discovery_returns_no_partial_device_list():
    async def unavailable(_request):
        return jsonapi_error(503, title="Service Unavailable")

    with pytest.raises(RingServiceUnavailableError):
        run_against({"/v1/devices": unavailable}, lambda c: c.list_devices())


def test_non_json_error_body_keeps_only_http_status():
    async def proxy_page(_request):
        return web.Response(status=502, text="<html><body>Bad Gateway</body></html>", content_type="text/html")

    with pytest.raises(RingPartnerError) as excinfo:
        run_against({"/v1/devices": proxy_page}, lambda c: c.list_devices())

    error = excinfo.value
    assert type(error) is RingPartnerError
    assert error.status == 502
    assert error.title is None and error.detail is None
    assert "<html>" not in str(error)


@pytest.mark.parametrize(
    "body, message",
    [
        ("{not json", "invalid JSON"),
        ("[]", "unexpected document"),
        ('"text"', "unexpected document"),
    ],
)
def test_2xx_with_invalid_or_unexpected_json_raises(body, message):
    async def broken(_request):
        return web.Response(status=200, text=body, content_type="application/json")

    with pytest.raises(RingPartnerError, match=message):
        run_against({"/v1/devices": broken}, lambda c: c.list_devices())


@pytest.mark.parametrize("document", [{}, {"data": {"type": "devices"}}, {"data": None}])
def test_discovery_without_data_array_raises(document):
    async def devices(_request):
        return web.json_response(document)

    with pytest.raises(RingPartnerError, match="no 'data' array"):
        run_against({"/v1/devices": devices}, lambda c: c.list_devices())


@pytest.mark.parametrize("document", [{}, {"data": []}, {"data": None}])
def test_status_and_capabilities_without_data_object_raise(document):
    async def handler(_request):
        return web.json_response(document)

    routes = {
        f"/v1/devices/{ONLINE_ID}/status": handler,
        f"/v1/devices/{ONLINE_ID}/capabilities": handler,
    }
    with pytest.raises(RingPartnerError, match="no 'data' object"):
        run_against(routes, lambda c: c.get_status(ONLINE_ID))
    with pytest.raises(RingPartnerError, match="no 'data' object"):
        run_against(routes, lambda c: c.get_capabilities(ONLINE_ID))


def test_connection_refused_raises_typed_error():
    # Windows retries SYN to a closed local port for ~2s before refusing; keep timeout above that.
    client = RingPartnerClient(base_url=f"http://127.0.0.1:{free_local_port()}", timeout_seconds=10.0)

    with pytest.raises(RingPartnerError, match="connection error") as excinfo:
        asyncio.run(client.list_devices())

    assert not isinstance(excinfo.value, RingTimeoutError)
    assert FAKE_TOKEN not in str(excinfo.value)


def test_429_waits_for_retry_after_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    async def devices(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return web.json_response({"errors": []}, status=429, headers={"Retry-After": "2"})
        return web.json_response(DISCOVERY_DOCUMENT)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    result, _ = run_against({**discovery_routes(), "/v1/devices": devices}, lambda c: c.list_devices(),
                            sleep=fake_sleep)

    assert len(result) == 2
    assert calls["n"] == 2
    assert sleeps == [2.0]


def test_429_retries_are_bounded():
    calls = {"n": 0}
    sleeps = []

    async def devices(_request):
        calls["n"] += 1
        return web.json_response({"errors": []}, status=429, headers={"Retry-After": "1"})

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    with pytest.raises(RingRateLimitError) as excinfo:
        run_against(
            {"/v1/devices": devices}, lambda c: c.list_devices(), sleep=fake_sleep, max_rate_limit_retries=1
        )

    assert calls["n"] == 2
    assert sleeps == [1.0]
    assert excinfo.value.retry_after == 1.0


def test_429_retry_after_http_date_is_honoured_by_client():
    calls = {"n": 0}
    sleeps = []

    async def devices(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            when = datetime.now(timezone.utc) + timedelta(seconds=5)
            return jsonapi_error(429, title="Rate Limit Exceeded",
                                 headers={"Retry-After": email.utils.format_datetime(when, usegmt=True)})
        return web.json_response(DISCOVERY_DOCUMENT)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    result, _ = run_against({**discovery_routes(), "/v1/devices": devices}, lambda c: c.list_devices(),
                            sleep=fake_sleep)

    assert len(result) == 2
    assert calls["n"] == 2
    assert len(sleeps) == 1 and 0.0 <= sleeps[0] <= 5.0  # fake sleep: no real wait


@pytest.mark.parametrize("headers", [{"Retry-After": "3600"}, {}])
def test_429_with_long_or_missing_retry_after_is_raised_without_waiting(headers):
    calls = {"n": 0}
    sleeps = []

    async def devices(_request):
        calls["n"] += 1
        return web.json_response({"errors": []}, status=429, headers=headers)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    with pytest.raises(RingRateLimitError) as excinfo:
        run_against({"/v1/devices": devices}, lambda c: c.list_devices(), sleep=fake_sleep)

    assert calls["n"] == 1
    assert sleeps == []
    assert excinfo.value.retry_after == (3600.0 if headers else None)


def test_timeout_raises_typed_error():
    async def slow(_request):
        await asyncio.sleep(1.0)
        return web.json_response(DISCOVERY_DOCUMENT)

    with pytest.raises(RingTimeoutError):
        run_against({"/v1/devices": slow}, lambda c: c.list_devices(), timeout_seconds=0.2)


def test_missing_token_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("RING_PARTNER_ACCESS_TOKEN")
    # Unroutable local port: if the guard were missing, nothing external is contacted.
    client = RingPartnerClient(base_url="http://127.0.0.1:9")

    assert client.is_configured is False
    with pytest.raises(RingNotConfiguredError):
        asyncio.run(client.list_devices())


def test_repr_does_not_expose_token():
    client = RingPartnerClient()

    assert FAKE_TOKEN not in repr(client)
    assert "configured=True" in repr(client)


def test_parse_retry_after_supports_seconds_and_http_dates():
    now = datetime.now(timezone.utc)
    future = email.utils.format_datetime(now + timedelta(seconds=30), usegmt=True)

    assert parse_retry_after("5") == 5.0
    assert 25.0 <= parse_retry_after(future, now=now.timestamp()) <= 30.0
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("inf") is None
    assert parse_retry_after(None) is None


# ---------------------------------------------------------------------------
# RING-020: bounded retry of transient server and connection errors
# ---------------------------------------------------------------------------

def flaky_devices(responses):
    """Serves `responses` in order for GET /v1/devices (the last one repeats)."""
    calls = {"n": 0}

    async def devices(_request):
        calls["n"] += 1
        return responses[min(calls["n"], len(responses)) - 1]()

    return devices, calls


def recording_sleep():
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    return fake_sleep, sleeps


@pytest.mark.parametrize("status", [500, 502, 504])
def test_transient_5xx_is_retried_once_then_succeeds(status):
    devices, calls = flaky_devices([lambda: jsonapi_error(status, title="Internal"),
                                    lambda: web.json_response(DISCOVERY_DOCUMENT)])
    fake_sleep, sleeps = recording_sleep()

    result, _ = run_against({**discovery_routes(), "/v1/devices": devices}, lambda c: c.list_devices(),
                            sleep=fake_sleep)

    assert len(result) == 2
    assert calls["n"] == 2
    assert sleeps == [1.0]   # documented backoff start: 1 second


def test_persistent_500_is_raised_after_bounded_retries():
    devices, calls = flaky_devices([lambda: jsonapi_error(500, title="Internal Server Error")])
    fake_sleep, sleeps = recording_sleep()

    with pytest.raises(RingPartnerError) as excinfo:
        run_against({"/v1/devices": devices}, lambda c: c.list_devices(), sleep=fake_sleep,
                    max_transient_retries=2)

    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]   # exponential
    assert excinfo.value.status == 500
    assert FAKE_TOKEN not in str(excinfo.value)


def test_transient_retry_honours_retry_after_and_its_cap():
    devices, calls = flaky_devices([lambda: jsonapi_error(502, headers={"Retry-After": "3"}),
                                    lambda: web.json_response(DISCOVERY_DOCUMENT)])
    fake_sleep, sleeps = recording_sleep()
    run_against({**discovery_routes(), "/v1/devices": devices}, lambda c: c.list_devices(), sleep=fake_sleep)
    assert sleeps == [3.0] and calls["n"] == 2

    devices, calls = flaky_devices([lambda: jsonapi_error(502, headers={"Retry-After": "120"})])
    fake_sleep, sleeps = recording_sleep()
    with pytest.raises(RingPartnerError):
        run_against({"/v1/devices": devices}, lambda c: c.list_devices(), sleep=fake_sleep)
    assert sleeps == [] and calls["n"] == 1   # longer than max_retry_after_seconds: raised, not waited


@pytest.mark.parametrize("status", [400, 401, 403, 404, 503])
def test_non_transient_errors_are_not_retried(status):
    devices, calls = flaky_devices([lambda: jsonapi_error(status, title="Error")])
    fake_sleep, sleeps = recording_sleep()

    with pytest.raises(RingPartnerError):
        run_against({"/v1/devices": devices}, lambda c: c.list_devices(), sleep=fake_sleep)

    assert calls["n"] == 1 and sleeps == []


def test_retries_can_be_disabled():
    devices, calls = flaky_devices([lambda: jsonapi_error(500)])
    fake_sleep, sleeps = recording_sleep()

    with pytest.raises(RingPartnerError):
        run_against({"/v1/devices": devices}, lambda c: c.list_devices(), sleep=fake_sleep,
                    max_transient_retries=0)

    assert calls["n"] == 1 and sleeps == []


def test_connection_error_is_retried_once_then_raised_without_token():
    fake_sleep, sleeps = recording_sleep()
    client = RingPartnerClient(base_url=f"http://127.0.0.1:{free_local_port()}", timeout_seconds=10.0,
                               sleep=fake_sleep)

    with pytest.raises(RingPartnerError, match="connection error") as excinfo:
        asyncio.run(client.list_devices())

    assert sleeps == [1.0]
    assert not isinstance(excinfo.value, RingTimeoutError)
    assert FAKE_TOKEN not in str(excinfo.value)


def test_timeout_is_not_retried():
    async def slow(_request):
        await asyncio.sleep(1.0)
        return web.json_response(DISCOVERY_DOCUMENT)

    fake_sleep, sleeps = recording_sleep()
    with pytest.raises(RingTimeoutError):
        run_against({"/v1/devices": slow}, lambda c: c.list_devices(), sleep=fake_sleep, timeout_seconds=0.2)
    assert sleeps == []
