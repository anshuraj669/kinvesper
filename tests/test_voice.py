"""Phase 3: the voice agent (offline router and Claude loop) and the simulator API."""

import asyncio
import contextlib
import dataclasses
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from kinvesper import mcp_server, voice
from kinvesper.mcp_server import INSTRUCTIONS, build_server
from kinvesper.voice import VoiceSession, is_yes, route

MEDS = [
    {"name": "Lisinopril", "purpose": "blood pressure heart"},
    {"name": "Metformin", "purpose": "diabetes blood sugar"},
    {"name": "Atorvastatin", "purpose": "cholesterol"},
]


@pytest.mark.parametrize(("text", "tool"), [
    ("What pills do I need today?", "get_medication_schedule"),
    ("Did I take my blood pressure pill?", "get_medication_schedule"),
    ("I just took my Metformin", "log_medication_dose"),
    ("A man called, my grandson is in jail and needs gift cards for bail", "check_for_scam"),
    ("I fell in the bathroom", "emergency_help"),
    ("I'm feeling a bit lonely today", "daily_check_in"),
    ("Tell Priya I'm going to the doctor at 3", "notify_caregiver"),
    ("Can you order more of my cholesterol pills?", "request_pharmacy_refill"),
    ("How was Mom's week?", "get_care_summary"),
    ("How was my week?", "get_care_summary"),
])
def test_router_picks_tool(text, tool):
    assert route(text, MEDS, "Priya")[0] == tool


def test_router_arguments():
    assert route("What pills do I need today?", MEDS, "Priya") == ("get_medication_schedule", {})
    assert route("Let Priya know that I'm home", MEDS, "Priya") == ("notify_caregiver", {"message": "I'm home"})
    assert route("I'm feeling dizzy", MEDS, "Priya")[1]["mood"] == "unwell"
    assert route("Please deliver a refill of my thyroid pills", MEDS, "Priya")[1]["fulfillment"] == "delivery"
    assert route("hello there", MEDS, "Priya") is None


def test_yes_no():
    assert is_yes("Yes please") and is_yes("okay, send it")
    assert not is_yes("No, don't") and not is_yes("wait")


# ---------- agent against the real server, in memory ----------


@pytest.fixture
def connect(service, monkeypatch):
    monkeypatch.setattr(mcp_server, "settings", dataclasses.replace(mcp_server.settings, refill_step_seconds=0.02))
    server = build_server(service)

    @contextlib.asynccontextmanager
    async def _connect(elicit):
        async with create_connected_server_and_client_session(server, elicitation_callback=elicit) as session:
            yield session, INSTRUCTIONS

    return lambda elicit: _connect(elicit)


@pytest.fixture
async def offline(connect, monkeypatch):
    monkeypatch.setattr(voice.llm, "enabled", lambda: False)
    session = VoiceSession(connect=connect)
    await session.start()
    yield session
    await session.close()


async def test_offline_turns(offline, service):
    assert offline.mode == "offline" and offline.caregiver == "Priya"

    reply = await offline.turn("What pills do I need today?")
    assert reply.tool_calls[0].name == "get_medication_schedule"
    assert "doses today" in reply.reply

    ask = await offline.turn("Tell Priya I'm going to the doctor")
    assert ask.awaiting_confirmation and "Priya" in ask.reply
    done = await offline.turn("yes")
    assert "sent that to Priya" in done.reply
    assert any(a.kind == "caregiver_message" for a in service.alerts())

    await offline.turn("Tell Priya I'm lonely")
    declined = await offline.turn("no, don't")
    assert "won't send" in declined.reply


async def test_offline_refill_is_announced(offline):
    started = await offline.turn("Can you order more of my cholesterol pills?")
    assert started.tool_calls[0].as_task
    for _ in range(100):
        if announced := offline.take_announcements():
            break
        await asyncio.sleep(0.05)
    assert "ready to pick up" in announced[0].text
    assert offline.notes  # the model hears about it on the next turn


def _block(**kw):
    return SimpleNamespace(**kw)


def _reply(stop, *blocks):
    return SimpleNamespace(stop_reason=stop, content=list(blocks))


