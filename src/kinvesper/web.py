"""Voice simulator: the web page and its small JSON API.

    GET    /voice                                       the simulator page
    POST   /api/voice/sessions                          start a conversation
    POST   /api/voice/sessions/{sid}/turns {"text"}     one utterance -> spoken reply + tool trace
    POST   /api/voice/sessions/{sid}/transcribe         recorded audio -> {"text"} (Whisper or Amazon Transcribe)
    POST   /api/voice/sessions/{sid}/speech {"text"}    text -> MP3 (Amazon Polly), 503 = use the browser
    GET    /api/voice/sessions/{sid}/announcements      results of finished background tasks, and
                                                        reminders going off (the page rings them like an Echo)
    DELETE /api/voice/sessions/{sid}                    end a conversation

With OAuth off (local dev) the simulator is open. With OAuth on it needs SERVICE_TOKEN (to reach
/mcp) and SIMULATOR_KEY, sent by the page as X-Simulator-Key (open /voice?key=...).
"""

from __future__ import annotations

import hmac
import logging
import time
from datetime import timedelta
from importlib.resources import files

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import llm, reminders, speech
from .config import now, settings
from .service import CareService
from .voice import Announcement, VoiceSession, VoiceSessions

log = logging.getLogger(__name__)

MAX_UTTERANCE = 1000
RING_WINDOW = timedelta(minutes=3)  # a reminder rings if the page checks within this long after it's due
RING_CHECK_S = 10  # how often a page's polls look for reminders going off


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


def due_rings(service: CareService, session: VoiceSession) -> list[Announcement]:
    """Reminders going off now that this page hasn't rung yet: the simulator stands in for the Echo."""
    current = now()
    locale = "hi-IN" if session.language == "hi" else "en-US"
    due = reminders.ringing(service.profile(), service.medications(), service.reminders(upcoming_only=False),
                            current - RING_WINDOW, current, locale)
    out = []
    for key, text, alarm in due:
        if key not in session.rung:
            session.rung.add(key)
            out.append(Announcement(text=text, ring="alarm" if alarm else "reminder"))
    return out


def voice_routes(sessions: VoiceSessions, service: CareService | None = None) -> list[Route]:
    page = files("kinvesper").joinpath("static/voice.html").read_text()
    last_check: dict[str, float] = {}

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
            "patient": session.patient,
            "medications": [m["name"] for m in session.meds],
            "tools": [t["name"] for t in session.tools],
            "server_voice": speech.tts_available(),
            "voice_input": speech.stt_available(),
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

    async def transcribe(request: Request) -> Response:
        session, denied = lookup(request)
        if denied:
            return denied
        audio = await request.body()
        if not audio:
            return JSONResponse({"error": "No audio received"}, status_code=400)
        if len(audio) > speech.MAX_AUDIO_BYTES:
            return JSONResponse({"error": "That recording is too long"}, status_code=413)
        try:
            text = await speech.transcribe(audio, session.vocabulary)
        except speech.SpeechUnavailable as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        return JSONResponse({"text": text[:MAX_UTTERANCE]})

    async def speak(request: Request) -> Response:
        _, denied = lookup(request)
        if denied:
            return denied
        try:
            text = str((await request.json()).get("text", "")).strip()
        except ValueError:
            text = ""
        if not text:
            return JSONResponse({"error": "Nothing to say"}, status_code=400)
        try:
            audio = await speech.synthesize(text)
        except speech.SpeechUnavailable as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    async def announcements(request: Request) -> Response:
        session, denied = lookup(request)
        if denied:
            return denied
        out = session.take_announcements()
        if service is not None and time.monotonic() - last_check.get(session.id, 0) >= RING_CHECK_S:
            last_check[session.id] = time.monotonic()
            try:
                out = due_rings(service, session) + out
            except Exception:
                log.exception("Could not check for reminders going off")
        return JSONResponse([a.model_dump() for a in out])

    async def end(request: Request) -> Response:
        session, denied = lookup(request)
        if denied:
            return denied
        await sessions.close(session.id)
        last_check.pop(session.id, None)
        return Response(status_code=204)

    return [
        Route("/", home),
        Route("/voice", voice_page),
        Route("/api/voice/sessions", create, methods=["POST"]),
        Route("/api/voice/sessions/{sid}/turns", turn, methods=["POST"]),
        Route("/api/voice/sessions/{sid}/transcribe", transcribe, methods=["POST"]),
        Route("/api/voice/sessions/{sid}/speech", speak, methods=["POST"]),
        Route("/api/voice/sessions/{sid}/announcements", announcements),
        Route("/api/voice/sessions/{sid}", end, methods=["DELETE"]),
    ]
