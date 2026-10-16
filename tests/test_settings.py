"""Household settings (edited on the dashboard) and caregiver alerts to the saved phone and email."""

import dataclasses
import json
from datetime import timedelta

import httpx
import pytest

from kinvesper import notify
from kinvesper import service as service_module
from kinvesper.config import now
from kinvesper.service import SettingsError
from kinvesper.voice import VoiceSession

TOPIC = "arn:aws:sns:us-east-1:123456789012:alerts"


class FakeSns:
    """Records SNS calls; email subscriptions start pending until confirm() is called."""

    def __init__(self):
        self.published: list[dict] = []
        self.subs: dict[str, str] = {}  # endpoint -> subscription ARN or "PendingConfirmation"
        self.filters: dict[str, str] = {}  # endpoint -> filter policy

    def publish(self, **kw):
        self.published.append(kw)

    def subscribe(self, TopicArn, Protocol, Endpoint, Attributes):
        self.subs.setdefault(Endpoint, "PendingConfirmation")
        self.filters[Endpoint] = Attributes["FilterPolicy"]

    def confirm(self, endpoint):
        self.subs[endpoint] = f"{TOPIC}:{len(self.subs)}"

    def unsubscribe(self, SubscriptionArn):
        self.subs = {e: a for e, a in self.subs.items() if a != SubscriptionArn}

    def list_subscriptions_by_topic(self, TopicArn, **_):
        return {"Subscriptions": [{"Protocol": "email", "Endpoint": e, "SubscriptionArn": a} for e, a in self.subs.items()]}


@pytest.fixture
def sns(monkeypatch):
    fake = FakeSns()
    monkeypatch.setattr(notify, "_sns", lambda: fake)
    monkeypatch.setattr(notify, "settings", dataclasses.replace(notify.settings, sns_topic_arn=TOPIC))
    return fake


async def _save(service, **profile):
    view = await service.settings_view()
    return await service.update_settings({**view["profile"], **profile}, view["medications"])


async def test_alerts_go_to_the_saved_phone_and_email(service, sns):
    await _save(service, caregiver_name="Sam", caregiver_phone="+1 (555) 010-2030", caregiver_email="sam@example.com")
    assert sns.subs == {"sam@example.com": "PendingConfirmation"}  # Amazon emails Sam a confirmation link
    assert json.loads(sns.filters["sam@example.com"]) == {"audience": ["caregiver"]}
    assert (await service.settings_view())["email_status"] == "pending"
    sns.confirm("sam@example.com")
    assert (await service.settings_view())["email_status"] == "confirmed"

    result = await service.check_scam("Someone from my bank called and asked me to read out my card PIN")
    assert result.risk == "medium" and result.family_alerted  # suspicious, not only certain scams
    assert "Sam" in result.advice
    assert [m.get("TopicArn") or m.get("PhoneNumber") for m in sns.published] == [TOPIC, "+15550102030"]
    assert "Suspicious contact" in sns.published[0]["Subject"]


async def test_changing_the_email_moves_the_subscription(service, sns):
    await _save(service, caregiver_email="old@example.com")
    sns.confirm("old@example.com")
    await _save(service, caregiver_email="new@example.com")
    assert sns.subs == {"new@example.com": "PendingConfirmation"}


async def test_sms_failure_still_emails(service, sns, monkeypatch):
    def publish(**kw):
        if "PhoneNumber" in kw:
            raise RuntimeError("number not verified in the SMS sandbox")
        sns.published.append(kw)

    monkeypatch.setattr(sns, "publish", publish)
    await _save(service, caregiver_phone="+15550102030", caregiver_email="sam@example.com")
    alert = await service.emergency("I fell")
    assert alert.sent and sns.published[0]["TopicArn"] == TOPIC
    assert service.alerts()[0].notified


async def test_medicines_are_edited_and_retired(service):
    view = await service.settings_view()
    meds = view["medications"]
    kept = {**meds[0], "dose": "20 mg", "times": "21:00, 9:00"}
    new = {"name": "Vitamin D", "dose": "1000 IU", "times": ["12:00"], "pills_remaining": 60, "purpose": "bones"}
    out = await service.update_settings({**view["profile"], "caregiver_email": "a@b.co"}, [kept, new])

    names = {m["name"]: m for m in out["medications"]}
    assert set(names) == {meds[0]["name"], "Vitamin D"}
    assert names[meds[0]["name"]]["times"] == ["09:00", "21:00"] and names[meds[0]["name"]]["id"] == meds[0]["id"]
    assert service.find_medication("my bones pill").name == "Vitamin D"
    vitamin = service.find_medication("Vitamin D")
    expected = 3 if vitamin.added_at[11:16] <= "12:00" else 2  # added after noon: its first dose is tomorrow
    assert len(service.schedule().items) == expected  # the retired medicines are off the schedule, history kept


