"""Echo pill reminders through the Alexa Reminders API (a fake API here)."""

from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from conftest import SYSTEM

from kinvesper import voice
from kinvesper.alexa import AlexaBridge
from kinvesper.config import settings
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
                          "payload": {"permissionScopes": [{"permissionScope": "alexa::alerts:reminders:skill:readwrite", "status": "ACCEPTED"}]}}}
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


# ---------- one-off reminders and important alarms ----------


@pytest.fixture
def clock(monkeypatch):
    from kinvesper import reminders, service as service_module, voice as voice_module
    state = {"t": datetime(2026, 10, 14, 14, 0, tzinfo=settings.tz)}
    for module in (service_module, reminders, voice_module):
        monkeypatch.setattr(module, "now", lambda: state["t"])
    return state


async def test_one_off_reminder(service, clock):
    out = await service.add_reminder("call Priya", "2026-10-14T17:00")
    assert out.message == "Okay, I'll remind you at 5 PM today: call Priya."
    assert service.list_reminders().message == "You have 1 reminder: call Priya at 5 PM today."
    bad = await service.add_reminder("call Priya", "2026-10-14T09:00")
    assert "already passed" in bad.message and bad.reminder is None
    assert "time" in (await service.add_reminder("x", "tomorrowish")).message


async def test_reminder_in_a_minute(service, clock):
    """'In a minute' counts from the real clock, not the [Now] line, which shows no seconds."""
    clock["t"] = clock["t"].replace(second=45)
    exact = await service.add_reminder("drink water", in_minutes=10)
    assert exact.reminder.at.startswith("2026-10-14T14:10") and "at 2:10 PM today" in exact.message
    rounded = await service.add_reminder("stretch", "2026-10-14T14:01")  # 15 seconds away
    assert rounded.reminder is not None and "2:01 PM" in rounded.message
    late = await service.add_reminder("stretch", "2026-10-14T14:00")  # this minute, already begun
    assert late.reminder is not None
    short = await service.add_reminder("doctor", "2026-10-14T14:20", important=True)
    assert "30 minutes before" not in short.message and "let Priya know" in short.message


async def test_cancel_by_what_it_is_about(service, clock):
    await service.add_reminder("call Priya", "2026-10-14T17:00")
    await service.add_reminder("doctor's appointment", "2026-10-16T10:00", important=True)
    out = service.cancel_reminder("cancel the doctor reminder")
    assert out.reminder.text == "doctor's appointment" and out.reminder.status == "cancelled"
    assert [r.text for r in service.reminders()] == ["call Priya"]


async def test_important_alarm_tells_the_caregiver_when_due(service, clock):
    out = await service.add_reminder("doctor's appointment", "2026-10-14T15:00", important=True)
    assert "important alarm" in out.message and "Priya" in out.message
    clock["t"] = datetime(2026, 10, 14, 15, 2, tzinfo=settings.tz)
    await service.run_checks()
    await service.run_checks()
    alerts = [a for a in service.alerts() if a.kind == "reminder"]
    assert len(alerts) == 1 and "doctor's appointment" in alerts[0].title
    assert not service.reminders()  # done


async def test_echo_gets_reminder_heads_up_and_ringing_timer(bridge, api, service, clock):
    await service.add_reminder("call Priya", "2026-10-14T17:00")
    await service.add_reminder("doctor's appointment", "2026-10-14T15:30", important=True)
    await service.add_reminder("flight to Delhi", "2026-10-15T09:00", important=True)
    assert await bridge.reminders.push(SYSTEM, service.profile(), service.reminders(), "en-US") == "ok"

    spoken = sorted((b["trigger"]["scheduledTime"], b["alertInfo"]["spokenInfo"]["content"][0]["text"])
                    for b in api.reminders.values())
    assert spoken == [
        ("2026-10-14T15:00:00.000", "Margaret, in 30 minutes it'll be time for your doctor's appointment."),
        ("2026-10-14T15:30:00.000", "Margaret, this is an important reminder. It's time for your doctor's appointment."),
        ("2026-10-14T17:00:00.000", "Margaret, it's time to call Priya."),
        ("2026-10-15T08:30:00.000", "Margaret, in 30 minutes it'll be time for your flight to Delhi."),
        ("2026-10-15T09:00:00.000", "Margaret, this is an important reminder. It's time for your flight to Delhi."),
    ]
    (timer,) = api.timers.values()  # only the alarm within 2 hours rings until stopped
    assert timer["duration"] == "PT5400S" and timer["triggeringBehavior"]["notificationConfig"]["playAudible"]

    await bridge.reminders.push(SYSTEM, service.profile(), service.reminders(), "en-US")
    assert len(api.reminders) == 5  # pushed once
    service.cancel_reminder("the doctor")
    await bridge.reminders.push(SYSTEM, service.profile(), service.reminders(upcoming_only=False), "en-US")
    assert len(api.reminders) == 3 and not api.timers


