"""Alexa skill bridge: a classic custom skill that talks to Kinvesper on any Echo, today.

    POST /alexa      Alexa Skills Kit requests (signed by Amazon)

Alexa+ add-ons reach /mcp directly, but that tooling is allowlisted. Until then a custom skill
("Alexa, open kin vesper") hands each phrase to the same voice agent as the simulator, which calls
the same MCP tools. A catch-all slot carries the whole utterance; Yes/No answer Kinvesper's
confirmation questions; Stop and Cancel end the conversation. With ALEXA_ACCOUNT_LINKING=required the
skill answers only once a family login is linked in the Alexa app (the same Cognito logins as the
dashboard): until then it asks for linking and puts a Link account card in the app. "Set up my pill reminders" asks for the
reminders permission by voice and has the Echo itself remind the person at every dose time. On an Echo
Show every reply also shows today's medicines, with a "Taken" button for each dose that's due. When the
skill opens, family messages sent from the dashboard are read out first.

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
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html import escape
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

from . import apl, llm, media, signin
from .auth import JwtTokenVerifier
from .config import settings
from .i18n import lang, t
from .reminders import SCOPE, TIMER_SCOPE, EchoReminders, spoken_times
from .service import CareService, heard_messages, heard_one
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


def ask_reminders_permission(purpose: str = "pills") -> dict:
    """Alexa asks the person, by voice, to let Kinvesper set reminders and timers (alarms); the answer
    comes back as a Connections.Response request carrying `purpose` as its token."""
    return {"version": "1.0", "response": {"directives": [{
        "type": "Connections.SendRequest", "name": "AskFor", "token": purpose,
        "payload": {"@type": "AskForPermissionsConsentRequest", "@version": "2",
                    "permissionScopes": [{"permissionScope": scope, "consentLevel": "ACCOUNT"}
                                         for scope in (SCOPE, TIMER_SCOPE)]},
    }]}}


def link_account(locale: str) -> dict:
    """Ask for account linking: Alexa shows a Link account card in the app."""
    reply = speak(t(locale, "link_account"), end=True)
    reply["response"]["card"] = {"type": "LinkAccount"}
    return reply


def speak(text: str, *, end: bool, reprompt: str | None = REPROMPT) -> dict:
    """Alexa's reply. Text that starts with <speak> is SSML (a family voice note is an <audio> in it)."""
    said = {"type": "SSML", "ssml": text} if text.startswith("<speak>") else {"type": "PlainText", "text": text}
    response: dict = {"outputSpeech": said, "shouldEndSession": end}
    if not end and reprompt:
        response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "response": response}


