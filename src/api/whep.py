"""
whep.py — Ring live view (WHEP) with this server as the SDP exchange intermediary.

Ring's API is server-to-server only (browsers are blocked by CORS), so the browser builds the SDP offer,
this server forwards it to Ring with the access token, and the browser gets only the SDP answer and an
opaque session_id. Ring's session URL (Location) and the token stay here and are never logged or returned.

One session at a time: a new request closes the previous one first (a reload or a second tab must not
leave a session open). A session the browser does not close is closed by this server after
SESSION_TTL_SECONDS (Ring ends them after 30 s on battery devices and 60 s on wired ones), and every open
session is closed when the app shuts down.
"""

import asyncio
import logging
import secrets
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import urlsplit

from src.api.funnel import funnel
from src.utils.ring_partner_client import RingPartnerClient, RingPartnerError

logger = logging.getLogger(__name__)

MAX_OFFER_BYTES = 64 * 1024        # design limit for one SDP offer, not a Ring value
SESSION_TTL_SECONDS = 65.0
BATTERY_MAX_SECONDS, WIRED_MAX_SECONDS = 30, 60

# Customer wording per failure reason (the T21 / T42 reasons plus WHEP's own); never the token or a URL.
REASON_TEXT = {
    "invalid_offer": "the browser's video offer was not accepted.",
    "missing_token": "this server has no Ring access token.",
    "token_rejected": "Ring rejected this server's access token.",
    "blocked_406": "Ring's firewall blocked this server's request (HTTP 406).",
    "rate_limited": "Ring's rate limit was reached; try again in a moment.",
    "device_offline_503": "Ring says the device is offline or busy (HTTP 503).",
    "network_error": "Ring could not be reached (timeout or connection error).",
    "invalid_session_url": "Ring returned a session address this server does not trust, so the session was dropped.",
    "time_range_not_authorized": "Ring did not authorize this request.",
    "api_error": "Ring returned an error for this device's live view.",
}


class WhepOfferError(ValueError):
    """The SDP offer is not a receive-only video offer this server forwards."""


class WhepSessionUrlError(Exception):
    """Ring's Location is not an HTTPS URL on Ring's API host (the token must never go elsewhere)."""


def reason_text(reason: str) -> str:
    return REASON_TEXT.get(reason, REASON_TEXT["api_error"])


def validate_offer(sdp: Any) -> str:
    """Returns the offer, or raises WhepOfferError: at most 64 KB, a video section, receive-only."""
    if not isinstance(sdp, str) or not sdp.strip():
        raise WhepOfferError("The SDP offer is empty.")
    if len(sdp.encode("utf-8")) > MAX_OFFER_BYTES:
        raise WhepOfferError(f"The SDP offer is larger than {MAX_OFFER_BYTES // 1024} KB.")
    lines = [line.strip() for line in sdp.splitlines()]
    if not sdp.startswith("v=0") or not any(line.startswith("m=video") for line in lines):
        raise WhepOfferError("The SDP offer has no video section (m=video).")
    if any(line in ("a=sendrecv", "a=sendonly") for line in lines):
        raise WhepOfferError("The SDP offer must be receive-only: sendrecv and sendonly are not allowed.")
    if "a=recvonly" not in lines:
        raise WhepOfferError("The SDP offer must declare a=recvonly video.")
    return sdp


def session_url_allowed(url: str, base_url: str) -> bool:
    """HTTPS (plain HTTP only on 127.0.0.1, for tests) on exactly the API host and port, without user info."""
    try:
        target, base = urlsplit(url), urlsplit(base_url)
        target_port, base_port = target.port, base.port
    except ValueError:
        return False
    secure = target.scheme == "https" or (target.scheme == "http" and target.hostname == "127.0.0.1")
    return (secure and target.scheme == base.scheme and target.username is None and target.password is None
            and target.hostname == base.hostname and target_port == base_port)


def max_seconds_hint(telemetry: Dict[str, Any]) -> int:
    """Ring's session limit: 60 s when capabilities say no battery (wired), else the 30 s battery limit."""
    status = ((telemetry.get("device_health") or {}).get("measurement_status") or {}).get("battery_percentage")
    return WIRED_MAX_SECONDS if status == "not_supported" else BATTERY_MAX_SECONDS


class WhepSessions:
    """In-memory WHEP sessions: session_id -> Ring session URL, the client that opened it and its timer."""

    def __init__(self, ttl_seconds: float = SESSION_TTL_SECONDS,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.ttl_seconds = ttl_seconds
        self._sleep = sleep
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def __len__(self) -> int:
        return len(self._sessions)

    async def open(self, client: RingPartnerClient, device_id: str, offer: str,
                   component_id: Optional[str] = None) -> Dict[str, str]:
        """
        Closes any open session, opens one at Ring and returns {session_id, sdp_answer}. Raises
        RingPartnerError (from Ring) or WhepSessionUrlError (untrusted Location, nothing sent to it).
        """
        await self.close_all()
        answer, url = await client.create_whep_session(device_id, offer, component_id)
        if not session_url_allowed(url, client.base_url):
            raise WhepSessionUrlError("Ring returned an untrusted WHEP session URL")
        session_id = secrets.token_hex(16)   # 128 bits
        self._sessions[session_id] = {"url": url, "client": client,
                                      "task": asyncio.create_task(self._expire(session_id))}
        # A concurrent request may have registered one meanwhile: keep only the newest.
        for other in [sid for sid in self._sessions if sid != session_id]:
            await self._close_quietly(other)
        return {"session_id": session_id, "sdp_answer": answer}

    async def close(self, session_id: str) -> bool:
        """False for an unknown session_id. The session is forgotten even if Ring's DELETE fails (raised)."""
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return False
        entry["task"].cancel()
        await entry["client"].close_whep_session(entry["url"])
        return True

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self._close_quietly(session_id)

    async def _close_quietly(self, session_id: str) -> None:
        try:
            await self.close(session_id)
        except RingPartnerError as exc:
            logger.warning("[WHEP] closing a session failed (HTTP %s)", exc.status)   # never the URL

    async def _expire(self, session_id: str) -> None:
        await self._sleep(self.ttl_seconds)
        entry = self._sessions.pop(session_id, None)
        if entry is None:
            return
        funnel.count("whep_closed_by_timer")
        logger.info("[WHEP] session not closed by the browser; closed after %.0f s", self.ttl_seconds)
        try:
            await entry["client"].close_whep_session(entry["url"])
        except RingPartnerError as exc:
            logger.warning("[WHEP] closing an expired session failed (HTTP %s)", exc.status)


sessions = WhepSessions()