async def test_voice_reminder_asks_permission_then_rings(bridge, api, service, clock, monkeypatch):
    monkeypatch.setattr(voice.llm, "enabled", lambda: False)
    api.allowed = False
    said = {"session": {"sessionId": "r1"}, "context": {"System": SYSTEM},
            "request": {"type": "IntentRequest", "locale": "en-US", "intent": {
                "name": "CatchAllIntent", "slots": {"utterance": {"value": "remind me to call Priya at 5 PM"}}}}}
    ask = await bridge.handle(said)
    directive = ask["response"]["directives"][0]
    assert directive["token"] == "oneoff"
    assert [s["permissionScope"] for s in directive["payload"]["permissionScopes"]] == [
        "alexa::alerts:reminders:skill:readwrite", "alexa::alerts:timers:skill:readwrite"]
    assert [r.text for r in service.reminders()] == ["call Priya"]  # saved even before permission

    api.allowed = True
    answer = {"session": {"sessionId": "r1"}, "context": {"System": SYSTEM},
              "request": {"type": "Connections.Response", "name": "AskFor", "token": "oneoff", "locale": "en-US",
                          "payload": {"permissionScopes": [
                              {"permissionScope": "alexa::alerts:reminders:skill:readwrite", "status": "ACCEPTED"}]}}}
    assert "set on this Echo" in (await bridge.handle(answer))["response"]["outputSpeech"]["text"]
    assert len(api.reminders) == 1


async def test_timer_permission_missing_still_sets_the_reminders(bridge, api, service, clock):
    api.timers_allowed = False
    await service.add_reminder("take the cake out", "2026-10-14T14:40", important=True)
    assert await bridge.reminders.push(SYSTEM, service.profile(), service.reminders(), "en-US") == "ok"
    assert len(api.reminders) == 2 and not api.timers  # the heads-up and the alarm still play; no ringing timer


def test_simulator_rings_what_the_echo_would(service, monkeypatch):
    """The voice simulator has no Echo: due dose times, reminders and alarms ring on the page, once."""
    from datetime import timedelta

    from kinvesper import web
    from kinvesper.models import Reminder

    current = datetime(2026, 10, 15, 8, 1, tzinfo=settings.tz)
    monkeypatch.setattr(web, "now", lambda: current)
    service.store.save("reminder", Reminder(id="rem_call", text="drink water", at=(current - timedelta(minutes=1)).isoformat()))
    service.store.save("reminder", Reminder(id="rem_dr", text="see the doctor", important=True,
                                            at=(current + timedelta(minutes=29)).isoformat()))
    service.store.save("reminder", Reminder(id="rem_off", text="nothing", status="cancelled", at=current.isoformat()))
    session = SimpleNamespace(language="en", rung=set())

    rings = web.due_rings(service, session)
    texts = [(a.ring, a.text) for a in rings]
    assert any(ring == "reminder" and "time for your" in text and "Metformin" in text for ring, text in texts)  # 8 AM dose
    assert ("reminder", "Margaret, it's time to drink water.") in texts
    assert ("reminder", "Margaret, in 30 minutes it'll be time to see the doctor.") in texts
    assert not any("nothing" in text for _, text in texts)
    assert web.due_rings(service, session) == []  # each rings once

    current += timedelta(minutes=29)
    assert [(a.ring, a.text) for a in web.due_rings(service, session)] == [
        ("alarm", "Margaret, this is an important reminder. It's time to see the doctor.")]


def test_reminders_are_said_back_to_the_person():
    from kinvesper.i18n import action
    assert action("drink water") == "to drink water"
    assert action("take my blood pressure medicine") == "to take your blood pressure medicine"
    assert action("call Priya and tell her I am fine") == "to call Priya and tell her you are fine"
    assert action("doctor's appointment") == "for your doctor's appointment"
    assert action("the plumber") == "for the plumber"