@pytest.mark.parametrize("profile, meds, error", [
    ({"caregiver_phone": "5550102030"}, None, "country code"),
    ({"caregiver_email": "not-an-email"}, None, "email"),
    ({"caregiver_phone": "", "caregiver_email": ""}, None, "phone number, an email or Telegram"),
    ({"caregiver_name": " "}, None, "Caregiver name is required"),
    ({}, [{"name": "X", "dose": "1 mg", "times": ["25:00"]}], "HH:MM"),
    ({}, [{"name": "X", "dose": "", "times": ["08:00"]}], "dose"),
    ({}, [{"name": "X", "dose": "1 mg", "times": ["08:00"], "pills_remaining": "lots"}], "whole number"),
    ({}, [], "at least one medicine"),
    ({"emergency_number": "call 911"}, None, "short local number"),
])
async def test_invalid_settings_are_rejected(service, profile, meds, error):
    view = await service.settings_view()
    before = service.profile()
    with pytest.raises(SettingsError, match=error):
        await service.update_settings({**view["profile"], "caregiver_email": "a@b.co", **profile},
                                      view["medications"] if meds is None else meds)
    assert service.profile() == before


async def test_voice_uses_the_saved_household(service, connect, monkeypatch):
    monkeypatch.setattr("kinvesper.voice.llm.enabled", lambda: False)
    await _save(service, preferred_name="Rosa", caregiver_name="Leo", caregiver_email="leo@example.com")
    session = VoiceSession(connect=connect)
    await session.start()
    try:
        assert (session.patient, session.caregiver) == ("Rosa", "Leo")
        assert "Rosa" in session.system
        out = await session.turn("tell Leo I'm fine")
        assert "Leo" in out.reply
    finally:
        await session.close()


def test_settings_api(fresh_server):
    base = fresh_server
    assert httpx.get(f"{base}/dashboard/settings").status_code == 200
    view = httpx.get(f"{base}/api/settings").json()
    assert view["email_status"] == "no_topic"  # local: alerts are logged
    profile = {**view["profile"], "caregiver_name": "Sam", "caregiver_email": "sam@example.com"}
    saved = httpx.put(f"{base}/api/settings", json={"profile": profile, "medications": view["medications"]}).json()
    assert saved["profile"]["caregiver_name"] == "Sam"
    assert httpx.get(f"{base}/api/dashboard").json()["profile"]["caregiver_name"] == "Sam"

    bad = httpx.put(f"{base}/api/settings", json={"profile": {**profile, "caregiver_phone": "123"}, "medications": []})
    assert bad.status_code == 400 and "country code" in bad.json()["error"]
    assert httpx.put(f"{base}/api/settings", content=b"nope").status_code == 400

    test = httpx.post(f"{base}/api/settings/test-alert").json()
    assert test["sent"] is False  # no SNS locally
    assert httpx.get(f"{base}/api/dashboard").json()["alerts"][0]["kind"] == "test"


def test_settings_need_the_dashboard_key(auth_server):
    assert httpx.get(f"{auth_server}/api/settings").status_code == 404
    assert httpx.put(f"{auth_server}/api/settings", json={}).status_code == 404


# ---------- trusted contacts ----------

BANK = {"name": "State Bank of India", "relation": "bank", "phone": "+91 98412 37777"}


async def _with_contacts(service, *people):
    view = await service.settings_view()
    profile = {**view["profile"], "caregiver_phone": "+919800000001"}
    return await service.update_settings(profile, view["medications"], list(people))


async def test_caller_pretending_to_be_the_bank(service):
    await _with_contacts(service, BANK)
    result = await service.check_scam("Someone from my bank called from 98 4123 9999 to check my account")
    assert result.risk == "high" and result.number_check == "impersonation"
    assert result.trusted_contact.name == "State Bank of India"
    assert "4 1 2 3, 7 7 7 7" in result.advice  # the real number to call back on
    assert result.family_alerted and service.alerts()[0].level == "urgent"


