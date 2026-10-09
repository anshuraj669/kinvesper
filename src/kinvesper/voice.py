"""Voice agent behind the Alexa+-style simulator.

Each browser conversation is a VoiceSession holding its own MCP client connection to Kinvesper's
/mcp endpoint, so the simulator uses exactly the protocol surface Alexa+ would. The brain is Claude
(Bedrock or the Claude API, or a local model through Ollama) running a tool-use loop over the MCP tools, or, with LLM_PROVIDER=none,
a keyword router that picks one tool per utterance.

MCP features surface in the conversation:
- Elicitation: a confirmation request from the server becomes a spoken question; the next utterance
  ("yes" / "no") answers it.
- Tasks: task-capable tools are called as tasks; the result is announced when the task finishes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
import warnings
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx
from mcp import ClientSession
from mcp.client.session import ElicitationFnT
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.shared.experimental.tasks.helpers import MODEL_IMMEDIATE_RESPONSE_KEY
from mcp.types import CallToolResult, ElicitResult
from pydantic import BaseModel

from . import llm
from .config import settings
from .scam import rule_assess

log = logging.getLogger(__name__)
warnings.filterwarnings("ignore", "The experimental tasks API is deprecated", DeprecationWarning)

MAX_STEPS = 8
LOCAL_HISTORY_TURNS = 4  # local models have small context windows: send only the last few exchanges
# Free-text arguments the server scans for symptoms and scam signs. Small local models sometimes leave
# them empty, so the person's own words are passed instead.
SPOKEN_TEXT_ARGS = {"daily_check_in": "notes", "check_for_scam": "description"}
TURN_TIMEOUT_S = 90
CONFIRM_TIMEOUT_S = 120
SESSION_IDLE_S = 30 * 60
MAX_SESSIONS = 50

VOICE_RULES = """\
You are the Alexa+ voice assistant with the Kinvesper skill, talking with {name} at home.
Everything you write is spoken aloud by a speaker:
- Reply in one to three short, warm sentences. No markdown, lists, emoji or headings.
- Use the tools for anything about medications, doses, scams, feelings, family or refills. Never
  guess medical facts or the medication schedule.