class AlexaBridge:
    """One Kinvesper voice session per Alexa session."""

    def __init__(self, sessions: VoiceSessions, service: CareService | None = None,
                 linked: Callable[[str], Awaitable[bool]] | None = None):
        self.sessions = sessions
        self.service = service
        self.linked = linked  # account linking required: is this access token a family login?
        self.reminders = EchoReminders(service.store) if service else None
        self._background: set[asyncio.Task] = set()
        self._by_alexa: dict[str, str] = {}
        # Answers that missed Alexa's deadline, spoken on the person's next turn.
        self._late: dict[str, asyncio.Future] = {}
        self._photos: dict[str, tuple[str, str]] = {}  # a family photo to show with this reply: (url, caption)

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
        kind = (body.get("request") or {}).get("type", "")
        if self.linked and kind != "SessionEndedRequest":
            user = ((body.get("context") or {}).get("System") or {}).get("user") or (body.get("session") or {}).get("user") or {}
            token = user.get("accessToken") or ""
            if not token or not await self.linked(token):
                return link_account((body.get("request") or {}).get("locale") or "en-US")
        reply = await self._handle(body)
        response = reply.get("response")
        photo = self._photos.pop((body.get("session") or {}).get("sessionId", ""), None)
        if (response is not None and self.service and apl.supports_screen(body)
                and not any(d.get("type") == "Connections.SendRequest" for d in response.get("directives", []))):
            try:
                said = (response.get("outputSpeech") or {}).get("text", "")
                response.setdefault("directives", []).append(
                    apl.photo(*photo) if photo else
                    apl.screen(self.service.schedule(), self.service.profile().preferred_name, said if len(said) < 140 else ""))
            except Exception:
                log.exception("Couldn't draw the Echo Show screen")
        return reply

    async def _handle(self, body: dict) -> dict:
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
        if kind == "Alexa.Presentation.APL.UserEvent" and self.service:  # "Taken" touched on the Echo Show
            args = request.get("arguments") or []
            if len(args) >= 2 and args[0] == "taken":
                self.service.touch_activity()
                result = self.service.log_dose(str(args[1]), source="screen")
                if result.status == "already_taken":
                    await self.service.note_repeat("double_dose")
                return speak(result.message, end=False, reprompt=t(locale, "reprompt"))
            return say("cant_do")
        if kind == "Connections.Response" and request.get("name") == "AskFor":
            scopes = (request.get("payload") or {}).get("permissionScopes") or [{}]
            accepted = any(s.get("status") == "ACCEPTED" and s.get("permissionScope", SCOPE) == SCOPE for s in scopes)
            if request.get("token") == "oneoff":
                if accepted and await self._push_reminders(system, locale) == "ok":
                    return say("reminder_saved")
                return say("reminder_not_on_echo")
            if accepted:
                return await self._set_reminders(system, locale, ask=False)
            return say("reminders_declined")

        session = await self._session(alexa_id)
        session.language = lang(locale)  # Claude answers in the language the skill is used in
        anything = t(locale, "anything") if llm.enabled() else ""
        if kind == "LaunchRequest":
            self._refresh_reminders(system, locale)
            welcome = t(locale, "welcome", name=session.patient, caregiver=session.caregiver, anything=anything)
            if self.service and self.service.family_messages(unheard_only=True):
                news = self.service.take_family_messages().messages
                said, note = heard_messages(news, locale, session.patient)
                session.notes.append(f"They just heard these family messages: {said} {note}".strip())
                welcome = f"{said} {t(locale, 'reprompt')}"
                if any(m.media == "voice" for m in news):  # play the family's own voice
                    welcome = "<speak>" + " ".join(
                        escape(heard_one(m, locale, session.patient))
                        + (f' <audio src="{escape(media.url(m.id, absolute=True))}"/>' if m.media == "voice" else "")
                        for m in news) + f" {escape(t(locale, 'reprompt'))}</speak>"
                if photo := next((m for m in news if m.media == "photo"), None):
                    self._photos[alexa_id] = (media.url(photo.id, absolute=True), f"From {photo.sender}")
            return speak(welcome, end=False, reprompt=t(locale, "reprompt"))
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
            return await self._reply(alexa_id, session, late, one_shot=False, locale=locale, system=system)
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
        return await self._reply(alexa_id, session, asyncio.ensure_future(session.turn(text)), one_shot, locale, system)

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

    async def _push_reminders(self, system: dict, locale: str) -> str:
        if self.reminders is None:
            return "error"
        return await self.reminders.push(system, self.service.profile(), self.service.reminders(upcoming_only=False), locale)

    def _refresh_reminders(self, system: dict, locale: str) -> None:
        """When the skill opens, quietly bring reminders up to date if the medicines changed (only once
        the person has allowed reminders; never asks)."""
        if self.reminders is None or not system.get("apiAccessToken"):
            return

        async def refresh() -> None:
            try:
                await self.reminders.sync(system, self.service.profile(), self.service.medications(), locale)
                await self._push_reminders(system, locale)  # e.g. ones set from another device
            except Exception:
                log.exception("Reminder refresh failed")

        task = asyncio.create_task(refresh())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _reply(self, alexa_id: str, session: VoiceSession, turn: asyncio.Future, one_shot: bool,
                     locale: str = "en-US", system: dict | None = None) -> dict:
        try:
            result = await asyncio.wait_for(asyncio.shield(turn), REPLY_DEADLINE_S)
        except TimeoutError:
            self._late[alexa_id] = turn
            return speak(t(locale, "still_working"), end=False, reprompt=t(locale, "say_okay"))
        news = " ".join(a.text for a in session.take_announcements())
        reply = f"{news} {result.reply}".strip() if news else result.reply
        log.info("Alexa said %r (%s)", reply, ", ".join(c.name for c in result.tool_calls) or "no tools")
        changed = {"set_reminder", "cancel_reminder"} & {c.name for c in result.tool_calls if not c.is_error}
        if changed and system and system.get("apiAccessToken"):
            if await self._push_reminders(system, locale) == "no_permission" and "set_reminder" in changed:
                return ask_reminders_permission("oneoff")  # saved in Kinvesper; Alexa asks to let the Echo ring
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
    linked = None
    if settings.alexa_account_linking == "required" and settings.oauth_issuer and service:
        tokens = JwtTokenVerifier(settings, allow=signin.token_check(lambda: signin.family_emails(service.profile())))

        async def linked(token: str) -> bool:
            return await tokens.verify_token(token) is not None
    bridge = AlexaBridge(sessions, service, linked)

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