async def test_matching_number_never_lowers_the_risk(service):
    text = "My bank called and wants me to buy gift cards to unlock my account"
    without = await service.check_scam(text)
    await _with_contacts(service, BANK)
    result = await service.check_scam(text, caller_number="nine eight four one two three seven seven seven seven")
    assert result.number_check == "matches" and result.risk == without.risk != "low"
    assert "callers can fake that" in result.advice


async def test_unknown_number_and_family_impersonation(service):
    await _with_contacts(service, {"name": "Ravi", "relation": "grandson", "phone": "+919811112222"})
    unknown = await service.check_scam("A man called from 555 0102 030 about my car warranty")
    assert unknown.number_check == "unknown"
    grandson = await service.check_scam("My grandson called from a new number 98 7654 3210, he needs bail money")
    assert grandson.number_check == "impersonation" and grandson.risk == "high"


async def test_lookup_and_caregiver_is_trusted(service):
    await _with_contacts(service, BANK)
    found = service.find_contact("what's my bank's number")
    assert found.found and found.message.startswith("State Bank of India's number is 9 1 9 8")
    assert service.find_contact("call Priya").contact.id == "caregiver"
    assert not service.find_contact("my plumber").found


async def test_contacts_are_replaced_and_validated(service):
    out = await _with_contacts(service, BANK, {"name": "Dr. Patel", "relation": "doctor", "phone": "01234 567890"})
    assert [c["name"] for c in out["contacts"]] == ["State Bank of India", "Dr. Patel"]
    out = await _with_contacts(service, out["contacts"][1])
    assert [c["name"] for c in out["contacts"]] == ["Dr. Patel"]
    with pytest.raises(SettingsError, match="digits"):
        await _with_contacts(service, {"name": "X", "phone": "call me"})


# ---------- escalation and the weekly report ----------


@pytest.fixture
def clock(monkeypatch):
    state = {"t": now().replace(hour=6, minute=0, second=0, microsecond=0)}  # well before any dose is overdue
    monkeypatch.setattr(service_module, "now", lambda: state["t"])
    return state


async def test_unanswered_urgent_alert_goes_to_the_backup(service, sns, clock):
    await _save(service, caregiver_email="priya@example.com", backup_name="Arun", backup_phone="+919822223333",
                backup_email="arun@example.com")
    assert json.loads(sns.filters["arun@example.com"]) == {"audience": ["backup"]}
    await service.emergency("I fell in the bathroom")
    await service.check_scam("Someone from my bank called and asked me to read out my card PIN")  # a warning
    sns.published.clear()

    clock["t"] += timedelta(minutes=5)
    await service.run_checks()
    assert not sns.published  # still within ESCALATE_AFTER_MIN

    clock["t"] += timedelta(minutes=6)
    await service.run_checks()
    await service.run_checks()  # once only
    to_backup = [m for m in sns.published if m.get("PhoneNumber") == "+919822223333"
                 or m.get("MessageAttributes", {}).get("audience", {}).get("StringValue") == "backup"]
    assert len(to_backup) == 2 and "Arun" in to_backup[0]["Message"] and "fell" in to_backup[0]["Message"]
    assert next(a for a in service.alerts() if a.kind == "emergency").escalated_at


async def test_acknowledged_alert_is_not_escalated(service, sns, clock):
    await _save(service, caregiver_email="priya@example.com", backup_name="Arun", backup_email="arun@example.com")
    alert = (await service.emergency("chest pain")).alert_id
    service.acknowledge(alert)
    clock["t"] += timedelta(minutes=30)
    sns.published.clear()
    await service.run_checks()
    assert not sns.published


async def test_weekly_report_on_the_report_day(service, sns, clock):
    await _save(service, caregiver_email="priya@example.com")
    sunday = clock["t"] + timedelta(days=(6 - clock["t"].weekday()))
    clock["t"] = sunday.replace(hour=17, minute=0)
    await service.run_checks()
    assert not any(a.kind == "report" for a in service.alerts())
    clock["t"] = sunday.replace(hour=18, minute=5)
    await service.run_checks()
    await service.run_checks()
    reports = [a for a in service.alerts() if a.kind == "report"]
    assert len(reports) == 1 and reports[0].notified
    email = next(m for m in sns.published if m.get("Subject", "").startswith("Kinvesper weekly report"))
    assert "Doses taken:" in email["Message"] and "Suspicious calls" in email["Message"]
    assert "PhoneNumber" not in email


async def test_emergency_advice_uses_the_local_number(service):
    await _save(service, caregiver_email="a@b.co", emergency_number="112")
    result = await service.emergency("I fell")
    assert "call 112" in result.message and "911" not in result.message
