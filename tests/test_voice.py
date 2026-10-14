"""Phase 3: the voice agent (offline router and Claude loop) and the simulator API."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from kinvesper import voice
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
        assert {t["name"] for t in params["tools"]} >= {"get_medication_schedule", "request_pharmacy_refill"}
        assert "Margaret" in params["system"][0]["text"]
        return _reply("tool_use", _block(type="tool_use", id="tu_1", name="get_medication_schedule",
                                         input={"medication": "blood pressure"}))

    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        out = await session.turn("Did I take my blood pressure pill?")
        # The tool's own spoken reply, from the real schedule: no second model call.
        assert "Lisinopril" in out.reply and len(calls) == 1
        assert [c.name for c in out.tool_calls] == ["get_medication_schedule"]
        roles = [m["role"] for m in session.history]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert session.history[2]["content"][0]["tool_use_id"] == "tu_1"
    finally:
        await session.close()


async def test_claude_loop_waits_for_spoken_confirmation(connect, monkeypatch):
    step = 0

    async def fake_create(**params):
        nonlocal step
        step += 1
        return _reply("tool_use", _block(type="tool_use", id="tu_1", name="notify_caregiver",
                                         input={"message": "Running late"}))

    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        ask = await session.turn("Tell Priya I'm running late")
        assert ask.awaiting_confirmation
        done = await session.turn("Yes")
        assert "sent that to Priya" in done.reply and step == 1
        assert json.loads(session.history[2]["content"][0]["content"])["sent"] is True
    finally:
        await session.close()


async def test_claude_answers_anything_with_web_search(connect, monkeypatch):
    calls = []

    async def fake_create(**params):
        calls.append(params)
        if len(calls) == 1:  # the server paused a long search; sending the turn back resumes it
            assert {"type": "web_search_20250305", "name": "web_search", "max_uses": 1} in params["tools"]
            return _reply("pause_turn", _block(type="server_tool_use", id="srv_1", name="web_search",
                                               input={"query": "Kathmandu weather today"}))
        assert params["messages"][-1]["role"] == "assistant"
        return _reply("end_turn", _block(type="text", text="It's sunny in Kathmandu, "),
                      _block(type="text", text="around 24 degrees."))

    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "server_tools", lambda: [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 1}])
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        out = await session.turn("What's the weather in Kathmandu today?")
        assert out.reply == "It's sunny in Kathmandu, around 24 degrees."
        assert out.tool_calls == [] and len(calls) == 2
        # The search results are dropped from the history: later turns only carry what was said.
        question, answer = session.history
        assert question["content"].endswith("What's the weather in Kathmandu today?")
        assert answer == {"role": "assistant", "content": out.reply}
    finally:
        await session.close()


def test_web_search_only_with_the_claude_api(monkeypatch):
    from kinvesper import llm
    from kinvesper.config import Settings

    for provider, searches in [("anthropic", True), ("bedrock", False), ("ollama", False), ("none", False)]:
        monkeypatch.setattr(llm, "settings", Settings(llm_provider=provider))
        assert bool(llm.server_tools()) is searches


async def test_claude_knows_the_local_time(connect, monkeypatch):
    seen = []

    async def fake_create(**params):
        seen.append(params["messages"][-1]["content"])
        return _reply("end_turn", _block(type="text", text="It's 3:22 PM."))

    monkeypatch.setattr(voice, "now", lambda: datetime(2026, 10, 14, 15, 22))
    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        await session.turn("What is the time right now?")
        assert seen[0].startswith("[Now] 3:22 PM on Wednesday, October 14, 2026\n")
        assert "[Now] line is the current local date and time" in session.system
    finally:
        await session.close()


async def test_offline_router_tells_the_time(offline, monkeypatch):
    monkeypatch.setattr(voice, "now", lambda: datetime(2026, 10, 14, 20, 5))
    out = await offline.turn("Hey, what is the time right now?")
    assert out.reply == "It's 8:05 PM on Wednesday, October 14, 2026." and not out.tool_calls


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

    speech = f"{base}/api/voice/sessions/{created['id']}"
    assert created["server_voice"] is False and created["voice_input"] is False
    assert httpx.post(f"{speech}/speech", json={"text": "Hello"}).status_code == 503  # page uses the browser's voice
    assert httpx.post(f"{speech}/transcribe", content=b"").status_code == 400
    assert httpx.post(f"{speech}/transcribe", content=b"audio").status_code == 422  # speech-to-text is off
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


def test_speech_vocabulary_hint_and_name_correction():
    from kinvesper.speech import Vocabulary

    vocab = Vocabulary("Margaret", "Priya", ("Atorvastatin", "Lisinopril", "Metformin"))
    assert "Margaret" in vocab.hint and "Tell Priya" in vocab.hint and "Atorvastatin, Lisinopril" in vocab.hint
    assert vocab.correct("Tell Pri I'll call her tonight.") == "Tell Priya I'll call her tonight."
    assert vocab.correct("order a refill of lisinopril and metformin") == "order a refill of Lisinopril and Metformin"
    assert vocab.correct("Did I take my atorvastatine?") == "Did I take my Atorvastatin?"
    # Ordinary words that merely start like a name stay as they are.
    assert vocab.correct("I met a friend at the price check") == "I met a friend at the price check"


def test_polly_without_credentials_falls_back_for_good(monkeypatch):
    from botocore.exceptions import NoCredentialsError

    from kinvesper import speech
    from kinvesper.config import Settings

    class NoCreds:
        def synthesize_speech(self, **_):
            raise NoCredentialsError()

    monkeypatch.setattr(speech, "_polly", NoCreds())
    monkeypatch.setattr(speech, "_polly_failed", False)
    monkeypatch.setattr(speech, "settings", Settings(tts_provider="polly"))
    with pytest.raises(speech.SpeechUnavailable):
        speech._synthesize("Hello")
    assert not speech.tts_available()


def test_available_cpus_respects_container_limit(tmp_path):
    from kinvesper.speech import available_cpus

    limit = tmp_path / "cpu.max"
    limit.write_text("100000 100000\n")  # 1 CPU
    assert available_cpus(str(limit)) == 1
    limit.write_text("150000 100000\n")  # 1.5 CPUs rounds up
    assert available_cpus(str(limit)) == min(2, available_cpus(str(tmp_path / "missing")))
    limit.write_text("max 100000\n")  # no limit: every core this process may run on
    assert available_cpus(str(limit)) == available_cpus(str(tmp_path / "missing")) >= 1
