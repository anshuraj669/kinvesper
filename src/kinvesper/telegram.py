"""Telegram alerts through the Kinvesper bot (the bot itself is set up from telegram/ at the repo root).

The caregiver links their Telegram chat from Settings: "Connect Telegram" opens t.me/<bot>?start=<code>,
and pressing Start sends that one-time code here. From then on alerts arrive in that chat with
"I'm on it" and "Status" buttons, and whatever they type is read out on the Echo.

Telegram delivers the bot's updates to /api/telegram/webhook with a secret header (derived from the
bot token, so the setup script and the server agree without another setting). Locally,
telegram/poll.py fetches updates and forwards them to the same endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from html import escape

import httpx

from .config import settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
LEVEL_ICON = {"urgent": "🚨", "warning": "⚠️", "info": "ℹ️"}
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a fake Telegram
_username: str | None = None


def enabled() -> bool:
    return bool(TOKEN.match(settings.telegram_bot_token))


def webhook_secret(token: str | None = None) -> str:
    """The X-Telegram-Bot-Api-Secret-Token value Telegram sends with each update."""
    token = token if token is not None else settings.telegram_bot_token
    return hmac.new(token.encode(), b"kinvesper-telegram-webhook", hashlib.sha256).hexdigest()


def secret_ok(header: str) -> bool:
    return enabled() and bool(header) and hmac.compare_digest(header, webhook_secret())


async def call(method: str, **params) -> dict | None:
    """One Bot API call. Returns its result, or None (logged) when Telegram refuses or can't be reached."""
    if not enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=10, transport=_transport) as http:
            r = await http.post(f"{API}/bot{settings.telegram_bot_token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            log.warning("Telegram %s failed: %s", method, data.get("description", r.status_code))
            return None
        return data.get("result")
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Telegram %s failed: %s", method, type(e).__name__)
        return None


async def bot_username() -> str:
    global _username
    if _username is None and (me := await call("getMe")):
        _username = me.get("username") or ""
    return _username or ""


async def delivered_elsewhere() -> str:
    """The server Telegram delivers this bot's chats to, when it isn't this one (else "").

    Linking only works on the server that receives the Start message: with the webhook on the deployed
    service, a Connect link made by a local server would be answered by the deployed one, which has
    never seen the code. Locally, telegram/poll.py removes the webhook while it relays.
    """
    url = ((await call("getWebhookInfo")) or {}).get("url") or ""
    if not url or url.startswith(settings.base_url.rstrip("/") + "/"):
        return ""
    return url.split("/api/")[0]


async def link_url(code: str) -> str:
    name = await bot_username()
    return f"https://t.me/{name}?start={code}" if name else ""


def buttons(*rows: list[tuple[str, str]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def alert_buttons(alert_id: str) -> dict:
    return buttons([("✅ I'm on it", f"ack:{alert_id}"), ("📋 Status", "status")])


async def send(chat_id: str, html: str, markup: dict | None = None, reply_to: int | None = None) -> bool:
    """A message to a linked chat (Telegram HTML: <b>, <i>, escaped text). True when delivered."""
    if not chat_id:
        return False
    params = {"chat_id": chat_id, "text": html[:4000], "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if markup:
        params["reply_markup"] = markup
    if reply_to:  # shown as a reply to that message in the chat
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return await call("sendMessage", **params) is not None


def alert_html(level: str, title: str, detail: str, about: str) -> str:
    return (f"{LEVEL_ICON.get(level, 'ℹ️')} <b>{escape(title)}</b>\n{escape(detail).strip()}"
            f"\n<i>About {escape(about)}</i>").replace("\n\n<i>", "\n<i>")


def name_of(user: dict) -> str:
    return " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "you"


async def download(file_id: str) -> bytes | None:
    """A file someone sent the bot (a voice note, a photo), by its file_id."""
    info = await call("getFile", file_id=file_id)
    if not info or not info.get("file_path"):
        return None
    try:
        async with httpx.AsyncClient(timeout=20, transport=_transport) as http:
            r = await http.get(f"{API}/file/bot{settings.telegram_bot_token}/{info['file_path']}")
        r.raise_for_status()
        return r.content
    except httpx.HTTPError as e:
        log.warning("Telegram download failed: %s", type(e).__name__)
        return None


async def send_voice(chat_id: str, ogg: bytes, caption: str = "", reply_to: int | None = None) -> bool:
    """A voice note (OGG/Opus) to a linked chat."""
    if not (enabled() and chat_id):
        return False
    data = {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"}
    if reply_to:
        data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
    try:
        async with httpx.AsyncClient(timeout=30, transport=_transport) as http:
            r = await http.post(f"{API}/bot{settings.telegram_bot_token}/sendVoice", data=data,
                                files={"voice": ("answer.ogg", ogg, "audio/ogg")})
        ok = r.json().get("ok", False)
        if not ok:
            log.warning("Telegram sendVoice failed: %s", r.json().get("description", r.status_code))
        return bool(ok)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("Telegram sendVoice failed: %s", type(e).__name__)
        return False
