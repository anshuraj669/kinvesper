"""Alexa skill bridge: a classic custom skill that talks to Kinvesper on any Echo, today.

    POST /alexa      Alexa Skills Kit requests (signed by Amazon)

Alexa+ add-ons reach /mcp directly, but that tooling is allowlisted. Until then a custom skill
("Alexa, open kin vesper") hands each phrase to the same voice agent as the simulator, which calls
the same MCP tools. A catch-all slot carries the whole utterance; Yes/No answer Kinvesper's
confirmation questions; Stop and Cancel end the conversation. "Set up my pill reminders" asks for the
reminders permission by voice and has the Echo itself remind the person at every dose time.

Every request is checked as Amazon requires for an HTTPS endpoint: the signing certificate chain
(from Amazon's S3 bucket, issued to echo-api.amazon.com, trusted root), the SHA-256 signature of the
body, a timestamp within 150 seconds, and our skill ID. The bridge is off until ALEXA_SKILL_ID is set.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import posixpath
import time
from datetime import UTC, datetime
from urllib.parse import urlsplit

import certifi
import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import llm
from .config import settings
from .i18n import lang, t
from .reminders import SCOPE, EchoReminders, spoken_times
from .service import CareService
from .voice import VoiceSession, VoiceSessions

log = logging.getLogger(__name__)

MAX_REQUEST_AGE_S = 150
REPLY_DEADLINE_S = 7  # Alexa gives a skill 8 seconds
SIGNING_HOST = "echo-api.amazon.com"
MAX_BODY_BYTES = 128 * 1024

REPROMPT = t("en", "reprompt")
STILL_WORKING = t("en", "still_working")


class InvalidRequest(Exception):
    pass


class RequestVerifier:
    """Checks that a request really comes from Alexa, for our skill."""

    def __init__(self, skill_id: str, fetch=None, now=None, roots: Store | None = None):
        self.skill_id = skill_id
        self._fetch = fetch or _fetch_pem
        self._now = now or (lambda: datetime.now(UTC))
        self._chains: dict[str, x509.Certificate] = {}
        self._store = roots or Store(x509.load_pem_x509_certificates(open(certifi.where(), "rb").read()))

    async def verify(self, headers, body: bytes, request: dict) -> None:
        leaf = await self._signing_cert(headers.get("signaturecertchainurl", ""))
        try:
            signature = base64.b64decode(headers.get("signature-256", ""), validate=True)
            leaf.public_key().verify(signature, body, padding.PKCS1v15(), hashes.SHA256())
        except (ValueError, InvalidSignature) as e:
            raise InvalidRequest("bad signature") from e

        try:
            stamp = datetime.fromisoformat(request["request"]["timestamp"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as e:
            raise InvalidRequest("no timestamp") from e
        if abs((self._now() - stamp).total_seconds()) > MAX_REQUEST_AGE_S:
            raise InvalidRequest("stale request")

        if application_id(request) != self.skill_id:
            raise InvalidRequest("wrong skill")

    async def _signing_cert(self, url: str) -> x509.Certificate:
        check_cert_url(url)
        leaf = self._chains.get(url)
        if leaf is None or not self._valid_now(leaf):
            try:
                chain = x509.load_pem_x509_certificates(await self._fetch(url))
            except (httpx.HTTPError, ValueError) as e:
                raise InvalidRequest("can't load the signing certificate") from e
            leaf = chain[0]
            verifier = PolicyBuilder().store(self._store).time(self._now()).build_server_verifier(
                x509.DNSName(SIGNING_HOST))  # also checks dates and the subject alternative name
            try:
                verifier.verify(leaf, chain[1:])
            except VerificationError as e:
                raise InvalidRequest(f"untrusted signing certificate: {e}") from e
            self._chains[url] = leaf
        return leaf

    def _valid_now(self, cert: x509.Certificate) -> bool:
        return cert.not_valid_before_utc <= self._now() <= cert.not_valid_after_utc


def check_cert_url(url: str) -> None:
    """Amazon's rules for SignatureCertChainUrl: https://s3.amazonaws.com[:443]/echo.api/..."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise InvalidRequest("bad certificate URL") from e
    path = posixpath.normpath(parts.path) if parts.path else ""  # "/echo.api/../echo.api/x" is allowed
    if (parts.scheme.lower() != "https" or parts.hostname != "s3.amazonaws.com"
            or port not in (None, 443) or not path.startswith("/echo.api/")):
        raise InvalidRequest(f"bad certificate URL {url[:200]!r}")


