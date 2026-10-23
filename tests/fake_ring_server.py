"""
Reusable fake Ring API for tests (Tanda 8b onwards): an aiohttp server on 127.0.0.1 in its own
thread, so it serves both asyncio tests and FastAPI's synchronous TestClient. No real network.

It implements, with fictitious data shaped like the documented JSON:API payloads:
  GET  /v1/devices, /v1/devices/{id}?include=..., /v1/devices/{id}/{status|capabilities}
  POST /v1/devices/{id}/media/image/download   -> scripted replies, default 303 + Location
  GET  /media/presigned/{key}                  -> scripted replies, default the image (no Authorization expected)
  GET  /v1/history/devices/{id}/events         -> event pages linked by links.next

Every request is recorded in `requests` (method, path, query, headers, json body).
"""
import asyncio
import threading
from typing import Any, Dict, List, Optional

import cv2
import numpy as np
from aiohttp import web

DEVICE_ID = "ava1.ring.device.FAKE1"
PRESIGNED_PATH = "/media/presigned/snap-1"


# ---------------------------------------------------------------------------
# Synthetic frames (1280x720 BGR) and encoders
# ---------------------------------------------------------------------------

def sharp_frame() -> np.ndarray:
    """Textured, mid-brightness, neutral colour: passes every image check."""
    rng = np.random.default_rng(3)
    gray = cv2.GaussianBlur(rng.integers(0, 256, (720, 1280), dtype=np.uint8), (0, 0), 1.0)
    gray = cv2.normalize(gray, None, 70, 190, cv2.NORM_MINMAX)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def blurry_frame() -> np.ndarray:
    return cv2.GaussianBlur(sharp_frame(), (0, 0), 9.0)


def pink_frame() -> np.ndarray:
    """Sharp texture with a strong magenta cast (red and blue far above green)."""
    frame = sharp_frame().astype(np.int16)
    frame[:, :, 0] += 40   # B
    frame[:, :, 1] -= 60   # G
    frame[:, :, 2] += 60   # R
    return np.clip(frame, 0, 255).astype(np.uint8)


def jpeg(frame: np.ndarray) -> bytes:
    return cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])[1].tobytes()


# ---------------------------------------------------------------------------
# Fake server
# ---------------------------------------------------------------------------

def jsonapi_error(status: int, code: Optional[str] = None, headers: Optional[Dict[str, str]] = None):
    error: Dict[str, Any] = {"status": str(status), "title": "Fake error"}
    if code:
        error["code"] = code
    return web.json_response({"errors": [error]}, status=status, headers=headers)


