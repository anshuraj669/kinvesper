"""Kinvesper in Hindi: the skill's fixed phrases, Echo reminders, and Claude answering in Hindi."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kinvesper import voice
from kinvesper.alexa import AlexaBridge
from kinvesper.i18n import TEXT, hindi_time
from kinvesper.reminders import EchoReminders, reminder_text, spoken_times
from kinvesper.voice import VoiceSession, VoiceSessions, is_yes

SKILL = Path(__file__).resolve().parent.parent / "alexa" / "skill" / "skill-package"


def test_every_phrase_has_a_hindi_version():
    assert TEXT["hi"].keys() == TEXT["en"].keys()


@pytest.mark.parametrize("hhmm, said", [
    ("06:30", "सुबह 6:30 बजे"), ("08:00", "सुबह 8 बजे"), ("13:00", "दोपहर 1 बजे"),
    ("18:00", "शाम 6 बजे"), ("20:00", "रात 8 बजे"), ("00:15", "रात 12:15 बजे"),
])
def test_hindi_times(hhmm, said):
    assert hindi_time(hhmm) == said


def test_hindi_yes_and_no():
    assert is_yes("हाँ, भेज दो") and is_yes("जी")
    assert not is_yes("नहीं") and not is_yes("जी नहीं, रहने दो")


def test_hindi_reminder(service):
    meds = [m for m in service.medications() if "08:00" in m.times]
    text = reminder_text(service.profile(), meds, "hi-IN")
    assert text.startswith("Margaret, आपकी") and " और " in text and "kin vesper से कहो" in text
    assert spoken_times(["06:30", "20:00"], "hi-IN") == "सुबह 6:30 बजे और रात 8 बजे"


@pytest.fixture
async def bridge(connect, service, reminders_api, monkeypatch):
    monkeypatch.setattr(voice.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    b = AlexaBridge(sessions, service)
    b.reminders = EchoReminders(service.store, transport=httpx.MockTransport(reminders_api))
    yield b
    await sessions.close_all()


async def test_skill_speaks_hindi(bridge, alexa_system):
    launch = {"session": {"sessionId": "h1", "new": True}, "context": {"System": {}},
              "request": {"type": "LaunchRequest", "locale": "hi-IN"}}
    reply = await bridge.handle(launch)
    assert reply["response"]["outputSpeech"]["text"].startswith("नमस्ते Margaret")
    assert reply["response"]["reprompt"]["outputSpeech"]["text"] == "और कुछ?"
    reminders = {**launch, "context": {"System": alexa_system},
                 "request": {"type": "IntentRequest", "locale": "hi-IN", "intent": {"name": "SetUpRemindersIntent"}}}
    said = (await bridge.handle(reminders))["response"]["outputSpeech"]["text"]
    assert said.startswith("हो गया") and "रात 8 बजे" in said
    assert bridge.sessions.get(bridge._by_alexa["h1"]).language == "hi"


async def test_claude_answers_in_hindi(connect, monkeypatch):
    calls = []

    async def fake_create(**params):
        calls.append(params)
        if len(calls) == 1:
            return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(
                type="tool_use", id="tu_1", name="get_medication_schedule", input={})])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="आज आपको पाँच दवाइयाँ लेनी हैं।")])

    monkeypatch.setattr(voice.llm, "enabled", lambda: True)
    monkeypatch.setattr(voice.llm, "create_message", fake_create)
    session = VoiceSession(connect=connect)
    await session.start()
    session.language = "hi"
    try:
        out = await session.turn("आज मुझे कौन सी दवाई लेनी है")
        assert out.reply == "आज आपको पाँच दवाइयाँ लेनी हैं।"
        assert len(calls) == 2  # the English tool reply goes back to Claude to put into Hindi
        assert "Speak Hindi" in calls[0]["system"][1]["text"] and "cache_control" in calls[0]["system"][0]
    finally:
        await session.close()


def test_hindi_skill_package():
    model = json.loads((SKILL / "interactionModels/custom/hi-IN.json").read_text())["interactionModel"]["languageModel"]
    assert model["invocationName"] == "kin vesper"
    intents = {i["name"]: i for i in model["intents"]}
    assert {"CatchAllIntent", "SetUpRemindersIntent", "RemoveRemindersIntent", "AMAZON.YesIntent"} <= intents.keys()
    assert any("रिमाइंडर" in s for s in intents["SetUpRemindersIntent"]["samples"])
    manifest = json.loads((SKILL / "skill.json").read_text())["manifest"]
    assert "hi-IN" in manifest["publishingInformation"]["locales"] and "hi-IN" in manifest["privacyAndCompliance"]["locales"]
    assert manifest["permissions"] == [{"name": "alexa::alerts:reminders:skill:readwrite"}]
