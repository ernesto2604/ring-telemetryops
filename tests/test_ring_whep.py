"""T46: Ring live view (WHEP) with this server as the SDP intermediary. Ring is the local fake server
(127.0.0.1); no real Ring call, no real token. The token, Ring's session URL and the SDP must never reach
a response, stdout or the logs (the SDP answer is returned to the browser by design)."""
import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from src.api import whep
from src.api.funnel import funnel
from src.utils.ring_partner_client import RingPartnerError
from tests.fake_ring_server import DEVICE_ID, WHEP_ANSWER, WHEP_SESSION_PATH, FakeRing
from tests.test_partner_snapshots_events import TOKEN, partner, ring, sleeps  # noqa: F401  (fixtures)

WHEP_POST = f"/v1/devices/{DEVICE_ID}/media/streaming/whep/sessions"
OFFER = ("v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
         "m=video 9 UDP/TLS/RTP/SAVPF 96\r\na=recvonly\r\na=rtpmap:96 H264/90000\r\n"
         "a=candidate:1 1 udp 2122260223 192.0.2.10 54400 typ host\r\n")


@pytest.fixture
def api(partner, monkeypatch):
    """Partner mode against the fake Ring, one event loop for the whole test (so the 65 s timer lives)."""
    funnel.reset()
    sessions = whep.WhepSessions()
    monkeypatch.setattr(whep, "sessions", sessions)
    with TestClient(server.app) as client:
        yield client
    funnel.reset()


def open_session(api, sdp=OFFER):
    return api.post("/api/ring/whep/session", json={"sdp": sdp})


def deletes(ring):
    return [r for r in ring.requests if r["method"] == "DELETE"]


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


# -- valid offer --------------------------------------------------------------

