"""Household settings (edited on the dashboard) and caregiver alerts to the saved phone and email."""

import dataclasses

import httpx
import pytest

from kinvesper import notify
from kinvesper.service import SettingsError
from kinvesper.voice import VoiceSession

TOPIC = "arn:aws:sns:us-east-1:123456789012:alerts"


class FakeSns:
    """Records SNS calls; email subscriptions start pending until confirm() is called."""

    def __init__(self):
        self.published: list[dict] = []
        self.subs: dict[str, str] = {}  # endpoint -> subscription ARN or "PendingConfirmation"

    def publish(self, **kw):
        self.published.append(kw)

    def subscribe(self, TopicArn, Protocol, Endpoint):
        self.subs.setdefault(Endpoint, "PendingConfirmation")

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
    assert len(service.schedule().items) == 3  # the retired medicines are off the schedule, history kept


@pytest.mark.parametrize("profile, meds, error", [
    ({"caregiver_phone": "5550102030"}, None, "country code"),
    ({"caregiver_email": "not-an-email"}, None, "email"),
    ({"caregiver_phone": "", "caregiver_email": ""}, None, "phone number or an email"),
    ({"caregiver_name": " "}, None, "Caregiver name is required"),
    ({}, [{"name": "X", "dose": "1 mg", "times": ["25:00"]}], "HH:MM"),
    ({}, [{"name": "X", "dose": "", "times": ["08:00"]}], "dose"),
    ({}, [{"name": "X", "dose": "1 mg", "times": ["08:00"], "pills_remaining": "lots"}], "whole number"),
    ({}, [], "at least one medicine"),
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
