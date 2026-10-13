"""Echo pill reminders through the Alexa Reminders API (a fake API here)."""

import httpx
import pytest
from conftest import SYSTEM

from kinvesper import voice
from kinvesper.alexa import AlexaBridge
from kinvesper.reminders import EchoReminders, reminder_text
from kinvesper.voice import VoiceSessions


@pytest.fixture
def api(reminders_api):
    return reminders_api


@pytest.fixture
async def bridge(connect, service, api, monkeypatch):
    monkeypatch.setattr(voice.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    b = AlexaBridge(sessions, service)
    b.reminders = EchoReminders(service.store, transport=httpx.MockTransport(api))
    yield b
    await sessions.close_all()


def _intent(name, sid="s1"):
    return {"session": {"sessionId": sid}, "context": {"System": SYSTEM},
            "request": {"type": "IntentRequest", "locale": "en-US", "intent": {"name": name}}}


def _said(reply):
    return reply["response"]["outputSpeech"]["text"]


async def test_one_daily_reminder_per_dose_time(bridge, api, service):
    reply = await bridge.handle(_intent("SetUpRemindersIntent"))
    assert "Every day at 6:30 AM, 8 AM and 8 PM" in _said(reply)
    bodies = sorted(api.reminders.values(), key=lambda b: b["trigger"]["recurrence"]["recurrenceRules"][0])
    assert len(bodies) == 3  # Metformin and Lisinopril share 8 AM
    rules = [b["trigger"]["recurrence"]["recurrenceRules"][0] for b in bodies]
    assert "FREQ=DAILY;BYHOUR=8;BYMINUTE=0;BYSECOND=0;INTERVAL=1;" in rules
    morning = next(b for b in bodies if "BYHOUR=8;" in b["trigger"]["recurrence"]["recurrenceRules"][0])
    text = morning["alertInfo"]["spokenInfo"]["content"][0]["text"]
    assert text.startswith("Margaret, it's time for your") and "Lisinopril" in text and "Metformin" in text
    assert "tell kin vesper I took my pills" in text
    assert morning["trigger"]["timeZoneId"] and "scheduledTime" not in morning["trigger"]


async def test_asks_for_permission_by_voice_then_sets_them(bridge, api):
    api.allowed = False
    ask = await bridge.handle(_intent("SetUpRemindersIntent"))
    directive = ask["response"]["directives"][0]
    assert directive["type"] == "Connections.SendRequest" and directive["name"] == "AskFor"
    assert directive["payload"]["permissionScopes"][0]["permissionScope"] == "alexa::alerts:reminders:skill:readwrite"
    assert "shouldEndSession" not in ask["response"]

    api.allowed = True
    answer = {"session": {"sessionId": "s1"}, "context": {"System": SYSTEM},
              "request": {"type": "Connections.Response", "name": "AskFor", "locale": "en-US",
                          "payload": {"permissionScopes": [{"permissionScope": "x", "status": "ACCEPTED"}]}}}
    assert "Every day at" in _said(await bridge.handle(answer))
    assert len(api.reminders) == 3

    answer["request"]["payload"]["permissionScopes"][0]["status"] = "DENIED"
    assert "no reminders for now" in _said(await bridge.handle(answer))


async def test_medicine_changes_rebuild_the_reminders(bridge, api, service):
    await bridge.handle(_intent("SetUpRemindersIntent"))
    first = set(api.reminders)
    sync = lambda: bridge.reminders.sync(SYSTEM, service.profile(), service.medications(), "en-US")  # noqa: E731
    assert (await sync()).outcome == "unchanged"  # nothing to do on the next launch

    view = await service.settings_view()
    await service.update_settings({**view["profile"], "caregiver_email": "a@b.co"}, view["medications"][:1])
    result = await sync()
    assert result.outcome == "ok" and result.times == ["06:30"]
    assert len(api.reminders) == 1 and not first & set(api.reminders)  # the old ones were deleted

    reply = await bridge.handle(_intent("RemoveRemindersIntent"))
    assert "turned off" in _said(reply) and not api.reminders


async def test_launch_refreshes_quietly(bridge, api):
    launch = {"session": {"sessionId": "s2", "new": True}, "context": {"System": SYSTEM},
              "request": {"type": "LaunchRequest", "locale": "en-US"}}
    await bridge.handle(launch)
    for task in list(bridge._background):
        await task
    assert len(api.reminders) == 3

    api.allowed = False  # never allowed: launching still just greets
    reply = await bridge.handle({**launch, "session": {"sessionId": "s3", "new": True}})
    assert "Kinvesper" in _said(reply)


def test_reminder_text_includes_instructions(service):
    meds = [m for m in service.medications() if m.name == "Metformin"]
    text = reminder_text(service.profile(), meds)
    assert "Metformin, 500 mg" in text and "take with food" in text