def test_valid_offer_is_forwarded_as_sdp_and_only_the_answer_comes_back(api, partner):
    ring, _ = partner
    response = open_session(api)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"source", "session_id", "sdp_answer", "max_seconds_hint"}
    assert len(body["session_id"]) == 32 and int(body["session_id"], 16) >= 0   # 128 random bits
    assert body["sdp_answer"] == WHEP_ANSWER and body["max_seconds_hint"] in (30, 60)
    post = next(r for r in ring.requests if r["path"] == WHEP_POST)
    assert post["method"] == "POST" and post["text"] == OFFER
    assert post["headers"]["Content-Type"] == "application/sdp"
    assert post["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert post["headers"]["User-Agent"].startswith("RingTelemetryOps/") and post["headers"]["Hardware_Id"]
    assert post["query"] == {}
    assert TOKEN not in response.text and WHEP_SESSION_PATH not in response.text and ring.url not in response.text
    assert funnel.snapshot()["whep_started"] == 1


def test_multi_camera_device_sends_the_component_id(api, partner):
    ring, _ = partner
    ring.capabilities = {**ring.capabilities, "components": {"items": [{"component_id": "cam-left"}]}}

    assert open_session(api).status_code == 200
    assert next(r for r in ring.requests if r["path"] == WHEP_POST)["query"] == {"component_id": "cam-left"}


def test_wired_device_gets_the_60_second_hint():
    wired = {"device_health": {"measurement_status": {"battery_percentage": "not_supported"}}}
    battery = {"device_health": {"measurement_status": {"battery_percentage": "reported_by_api"}}}
    assert whep.max_seconds_hint(wired) == 60 and whep.max_seconds_hint(battery) == 30
    assert whep.max_seconds_hint({}) == 30


# -- offer validation -----------------------------------------------------------

@pytest.mark.parametrize("sdp, words", [
    (OFFER.replace("a=recvonly", "a=sendrecv"), "receive-only"),
    (OFFER.replace("a=recvonly", "a=sendonly"), "receive-only"),
    (OFFER.replace("m=video 9 UDP/TLS/RTP/SAVPF 96", "m=audio 9 UDP/TLS/RTP/SAVPF 111"), "m=video"),
    (OFFER.replace("a=recvonly\r\n", ""), "recvonly"),
    (OFFER + "a=x-pad:" + "x" * whep.MAX_OFFER_BYTES + "\r\n", "64 KB"),
    ("", "empty"),
], ids=["sendrecv", "sendonly", "no_video", "no_recvonly", "too_large", "empty"])
def test_bad_offers_are_rejected_before_any_ring_call(api, partner, sdp, words):
    ring, _ = partner
    response = open_session(api, sdp)

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["reason"] == "invalid_offer" and words in detail["message"]
    assert WHEP_POST not in ring.paths()
    assert funnel.snapshot()["whep_failed"]["invalid_offer"] == 1


# -- Location (SSRF) -------------------------------------------------------------

@pytest.fixture
def other_host():
    other = FakeRing().start()
    yield other
    other.stop()


def test_location_on_another_host_is_rejected_and_never_contacted(api, partner, other_host):
    ring, _ = partner
    ring.whep_location = f"{other_host.url}/whep/sessions/stolen"   # same IP, another port: another host

    response = open_session(api)

    assert response.status_code == 502 and response.json()["detail"]["reason"] == "invalid_session_url"
    assert other_host.url not in response.text and "stolen" not in response.text
    assert len(whep.sessions) == 0
    open_session(api)   # a retry is rejected the same way: nothing ever reaches the other host
    assert other_host.requests == []
    assert funnel.snapshot()["whep_failed"]["invalid_session_url"] == 2


@pytest.mark.parametrize("url, allowed", [
    ("https://api.amazonvision.com/v1/whep/sessions/1", True),
    ("https://api.amazonvision.com:443/v1/whep/sessions/1", False),   # explicit port differs from the base
    ("http://api.amazonvision.com/v1/whep/sessions/1", False),
    ("https://evil.example/v1/whep/sessions/1", False),
    ("https://api.amazonvision.com.evil.example/x", False),
    ("https://user:pw@api.amazonvision.com/x", False),
    ("ftp://api.amazonvision.com/x", False),
])
def test_session_url_must_be_https_on_the_ring_api_host(url, allowed):
    assert whep.session_url_allowed(url, "https://api.amazonvision.com") is allowed


def test_plain_http_is_allowed_only_on_127_0_0_1():
    assert whep.session_url_allowed("http://127.0.0.1:8123/whep/1", "http://127.0.0.1:8123")
    assert not whep.session_url_allowed("http://localhost:8123/whep/1", "http://localhost:8123")


# -- one session at a time, closing ------------------------------------------------

def test_second_session_closes_the_first(api, partner):
    ring, _ = partner
    first = open_session(api).json()["session_id"]
    second = open_session(api).json()["session_id"]

    assert first != second and len(whep.sessions) == 1
    methods = [r["method"] for r in ring.requests if r["path"] in (WHEP_POST, WHEP_SESSION_PATH)]
    assert methods == ["POST", "DELETE", "POST"]   # the old one is closed before Ring sees the new offer
    assert api.delete(f"/api/ring/whep/session/{first}").status_code == 404
    assert api.delete(f"/api/ring/whep/session/{second}").status_code == 200


def test_manual_close_deletes_at_ring_with_the_token_and_forgets(api, partner):
    ring, _ = partner
    session_id = open_session(api).json()["session_id"]

    response = api.delete(f"/api/ring/whep/session/{session_id}")

    assert response.status_code == 200 and response.json() == {"closed": True}
    (delete,) = deletes(ring)
    assert delete["path"] == WHEP_SESSION_PATH and delete["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert api.delete(f"/api/ring/whep/session/{session_id}").status_code == 404
    assert len(deletes(ring)) == 1


def test_ring_404_on_close_means_the_session_already_ended(api, partner):
    ring, _ = partner
    ring.whep_delete_status = 404
    session_id = open_session(api).json()["session_id"]

    assert api.delete(f"/api/ring/whep/session/{session_id}").status_code == 200


def test_unknown_session_is_404(api):
    assert api.delete("/api/ring/whep/session/" + "0" * 32).status_code == 404


def test_forgotten_session_is_closed_by_the_timer(api, partner, monkeypatch):
    ring, _ = partner
    waited = []

    async def instant(seconds):   # simulated time: the 65 s pass at once
        waited.append(seconds)

    monkeypatch.setattr(whep.sessions, "_sleep", instant)
    session_id = open_session(api).json()["session_id"]

    wait_for(lambda: len(deletes(ring)) == 1)
    assert waited == [65.0]
    assert deletes(ring)[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert funnel.snapshot()["whep_closed_by_timer"] == 1
    assert api.delete(f"/api/ring/whep/session/{session_id}").status_code == 404


def test_app_shutdown_closes_open_sessions(partner, monkeypatch):
    ring, _ = partner
    monkeypatch.setattr(whep, "sessions", whep.WhepSessions())
    with TestClient(server.app) as client:
        assert open_session(client).status_code == 200
    assert len(deletes(ring)) == 1 and len(whep.sessions) == 0


# -- Ring errors ----------------------------------------------------------------

@pytest.mark.parametrize("status, http_status, reason", [
    (503, 503, "device_offline_503"),
    (401, 502, "token_rejected"),
    (406, 502, "blocked_406"),
    (429, 429, "rate_limited"),
    (404, 404, "api_error"),
])
def test_ring_errors_keep_the_existing_reasons(api, partner, status, http_status, reason):
    ring, _ = partner
    ring.whep_replies = [(status, None, None)]

    response = open_session(api)

    assert response.status_code == http_status
    detail = response.json()["detail"]
    assert detail["reason"] == reason and detail["upstream_status"] == status
    assert detail["message"] == whep.REASON_TEXT[reason]
    assert TOKEN not in response.text and "/v1/devices" not in response.text
    assert len(whep.sessions) == 0 and funnel.snapshot()["whep_failed"][reason] == 1


def test_connection_error_is_network_error(api, partner, monkeypatch):
    async def unreachable(*_args, **_kwargs):
        raise RingPartnerError("Ring API POST connection error (ClientConnectorError)")

    monkeypatch.setattr(server.partner_client, "create_whep_session", unreachable)
    response = open_session(api)

    assert response.status_code == 502 and response.json()["detail"]["reason"] == "network_error"


def test_token_session_url_and_sdp_never_reach_logs_or_stdout(api, partner, caplog, capsys, monkeypatch):
    ring, _ = partner
    caplog.set_level(logging.DEBUG)

    async def instant(_seconds):
        pass

    session_id = open_session(api).json()["session_id"]
    api.delete(f"/api/ring/whep/session/{session_id}")
    monkeypatch.setattr(whep.sessions, "_sleep", instant)
    open_session(api)
    wait_for(lambda: len(deletes(ring)) == 2)
    ring.whep_replies = [(401, None, None)]
    open_session(api)

    out = capsys.readouterr()
    # The fake Ring's own access log and the test client's request log are not this app's logs.
    ours = " | ".join(r.getMessage() for r in caplog.records if not r.name.startswith(("aiohttp.access", "httpx")))
    for text in (ours, out.out, out.err):
        assert TOKEN not in text and WHEP_SESSION_PATH not in text and "a=recvonly" not in text
        assert "a=rtpmap" not in text


# -- mode ---------------------------------------------------------------------

def test_not_found_outside_partner_mode(monkeypatch):
    monkeypatch.setattr(server, "ring_source", "simulator")
    with TestClient(server.app) as client:
        assert open_session(client).status_code == 404
        assert client.delete("/api/ring/whep/session/" + "0" * 32).status_code == 404


def test_close_all_survives_a_failing_ring_delete(ring):
    class Client:
        base_url = ring.url

        async def create_whep_session(self, *_args):
            return WHEP_ANSWER, f"{ring.url}{WHEP_SESSION_PATH}"

        async def close_whep_session(self, _url):
            raise RingPartnerError("down", status=500)

    async def scenario():
        sessions = whep.WhepSessions()
        await sessions.open(Client(), DEVICE_ID, OFFER)
        await sessions.close_all()
        return len(sessions)

    assert asyncio.run(scenario()) == 0