class FakeRing:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.name = "Fake Door"
        self.status: Dict[str, Any] = {"online": True}
        self.capabilities: Dict[str, Any] = {
            "video": {"max_resolution": 1080},
            "image_enhancements": {"configurations": ["color_night_vision", "privacy_zones", "snapshot"]},
        }
        self.configurations: Dict[str, Any] = {
            "e2e_encryption": {"enabled": False},
            "image_enhancements": {"color_night_vision": "off", "privacy_zones": []},
        }
        self.location: Dict[str, Any] = {"country": "US"}
        # POST replies in order: (status, headers, code); the last one repeats. None -> 303 + Location.
        self.snapshot_replies: List[Optional[tuple]] = [None]
        self.image: bytes = jpeg(sharp_frame())
        # Pre-signed GET replies in order: (status, json_body); the last one repeats. None -> the image.
        self.presigned_replies: List[Optional[tuple]] = [None]
        self.image_headers: Dict[str, str] = {"X-Media-Timestamp": "1761210000000", "X-Media-Origin": "recording"}
        # Event pages by page key ("" = first page); each is a list of history-events items.
        self.event_pages: Dict[str, List[Dict[str, Any]]] = {"": []}
        self.event_links: Dict[str, str] = {}      # page key -> next page key
        self.events_error: Optional[tuple] = None   # (status, headers)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[web.AppRunner] = None
        self.url = ""

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> "FakeRing":
        ready = threading.Event()

        def serve() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._start())
            ready.set()
            self._loop.run_forever()

        threading.Thread(target=serve, daemon=True).start()
        if not ready.wait(10):
            raise RuntimeError("fake Ring server did not start")
        return self

    def stop(self) -> None:
        if self._loop and self._runner:
            asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(10)
            self._loop.call_soon_threadsafe(self._loop.stop)

    async def _start(self) -> None:
        app = web.Application(middlewares=[self._record])
        app.router.add_get("/v1/devices", self._devices)
        app.router.add_get("/v1/devices/{device_id}", self._device)
        app.router.add_get("/v1/devices/{device_id}/{kind}", self._resource)
        app.router.add_post("/v1/devices/{device_id}/media/image/download", self._snapshot)
        app.router.add_get("/media/presigned/{key}", self._presigned)
        app.router.add_get("/v1/history/devices/{device_id}/events", self._events)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    # -- helpers for assertions -----------------------------------------------

    def paths(self, method: Optional[str] = None) -> List[str]:
        return [r["path"] for r in self.requests if method is None or r["method"] == method]

    # -- handlers ---------------------------------------------------------------

    @web.middleware
    async def _record(self, request: web.Request, handler):
        body = await request.json() if request.method == "POST" and request.can_read_body else None
        self.requests.append({"method": request.method, "path": request.path, "query": dict(request.query),
                              "headers": dict(request.headers), "json": body})
        return await handler(request)

    async def _devices(self, _request):
        return web.json_response({"data": [{"type": "devices", "id": DEVICE_ID, "attributes": {"name": self.name}}]})

    async def _device(self, _request):
        return web.json_response({
            "data": {"type": "devices", "id": DEVICE_ID, "attributes": {"name": self.name}},
            "included": [
                {"type": "device-status", "id": DEVICE_ID, "attributes": self.status},
                {"type": "device-capabilities", "id": DEVICE_ID, "attributes": self.capabilities},
                {"type": "device-configurations", "id": DEVICE_ID, "attributes": self.configurations},
                {"type": "device-location", "id": DEVICE_ID, "attributes": self.location},
            ],
        })

    async def _resource(self, request):
        attrs = {"status": self.status, "capabilities": self.capabilities}.get(request.match_info["kind"])
        if attrs is None:
            return jsonapi_error(404)
        return web.json_response({"data": {"type": f"device-{request.match_info['kind']}", "id": DEVICE_ID,
                                           "attributes": attrs}})

    async def _snapshot(self, _request):
        reply = self.snapshot_replies.pop(0) if len(self.snapshot_replies) > 1 else self.snapshot_replies[0]
        if reply is None:
            return web.Response(status=303, headers={"Location": PRESIGNED_PATH})
        status, headers, code = reply
        return jsonapi_error(status, code=code, headers=headers)

    async def _presigned(self, _request):
        reply = self.presigned_replies.pop(0) if len(self.presigned_replies) > 1 else self.presigned_replies[0]
        if reply is not None:
            status, body = reply
            return web.json_response(body, status=status)
        return web.Response(body=self.image, content_type="image/jpeg", headers=self.image_headers)

    async def _events(self, request):
        if self.events_error:
            status, headers = self.events_error
            return jsonapi_error(status, headers=headers)
        key = request.query.get("page[key]", "")
        document: Dict[str, Any] = {"data": self.event_pages.get(key, [])}
        if key in self.event_links:
            document["links"] = {"next": f"{request.path}?page[key]={self.event_links[key]}"}
        return web.json_response(document)


def greco_no_valid_key() -> tuple:
    """Pre-signed GET reply seen live (fictitious id/timestamp): 422 with an undocumented error code."""
    return 422, {"errors": [{"id": "fake-error-1", "status": "422", "code": "GRECO_NO_VALID_KEY",
                             "detail": "No valid Greco key available for decryption",
                             "meta": {"timestamp": "2026-10-23T10:00:00Z"}}]}


def history_event(event_id: str, event_type: str, start_ms: int, reviewed: bool = False) -> Dict[str, Any]:
    return {"type": "history-events", "id": event_id,
            "attributes": {"event_type": event_type, "is_third_party_reviewed": reviewed,
                           "start": start_ms, "end": start_ms + 20000},
            "relationships": {"source": {"data": {"type": "devices", "id": DEVICE_ID}}}}