async def test_claude_loop_uses_mcp_tools(connect, monkeypatch):
    calls = []

    async def fake_create(**params):
        calls.append(params)
        if len(calls) == 1:
            assert {t["name"] for t in params["tools"]} >= {"get_medication_schedule", "request_pharmacy_refill"}
            assert "Margaret" in params["system"][0]["text"]
            return _reply("tool_use", _block(type="tool_use", id="tu_1", name="get_medication_schedule",
                                             input={"medication": "blood pressure"}))
        result = params["messages"][-1]["content"][0]
        assert result["tool_use_id"] == "tu_1" and "Lisinopril" in result["content"]
        return _reply("end_turn", _block(type="text", text="Your blood pressure pill is Lisinopril."))

    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        out = await session.turn("Did I take my blood pressure pill?")
        assert out.reply == "Your blood pressure pill is Lisinopril."
        assert [c.name for c in out.tool_calls] == ["get_medication_schedule"]
        roles = [m["role"] for m in session.history]
        assert roles == ["user", "assistant", "user", "assistant"]
    finally:
        await session.close()


async def test_claude_loop_waits_for_spoken_confirmation(connect, monkeypatch):
    step = 0

    async def fake_create(**params):
        nonlocal step
        step += 1
        if step == 1:
            return _reply("tool_use", _block(type="tool_use", id="tu_1", name="notify_caregiver",
                                             input={"message": "Running late"}))
        assert json.loads(params["messages"][-1]["content"][0]["content"])["sent"] is True
        return _reply("end_turn", _block(type="text", text="Done, Priya knows."))

    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        ask = await session.turn("Tell Priya I'm running late")
        assert ask.awaiting_confirmation
        done = await session.turn("Yes")
        assert done.reply == "Done, Priya knows."
    finally:
        await session.close()


async def test_claude_refusal_keeps_history_clean(connect, monkeypatch):
    async def fake_create(**_params):
        return _reply("refusal")

    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        out = await session.turn("something odd")
        assert "can't help" in out.reply
        assert session.history == []
    finally:
        await session.close()


async def test_unreachable_llm_falls_back_to_keyword_router(connect, monkeypatch):
    import anthropic
    import httpx2

    async def fake_create(**_params):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "http://ollama.invalid/v1/messages"))

    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        out = await session.turn("Did I take my pills today?")
        assert [c.name for c in out.tool_calls] == ["get_medication_schedule"]
        assert "trouble" not in out.reply
        assert session.history == []
    finally:
        await session.close()


# ---------- simulator HTTP API ----------


def test_simulator_api(server_url):
    base = server_url.removesuffix("/mcp")
    assert httpx.get(f"{base}/voice").status_code == 200

    created = httpx.post(f"{base}/api/voice/sessions").json()
    assert created["mode"] == "offline" and "request_pharmacy_refill" in created["tools"]
    turns = f"{base}/api/voice/sessions/{created['id']}/turns"

    reply = httpx.post(turns, json={"text": "Did I take my blood pressure pill?"}, timeout=30).json()
    assert reply["tool_calls"][0]["name"] == "get_medication_schedule"
    assert httpx.post(turns, json={"text": ""}).status_code == 400
    assert httpx.get(f"{base}/api/voice/sessions/nope/announcements").status_code == 404
    assert httpx.delete(f"{base}/api/voice/sessions/{created['id']}").status_code == 204


def test_simulator_off_with_oauth_and_no_key(auth_server):
    assert httpx.get(f"{auth_server}/voice").status_code == 404
    assert httpx.post(f"{auth_server}/api/voice/sessions").status_code == 404


def test_ollama_provider_uses_local_messages_api(monkeypatch):
    from kinvesper import llm
    from kinvesper.config import Settings

    local = Settings(llm_provider="ollama", ollama_url="http://127.0.0.1:11434")
    monkeypatch.setattr(llm, "settings", local)
    monkeypatch.setattr(llm, "_client", None)
    assert llm.enabled()
    assert local.model_id == "llama3.1"
    assert str(llm.client().base_url).startswith("http://127.0.0.1:11434")


def test_recent_turns_keeps_tool_calls_with_their_results():
    from kinvesper.voice import recent_turns

    history = []
    for i in range(6):
        history += [
            {"role": "user", "content": f"question {i}"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}"}]},
            {"role": "assistant", "content": f"answer {i}"},
        ]
    recent = recent_turns(history, 2)
    assert recent[0] == {"role": "user", "content": "question 4"}
    assert len(recent) == 8
    assert recent_turns(history[:4], 2) == history[:4]