async def _fetch_pem(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=5) as http:
        r = await http.get(url)
        r.raise_for_status()
        return r.content


def application_id(request: dict) -> str:
    system = (request.get("context") or {}).get("System") or {}
    return ((system.get("application") or (request.get("session") or {}).get("application") or {})
            .get("applicationId", ""))


def ask_reminders_permission() -> dict:
    """Alexa asks the person, by voice, to let Kinvesper set reminders; the answer comes back as a
    Connections.Response request."""
    return {"version": "1.0", "response": {"directives": [{
        "type": "Connections.SendRequest", "name": "AskFor", "token": "",
        "payload": {"@type": "AskForPermissionsConsentRequest", "@version": "2",
                    "permissionScopes": [{"permissionScope": SCOPE, "consentLevel": "ACCOUNT"}]},
    }]}}


def speak(text: str, *, end: bool, reprompt: str | None = REPROMPT) -> dict:
    response: dict = {"outputSpeech": {"type": "PlainText", "text": text}, "shouldEndSession": end}
    if not end and reprompt:
        response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "response": response}


class AlexaBridge:
    """One Kinvesper voice session per Alexa session."""

    def __init__(self, sessions: VoiceSessions, service: CareService | None = None):
        self.sessions = sessions
        self.service = service
        self.reminders = EchoReminders(service.store) if service else None
        self._background: set[asyncio.Task] = set()
        self._by_alexa: dict[str, str] = {}
        # Answers that missed Alexa's deadline, spoken on the person's next turn.
        self._late: dict[str, asyncio.Future] = {}

    async def _session(self, alexa_id: str) -> VoiceSession:
        if (sid := self._by_alexa.get(alexa_id)) and (session := self.sessions.get(sid)):
            return session
        session = await self.sessions.create()
        self._by_alexa[alexa_id] = session.id
        return session

    async def _end(self, alexa_id: str) -> None:
        self._late.pop(alexa_id, None)
        if sid := self._by_alexa.pop(alexa_id, None):
            await self.sessions.close(sid)

    async def handle(self, body: dict) -> dict:
        request = body.get("request") or {}
        kind = request.get("type", "")
        alexa_id = (body.get("session") or {}).get("sessionId", "")
        one_shot = bool((body.get("session") or {}).get("new")) and kind == "IntentRequest"

        system = (body.get("context") or {}).get("System") or {}
        locale = request.get("locale") or "en-US"

        def say(key: str, end: bool = False, **values: str) -> dict:
            return speak(t(locale, key, **values), end=end, reprompt=t(locale, "reprompt"))

        if kind == "SessionEndedRequest":
            await self._end(alexa_id)
            return {"version": "1.0", "response": {}}
        if kind == "Connections.Response" and request.get("name") == "AskFor":
            scopes = (request.get("payload") or {}).get("permissionScopes") or [{}]
            if scopes[0].get("status") == "ACCEPTED":
                return await self._set_reminders(system, locale, ask=False)
            return say("reminders_declined")

        session = await self._session(alexa_id)
        session.language = lang(locale)  # Claude answers in the language the skill is used in
        anything = t(locale, "anything") if llm.enabled() else ""
        if kind == "LaunchRequest":
            self._refresh_reminders(system, locale)
            return say("welcome", name=session.patient, caregiver=session.caregiver, anything=anything)
        if kind != "IntentRequest":
            return say("cant_do")

        intent = request.get("intent") or {}
        name = intent.get("name", "")
        if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
            if session.awaiting_confirmation:  # a pending "send this to <caregiver>?" counts as no
                await session.turn("no")
            await self._end(alexa_id)
            return say("goodbye", end=True)
        if name == "AMAZON.HelpIntent":
            medicine = session.meds[0]["name"] if session.meds else "medicine"
            return say("help", caregiver=session.caregiver, medicine=medicine, anything=anything)
        if late := self._late.pop(alexa_id, None):  # whatever they said, they're waiting for this answer
            return await self._reply(alexa_id, session, late, one_shot=False, locale=locale)
        if name == "SetUpRemindersIntent":
            return await self._set_reminders(system, locale, ask=True)
        if name == "RemoveRemindersIntent":
            return await self._remove_reminders(system, locale)
        if name == "AMAZON.FallbackIntent":
            return say("not_caught")

        if name == "AMAZON.YesIntent":
            text = "yes"
        elif name == "AMAZON.NoIntent":
            text = "no"
        else:
            slots = intent.get("slots") or {}
            text = ((slots.get("utterance") or {}).get("value") or "").strip()
        if not text:
            return say("not_caught")
        log.info("Alexa heard %r", text)
        return await self._reply(alexa_id, session, asyncio.ensure_future(session.turn(text)), one_shot, locale)

    async def _set_reminders(self, system: dict, locale: str, ask: bool) -> dict:
        def say(key: str, **values: str) -> dict:
            return speak(t(locale, key, **values), end=False, reprompt=t(locale, "reprompt"))

        if self.reminders is None:
            return say("reminders_unavailable")
        meds = self.service.medications()
        if not meds:
            return say("reminders_no_meds")
        result = await self.reminders.sync(system, self.service.profile(), meds, locale, force=True)
        if result.outcome in ("ok", "unchanged"):
            return say("reminders_set", times=spoken_times(result.times, locale))
        if result.outcome == "no_permission" and ask:
            return ask_reminders_permission()
        if result.outcome == "no_permission":
            return say("reminders_still_blocked")
        return say("reminders_failed")

    async def _remove_reminders(self, system: dict, locale: str) -> dict:
        if self.reminders is None:
            return speak(t(locale, "reminders_unavailable"), end=False, reprompt=t(locale, "reprompt"))
        key = "reminders_off" if await self.reminders.clear(system) == "ok" else "reminders_off_failed"
        return speak(t(locale, key), end=False, reprompt=t(locale, "reprompt"))

    def _refresh_reminders(self, system: dict, locale: str) -> None:
        """When the skill opens, quietly bring reminders up to date if the medicines changed (only once
        the person has allowed reminders; never asks)."""
        if self.reminders is None or not system.get("apiAccessToken"):
            return

        async def refresh() -> None:
            try:
                await self.reminders.sync(system, self.service.profile(), self.service.medications(), locale)
            except Exception:
                log.exception("Reminder refresh failed")

        task = asyncio.create_task(refresh())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _reply(self, alexa_id: str, session: VoiceSession, turn: asyncio.Future, one_shot: bool,
                     locale: str = "en-US") -> dict:
        try:
            result = await asyncio.wait_for(asyncio.shield(turn), REPLY_DEADLINE_S)
        except TimeoutError:
            self._late[alexa_id] = turn
            return speak(t(locale, "still_working"), end=False, reprompt=t(locale, "say_okay"))
        news = " ".join(a.text for a in session.take_announcements())
        reply = f"{news} {result.reply}".strip() if news else result.reply
        log.info("Alexa said %r (%s)", reply, ", ".join(c.name for c in result.tool_calls) or "no tools")
        if result.awaiting_confirmation:
            return speak(reply, end=False, reprompt=reply)
        if one_shot:  # "Alexa, ask kin vesper ...": answer and finish, like any one-shot request
            await self._end(alexa_id)
            return speak(reply, end=True)
        return speak(reply, end=False, reprompt=t(locale, "reprompt"))


def alexa_routes(sessions: VoiceSessions, service: CareService | None = None) -> list[Route]:
    if not settings.alexa_skill_id:
        return []
    verifier = RequestVerifier(settings.alexa_skill_id)
    bridge = AlexaBridge(sessions, service)

    async def endpoint(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            payload = await request.json()
            await verifier.verify(request.headers, body, payload)
        except (ValueError, InvalidRequest) as e:
            log.warning("Rejected Alexa request: %s", e)
            return JSONResponse({"error": "invalid request"}, status_code=400)
        started = time.perf_counter()
        try:
            reply = await bridge.handle(payload)
        except Exception:
            log.exception("Alexa request failed")
            reply = speak(t((payload.get("request") or {}).get("locale"), "unreachable"), end=True)
        log.info("Alexa request %s answered in %.0f ms", payload.get("request", {}).get("type"),
                 (time.perf_counter() - started) * 1000)
        return JSONResponse(reply)

    return [Route("/alexa", endpoint, methods=["POST"])]
