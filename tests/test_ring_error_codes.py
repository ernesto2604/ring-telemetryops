"""T42: Ring 403 TIME_RANGE_NOT_AUTHORIZED is not a token error, and the 406 text names every documented cause.
Every Ring call goes to the local fake server (tests/fake_ring_server.py) or a test double."""
import asyncio

import pytest

import src.api.server as server
from src.utils.ring_partner_client import (
    BLOCKED_406_TEXT,
    TIME_RANGE_NOT_AUTHORIZED_TEXT,
    RingBlockedError,
    RingForbiddenError,
    RingPartnerClient,
    RingTimeRangeNotAuthorizedError,
)
from tests.fake_ring_server import DEVICE_ID
from tests.test_partner_snapshots_events import (  # noqa: F401  (fixtures)
    EVENT_STARTS, RING_SNAPSHOT_OK_FINDING, TOKEN, analyze, api, partner, ring, sleeps, snapshot_bodies, with_events,
)

TIME_RANGE = (403, None, "TIME_RANGE_NOT_AUTHORIZED")


def test_client_403_with_time_range_code_is_not_a_token_error(ring):
    ring.events_error = TIME_RANGE

    async def call():
        with pytest.raises(RingTimeRangeNotAuthorizedError) as info:
            await RingPartnerClient().list_events(DEVICE_ID)
        return info.value

    error = asyncio.run(call())
    assert not isinstance(error, RingForbiddenError)
    assert error.status == 403 and error.code == "TIME_RANGE_NOT_AUTHORIZED"
    assert TIME_RANGE_NOT_AUTHORIZED_TEXT in str(error) and TOKEN not in str(error)


def test_client_403_without_code_stays_forbidden(ring):
    ring.events_error = (403, None)

    async def call():
        with pytest.raises(RingForbiddenError):
            await RingPartnerClient().list_events(DEVICE_ID)

    asyncio.run(call())


def test_client_snapshot_403_time_range_is_a_result_not_an_error(ring):
    ring.snapshot_replies = [TIME_RANGE]
    result = asyncio.run(RingPartnerClient().download_snapshot(DEVICE_ID, at_timestamp_ms=1))
    assert result["status"] == "time_range_not_authorized" and result["image"] is None


def test_client_406_text_names_every_cause(ring):
    ring.snapshot_replies = [(406, None, None)]

    async def call():
        with pytest.raises(RingBlockedError) as info:
            await RingPartnerClient().download_snapshot(DEVICE_ID)
        return info.value

    message = str(asyncio.run(call()))
    assert message.startswith(BLOCKED_406_TEXT)
    assert "Ring Security blocked this IP (not retryable; contact Ring developer support)" in message
    assert "http" not in message.lower().split("(post")[0]   # no URL in the text


def test_analyze_now_skips_an_event_before_consent(with_events):
    with_events.snapshot_replies = [TIME_RANGE, None]

    response = analyze()

    assert response.status_code == 200
    assert response.json()["diagnostic_result"]["visual_findings"] == [RING_SNAPSHOT_OK_FINDING]
    assert [b.get("timestamp") for b in snapshot_bodies(with_events)] == EVENT_STARTS[:2]


def test_analyze_now_every_candidate_before_consent_explains_it(with_events):
    with_events.snapshot_replies = [TIME_RANGE]

    response = analyze()

    assert response.status_code == 200
    body = response.json()
    assert body["diagnostic_result"]["visual_findings"] == [TIME_RANGE_NOT_AUTHORIZED_TEXT]
    assert [b["type"] for b in snapshot_bodies(with_events)] == ["at_timestamp"] * 3 + ["latest_in_range"]
    assert "token" not in response.text.lower()


def test_analyze_now_event_history_time_range_falls_back_to_latest_in_range(partner):
    ring, _ = partner
    ring.events_error = TIME_RANGE

    response = analyze()

    assert response.status_code == 200
    assert [b["type"] for b in snapshot_bodies(ring)] == ["latest_in_range"]


def test_analyze_now_403_without_code_is_still_token_rejected(with_events):
    with_events.snapshot_replies = [(403, None, None), None]

    response = analyze()

    assert response.status_code == 502 and response.json()["detail"]["reason"] == "token_rejected"
    assert len(snapshot_bodies(with_events)) == 1


@pytest.mark.parametrize("error, reason", [
    (RingTimeRangeNotAuthorizedError(TIME_RANGE_NOT_AUTHORIZED_TEXT, status=403), "time_range_not_authorized"),
    (RingForbiddenError("HTTP 403", status=403), "token_rejected"),
    (RingBlockedError(BLOCKED_406_TEXT, status=406), "blocked_406"),
])
def test_api_reason_per_error(error, reason):
    assert server._partner_error_reason(error) == reason


def test_events_endpoint_reports_time_range_reason(partner, monkeypatch):
    ring, _ = partner
    ring.events_error = TIME_RANGE
    monkeypatch.setattr(server, "_events_poll", {"last_query": None, "paused_until": 0.0, "device_id": None})

    response = api.get("/api/ring/events")

    detail = response.json()["detail"]
    assert detail["reason"] == "time_range_not_authorized"
    assert TIME_RANGE_NOT_AUTHORIZED_TEXT in detail["message"] and "http://" not in detail["message"]