- Prefer the tool's `message` or `spoken_summary`, in your own gentle words.
- Lines starting with [Update] are background events (like a finished refill). Mention them briefly.
"""

# Opens an initialized MCP client session; yields it with the server's instructions.
Connect = Callable[[ElicitationFnT], AbstractAsyncContextManager[tuple[ClientSession, str]]]


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any]
    spoken: str = ""
    is_error: bool = False
    as_task: bool = False


class TurnResult(BaseModel):
    reply: str
    tool_calls: list[ToolCall] = []
    awaiting_confirmation: bool = False


class Announcement(BaseModel):
    text: str
    tool_calls: list[ToolCall] = []


def recent_turns(history: list[dict], turns: int) -> list[dict]:
    """The history from the start of the last `turns` user utterances (never splitting a tool call from its result)."""
    starts = [i for i, m in enumerate(history) if m["role"] == "user" and isinstance(m["content"], str)]
    return history[starts[-turns]:] if len(starts) > turns else history


def spoken_text(data: dict | None) -> str:
    data = data or {}
    for key in ("message", "spoken_summary", "advice", "narrative"):
        if data.get(key):
            return str(data[key])
    return ""


# ---------------- offline keyword router ----------------

EMERGENCY = re.compile(r"\b(i fell|fallen|fell down|chest pain|can'?t breathe|emergency|help me|hurt myself|bleeding)\b")
NOTIFY = re.compile(r"\b(?:tell|message|text|let)\s+(\w+)(?:\s+know)?(?:\s+that)?\s*(.*)", re.S)
REFILL = re.compile(r"\b(refill|more of my|run(?:ning)? out|order (?:more|my)|new prescription)\b")
TOOK = re.compile(r"\b(i (?:just )?took|i'?ve (?:just )?taken|i have taken|just had my)\b")
SCHEDULE = re.compile(r"\b(pills?|medicines?|medications?|meds|tablets?|dose|what'?s next)\b")
SUMMARY = re.compile(r"\b(summary|report|how was (?:mom|dad|her|his|my|the)(?:'s)? week|this week)\b")
FEELING = re.compile(r"\b(feel|feeling|lonely|sad|tired|dizzy|sick|unwell|great|wonderful|okay|slept)\b")
YES = re.compile(r"\b(yes|yeah|yep|sure|please do|go ahead|send it|do it|ok(ay)?|correct|right)\b")
NO = re.compile(r"\b(no|nope|don'?t|do not|cancel|stop|never ?mind|wait)\b")
MOODS = [
    ("unwell", r"dizzy|sick|unwell|pain|hurt|fell|nause|terrible|awful"),
    ("low", r"lonely|sad|down|tired|low|blue|not great"),
    ("great", r"great|wonderful|fantastic|excellent"),
    ("good", r"good|fine|well|better"),
]


def is_yes(text: str) -> bool:
    t = text.lower()
    return bool(YES.search(t)) and not NO.search(t)


def _mentions_med(text: str, meds: list[dict]) -> bool:
    words = set(re.findall(r"[a-z]+", text))
    for m in meds:
        if m["name"].lower() in text or words & (set(m.get("purpose", "").lower().split()) - {"blood"}):
            return True
    return False


def route(text: str, meds: list[dict], caregiver: str) -> tuple[str, dict] | None:
    """Pick one tool and its arguments for an utterance. None means small talk / unknown."""
    t = text.lower().strip()
    if EMERGENCY.search(t):
        return "emergency_help", {"description": text}
    if (n := NOTIFY.search(t)) and n.group(1) in {caregiver.lower(), "family", "my"}:
        body = text[n.start(2):].strip(" .") if n.group(2) else text
        return "notify_caregiver", {"message": body[:1].upper() + body[1:] if body else text}
    if REFILL.search(t):
        return "request_pharmacy_refill", {"medication": text, "fulfillment": "delivery" if "deliver" in t else "pickup"}
    if rule_assess(t)[2] >= 2:
        return "check_for_scam", {"description": text}
    if TOOK.search(t):
        return "log_medication_dose", {"medication": text}
    if SUMMARY.search(t):
        return "get_care_summary", {"days": 7}
    if SCHEDULE.search(t) or _mentions_med(t, meds):
        return "get_medication_schedule", {"medication": text} if _mentions_med(t, meds) else {}
    if FEELING.search(t):
        mood = next((m for m, pat in MOODS if re.search(pat, t)), "okay")
        return "daily_check_in", {"mood": mood, "notes": text}
    return None


# ---------------- session ----------------


def http_connect(elicit: ElicitationFnT) -> AbstractAsyncContextManager[tuple[ClientSession, str]]:
    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[tuple[ClientSession, str]]:
        headers = {"Authorization": f"Bearer {settings.service_token}"} if settings.service_token else {}
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=300)) as http:
            async with streamable_http_client(f"http://127.0.0.1:{settings.port}/mcp", http_client=http) as (r, w, _):
                async with ClientSession(r, w, elicitation_callback=elicit) as session:
                    init = await session.initialize()
                    yield session, init.instructions or ""

    return connect()


@dataclass
class _Pending:
    question: str
    answer: asyncio.Future[bool]


@dataclass
class VoiceSession:
    connect: Connect = http_connect
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    mode: str = field(default_factory=lambda: "claude" if llm.enabled() else "offline")
    last_used: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.mcp: ClientSession | None = None
        self.system = ""
        self.tools: list[dict] = []
        self.task_tools: set[str] = set()
        self._utterance = ""
        self.meds: list[dict] = []
        self.caregiver = "family"
        self.history: list[dict] = []
        self.notes: list[str] = []  # background updates for the model, sent with the next user turn
        self.announcements: list[Announcement] = []
        self._calls: list[ToolCall] = []
        self._pending: _Pending | None = None
        self._pending_event = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._error: BaseException | None = None
        self._runner: asyncio.Task | None = None

    # ----- connection lifecycle -----

    async def start(self) -> None:
        """Open the MCP connection in a long-lived task (its context managers must enter and exit there)."""
        self._runner = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._error:
            raise self._error

    async def _run(self) -> None:
        try:
            async with self.connect(self._on_elicit) as (session, instructions):
                self.mcp = session
                listed = (await session.list_tools()).tools
                self.tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in listed]
                self.task_tools = {t.name for t in listed if t.execution and t.execution.taskSupport in ("optional", "required")}
                profile = json.loads((await session.read_resource("care://profile")).contents[0].text)
                self.meds = json.loads((await session.read_resource("care://medications")).contents[0].text)
                self.caregiver = profile["caregiver_name"]
                self.system = VOICE_RULES.format(name=profile["preferred_name"]) + "\n" + instructions
                self._ready.set()
                await self._closed.wait()
        except BaseException as e:  # surfaced to start(), or logged if the connection drops later
            self._error = e
            if self._ready.is_set():
                log.warning("Voice session %s lost its MCP connection: %s", self.id, e)
        finally:
            self.mcp = None
            self._ready.set()

    async def close(self) -> None:
        for t in [self._worker, *self._background]:
            if t and not t.done():
                t.cancel()
        self._closed.set()
        if self._runner:
            with contextlib.suppress(BaseException):
                await self._runner

    # ----- turns -----

    async def turn(self, text: str) -> TurnResult:
        self.last_used = time.monotonic()
        async with self._lock:
            if self.mcp is None:
                return TurnResult(reply="Sorry, I lost my connection to Kinvesper. Please reload the page.")
            if self._pending:  # this utterance answers the server's confirmation question
                self._pending.answer.set_result(is_yes(text))
                self._pending = None
                self._pending_event.clear()
            else:
                self._calls = []
                self._worker = asyncio.create_task(self._respond(text))
            return await self._wait()

    async def _wait(self) -> TurnResult:
        asked = asyncio.create_task(self._pending_event.wait())
        try:
            await asyncio.wait({self._worker, asked}, timeout=TURN_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
        finally:
            asked.cancel()
        if self._worker.done():
            try:
                reply = self._worker.result()
            except Exception:
                log.exception("Voice turn failed")
                reply = "Sorry, something went wrong on my end. Could you say that again?"
            return TurnResult(reply=reply, tool_calls=self._calls)
        if self._pending:
            return TurnResult(reply=self._pending.question, tool_calls=self._calls, awaiting_confirmation=True)
        self._worker.cancel()
        return TurnResult(reply="Sorry, that's taking too long. Let's try again.", tool_calls=self._calls)

    async def _on_elicit(self, _ctx, params) -> ElicitResult:
        """Server asks for confirmation: speak the question and wait for the next utterance."""
        schema = getattr(params, "requestedSchema", None) or {}
        flags = [k for k, v in schema.get("properties", {}).items() if v.get("type") == "boolean"]
        if not flags:
            return ElicitResult(action="decline")
        answer = asyncio.get_running_loop().create_future()
        self._pending = _Pending(params.message, answer)
        self._pending_event.set()
        try:
            yes = await asyncio.wait_for(answer, CONFIRM_TIMEOUT_S)
        except TimeoutError:
            self._pending = None
            self._pending_event.clear()
            return ElicitResult(action="cancel")
        return ElicitResult(action="accept", content=dict.fromkeys(flags, True)) if yes else ElicitResult(action="decline")

    async def _respond(self, text: str) -> str:
        if self.mode == "claude":
            return await self._respond_claude(text)
        return await self._respond_offline(text)

    async def _respond_offline(self, text: str) -> str:
        choice = route(text, self.meds, self.caregiver)
        if choice is None:
            return (f"I can help with your medicines, check a suspicious call, tell {self.caregiver} something, "
                    "or order a refill. What would you like?")
        _, call = await self._invoke(*choice)
        return call.spoken or "Done."

    async def _respond_claude(self, text: str) -> str:
        notes, self.notes = self.notes, []
        self.history.append({"role": "user", "content": "\n".join([*(f"[Update] {n}" for n in notes), text])})
        system = [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}]
        self._utterance = text
        start = len(self.history) - 1
        for step in range(MAX_STEPS):
            messages = recent_turns(self.history, LOCAL_HISTORY_TURNS) if llm.local() else self.history
            try:
                response = await llm.create_message(system=system, tools=self.tools, messages=messages, max_tokens=4096)
            except anthropic.APIError as e:
                log.warning("LLM call failed: %s", e)
                del self.history[start:]  # drop this whole turn; earlier history is untouched
                if step == 0:  # nothing has run yet: answer with the keyword router instead
                    return await self._respond_offline(text)
                return "Sorry, I'm having trouble thinking right now. Please try again in a moment."
            if response.stop_reason == "refusal":
                self.history.pop()
                return "I'm sorry, I can't help with that one."
            self.history.append({"role": "assistant", "content": response.content})
            uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not uses:
                return llm.text_of(response) or "Okay."
            done = await asyncio.gather(*(self._tool_result(u) for u in uses))
            self.history.append({"role": "user", "content": [result for result, _ in done]})
            spoken = [call.spoken for _, call in done]
            if llm.local() and all(spoken):
                # Every tool returns a spoken reply built from the real data. A local model skips its slow
                # second call, and can't misstate doses or claim actions it didn't take.
                reply = " ".join(spoken)
                self.history.append({"role": "assistant", "content": reply})
                return reply
        return "Sorry, that took me too many steps. Could you ask me again?"

    async def _tool_result(self, use) -> tuple[dict, ToolCall]:
        args = dict(use.input) if isinstance(use.input, dict) else {}
        key = SPOKEN_TEXT_ARGS.get(use.name)
        if key and llm.local() and not str(args.get(key) or "").strip():
            args[key] = self._utterance
        content, call = await self._invoke(use.name, args)
        return {"type": "tool_result", "tool_use_id": use.id, "content": content, "is_error": call.is_error}, call

    async def _invoke(self, name: str, args: dict) -> tuple[str, ToolCall]:
        """Call an MCP tool (as a task when it supports one). Returns model-facing content and a trace."""
        call = ToolCall(name=name, arguments=args, as_task=name in self.task_tools)
        self._calls.append(call)
        try:
            if call.as_task:
                created = await self.mcp.experimental.call_tool_as_task(name, args)
                call.spoken = (created.meta or {}).get(MODEL_IMMEDIATE_RESPONSE_KEY) or "I've started on that."
                task = asyncio.create_task(self._follow_task(created.task.taskId, name, args))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
                return json.dumps({"status": "started", "task_id": created.task.taskId, "message": call.spoken}), call
            result = await self.mcp.call_tool(name, args)
        except McpError as e:
            call.is_error, call.spoken = True, e.error.message
            return e.error.message, call
        return self._record(call, result), call

    @staticmethod
    def _record(call: ToolCall, result: CallToolResult) -> str:
        call.is_error = bool(result.isError)
        call.spoken = spoken_text(result.structuredContent)
        if result.structuredContent is not None:
            return json.dumps(result.structuredContent)
        return "\n".join(getattr(c, "text", "") for c in result.content)

    async def _follow_task(self, task_id: str, name: str, args: dict) -> None:
        """Poll a task to completion, then announce the result and tell the model on its next turn."""
        async for status in self.mcp.experimental.poll_task(task_id):
            last = status
        call = ToolCall(name=name, arguments=args, as_task=True)
        if last.status == "completed":
            self._record(call, await self.mcp.experimental.get_task_result(task_id, CallToolResult))
        else:
            call.is_error, call.spoken = True, f"Sorry, that didn't finish: {last.statusMessage or last.status}."
        self.announcements.append(Announcement(text=call.spoken, tool_calls=[call]))
        self.notes.append(f"{name} finished: {call.spoken}")

    def take_announcements(self) -> list[Announcement]:
        self.last_used = time.monotonic()
        out, self.announcements = self.announcements, []
        return out


class VoiceSessions:
    def __init__(self, connect: Connect = http_connect):
        self.connect = connect
        self._sessions: dict[str, VoiceSession] = {}
        self._warm_up: asyncio.Task | None = None

    async def create(self) -> VoiceSession:
        await self.expire()
        if llm.local() and (self._warm_up is None or self._warm_up.done()):
            self._warm_up = asyncio.create_task(llm.warm_up())  # load the model while the page greets the user
        if len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.last_used)
            await self.close(oldest.id)
        session = VoiceSession(connect=self.connect)
        await session.start()
        self._sessions[session.id] = session
        return session

    def get(self, sid: str) -> VoiceSession | None:
        return self._sessions.get(sid)

    async def close(self, sid: str) -> None:
        if session := self._sessions.pop(sid, None):
            await session.close()

    async def expire(self) -> None:
        cutoff = time.monotonic() - SESSION_IDLE_S
        for sid in [s.id for s in self._sessions.values() if s.last_used < cutoff]:
            await self.close(sid)

    async def close_all(self) -> None:
        if self._warm_up:
            self._warm_up.cancel()
        for sid in list(self._sessions):
            await self.close(sid)
