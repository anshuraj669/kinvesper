"""Voice simulator: the web page and its small JSON API.

    GET    /voice                                       the simulator page
    POST   /api/voice/sessions                          start a conversation
    POST   /api/voice/sessions/{sid}/turns {"text"}     one utterance -> spoken reply + tool trace
    GET    /api/voice/sessions/{sid}/announcements      results of finished background tasks
    DELETE /api/voice/sessions/{sid}                    end a conversation

With OAuth off (local dev) the simulator is open. With OAuth on it needs SERVICE_TOKEN (to reach
/mcp) and SIMULATOR_KEY, sent by the page as X-Simulator-Key (open /voice?key=...).
"""

from __future__ import annotations

import hmac
import logging
from importlib.resources import files

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import llm
from .config import settings
from .voice import VoiceSessions

log = logging.getLogger(__name__)

MAX_UTTERANCE = 1000


def simulator_status() -> str | None:
    """None when the simulator may run, else the reason it is off."""
    if not settings.oauth_issuer:
        return None
    if not (settings.service_token and settings.simulator_key):
        return "The voice simulator is off: with OAuth on it needs SERVICE_TOKEN and SIMULATOR_KEY."
    return None


def _allowed(request: Request) -> bool:
    if not settings.oauth_issuer:
        return True
    return hmac.compare_digest(request.headers.get("x-simulator-key", ""), settings.simulator_key)


def voice_routes(sessions: VoiceSessions) -> list[Route]:
    page = files("kinvesper").joinpath("static/voice.html").read_text()

    def guard(request: Request) -> Response | None:
        if reason := simulator_status():
            return JSONResponse({"error": reason}, status_code=404)
        if not _allowed(request):
            return JSONResponse({"error": "Missing or wrong simulator key"}, status_code=403)
        return None

    async def home(_: Request) -> Response:
        return RedirectResponse("/voice")

    async def voice_page(_: Request) -> Response:
        if reason := simulator_status():
            return HTMLResponse(f"<p>{reason}</p>", status_code=404)
        return HTMLResponse(page)

    async def create(request: Request) -> Response:
        if denied := guard(request):
            return denied
        try:
            session = await sessions.create()
        except Exception:
            log.exception("Could not open a voice session")
            return JSONResponse({"error": "Could not connect to the Kinvesper MCP server"}, status_code=502)
        return JSONResponse({
            "id": session.id,
            "mode": session.mode,
            "provider": settings.llm_provider if llm.enabled() else "none",
            "caregiver": session.caregiver,
            "tools": [t["name"] for t in session.tools],
        })

    def lookup(request: Request):
        if denied := guard(request):
            return None, denied
        session = sessions.get(request.path_params["sid"])
        if session is None:
            return None, JSONResponse({"error": "Unknown or expired session"}, status_code=404)
        return session, None

    async def turn(request: Request) -> Response:
        session, denied = lookup(request)
        if denied:
            return denied
        try:
            text = str((await request.json()).get("text", "")).strip()
        except ValueError:
            text = ""
        if not text:
            return JSONResponse({"error": "Say something first"}, status_code=400)
        result = await session.turn(text[:MAX_UTTERANCE])
        return JSONResponse(result.model_dump())

    async def announcements(request: Request) -> Response:
        session, denied = lookup(request)
        if denied:
            return denied
        return JSONResponse([a.model_dump() for a in session.take_announcements()])

    async def end(request: Request) -> Response:
        session, denied = lookup(request)
        if denied:
            return denied
        await sessions.close(session.id)
        return Response(status_code=204)

    return [
        Route("/", home),
        Route("/voice", voice_page),
        Route("/api/voice/sessions", create, methods=["POST"]),
        Route("/api/voice/sessions/{sid}/turns", turn, methods=["POST"]),
        Route("/api/voice/sessions/{sid}/announcements", announcements),
        Route("/api/voice/sessions/{sid}", end, methods=["DELETE"]),
    ]
