"""Interaction checks, prescription photos, Ask Kinvesper, the daily check-in, phone calls, repeated
questions, family messages, companionship, the doctor summary and the Echo Show screen."""

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from conftest import SYSTEM

from kinvesper import interactions, llm, notify, voice, web
from kinvesper import service as service_module
from kinvesper.alexa import AlexaBridge
from kinvesper.config import now
from kinvesper.service import SettingsError
from kinvesper.voice import VoiceSessions, route

LISINOPRIL = {"openfda": {"generic_name": ["LISINOPRIL"], "brand_name": ["Zestril"], "substance_name": ["LISINOPRIL"],
                          "pharm_class_epc": ["Angiotensin Converting Enzyme Inhibitor [EPC]"]},
              "drug_interactions": ["7.3 Non-Steroidal Anti-Inflammatory Agents Including Selective COX-2 Inhibitors: "
                                    "co-administration of NSAIDs with ACE inhibitors may result in deterioration of renal function."]}
IBUPROFEN = {"openfda": {"generic_name": ["IBUPROFEN"], "brand_name": ["Advil"], "substance_name": ["IBUPROFEN"],
                         "pharm_class_epc": ["Nonsteroidal Anti-inflammatory Drug [EPC]"]},
             "ask_doctor_or_pharmacist": ["Ask a doctor or pharmacist before use if you are taking any other drug."]}
METFORMIN = {"openfda": {"generic_name": ["METFORMIN HYDROCHLORIDE"], "brand_name": ["Glucophage"],
                         "substance_name": ["METFORMIN HYDROCHLORIDE"]},
             "drug_interactions": ["Carbonic anhydrase inhibitors may increase the risk of lactic acidosis."]}
LABELS = {"lisinopril": LISINOPRIL, "ibuprofen": IBUPROFEN, "advil": IBUPROFEN, "metformin": METFORMIN}


def fake_openfda(request: httpx.Request) -> httpx.Response:
    query = request.url.params.get("search", "").split('"')[1].lower()
    if query in LABELS:
        return httpx.Response(200, json={"results": [LABELS[query]]})
    return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})


@pytest.fixture(autouse=True)
def offline_fda(monkeypatch):
    """openFDA in memory, Claude off, and no label cache carried between tests."""
    monkeypatch.setattr(interactions, "_cache", {})
    real = interactions.check

    async def check(new, current, transport=None):
        return await real(new, current, transport=transport or httpx.MockTransport(fake_openfda))

    monkeypatch.setattr(interactions, "check", check)
    monkeypatch.setattr(llm, "enabled", lambda: False)


@pytest.fixture
def clock(monkeypatch):
    state = {"t": now().replace(hour=9, minute=0, second=0, microsecond=0)}
    monkeypatch.setattr(service_module, "now", lambda: state["t"])
    return state


@pytest.fixture
def calls(monkeypatch):
    placed = []

    async def call(phone, text, ack_url=""):
        placed.append((phone, text))
        return True

    monkeypatch.setattr(notify, "call", call)
    return placed


# ---------- interactions ----------

async def test_painkiller_against_their_medicines(service):
    result = await service.check_interactions("ibuprofen")
    assert [i.medicines for i in result.interactions] == [["ibuprofen", "Lisinopril"]]
    assert result.message.startswith("Please check with your pharmacist before taking ibuprofen with your Lisinopril.")
    alert = next(a for a in service.alerts() if a.kind == "interaction")
    assert "ibuprofen" in alert.title and not alert.notified  # on the dashboard, not texted


async def test_no_finding_is_never_called_safe(service):
    result = await service.check_interactions("metformin", with_medicine="Lisinopril")
    assert not result.interactions and "Still, it's best to check with your pharmacist" in result.message
    unknown = await service.check_interactions("moonbeam extract")
    assert "couldn't find" in unknown.message and "moonbeam extract" in unknown.not_found


async def test_new_medicine_in_settings_is_checked(service):
    result = await service.check_new_medicines(["Advil"])
    assert result.interactions and any(a.title == "Possible interaction: Advil and Lisinopril" for a in service.alerts())


async def test_claude_reads_the_labels(service, monkeypatch):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    seen = {}

    async def structured(system, content, schema, max_tokens=0):
        seen["prompt"] = content
        return {"interactions": [
            {"medicines": ["ibuprofen", "Lisinopril"], "severity": "serious", "explanation": "It can harm the kidneys."},
            {"medicines": ["ibuprofen", "Aspirin"], "severity": "minor", "explanation": "Not a pair we asked about."}]}

    monkeypatch.setattr(llm, "structured", structured)
    result = await service.check_interactions("ibuprofen")
    assert "- ibuprofen + Lisinopril" in seen["prompt"] and "renal function" in seen["prompt"]
    assert len(result.interactions) == 1  # pairs nobody asked about are dropped
    assert result.message.startswith("Please don't take ibuprofen with your Lisinopril until you've checked")


# ---------- prescription photos ----------

async def test_scan_needs_claude(service):
    with pytest.raises(SettingsError, match="needs Claude"):
        await service.scan_prescription(b"jpeg", "image/jpeg")


async def test_scan_reads_medicines_for_review(service, monkeypatch):
    monkeypatch.setattr(llm, "enabled", lambda: True)
    sent = {}

    async def structured(system, content, schema, max_tokens=0):
        if "interactions" in schema["properties"]:  # the interaction check that follows the scan
            return {"interactions": [{"medicines": ["Advil", "Lisinopril"], "severity": "serious", "explanation": "Kidneys."}]}
        sent["content"] = content
        return {"medicines": [
            {"name": "Advil", "dose": "200 mg", "purpose": "pain", "times": ["8:00", "20:00", "noon"],
             "instructions": "take with food", "pills_remaining": 20},
            {"name": "", "dose": "", "purpose": "", "times": [], "instructions": "", "pills_remaining": 0}],
            "notes": "The second line was hard to read."}

    monkeypatch.setattr(llm, "structured", structured)
    before = len(service.medications())
    result = await service.scan_prescription(b"\xff\xd8photo", "image/jpeg")
    assert sent["content"][0]["source"]["media_type"] == "image/jpeg"
    assert [(m.name, m.times, m.pills_remaining) for m in result.medicines] == [("Advil", ["08:00", "20:00"], 20)]
    assert result.notes and result.interactions.interactions  # Advil vs Lisinopril, flagged before saving
    assert len(service.medications()) == before  # nothing saved until the caregiver reviews it
    with pytest.raises(SettingsError, match="JPEG"):
        await service.scan_prescription(b"%PDF", "application/pdf")


# ---------- ask Kinvesper ----------

async def test_ask_uses_the_care_data(service, monkeypatch):
    assert "needs Claude" in await service.ask("How was her week?")
    monkeypatch.setattr(llm, "enabled", lambda: True)
    seen = {}

    async def create_message(**params):
        seen.update(params)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="She took most doses.")])

    monkeypatch.setattr(llm, "create_message", create_message)
    answer = await service.ask("Did she miss anything?", [{"q": "Hi", "a": "Hello"}])
    assert answer == "She took most doses."
    data = seen["system"][1]["text"]
    assert "Lisinopril 10 mg at 08:00" in data and "Adherence by day" in data
    assert [m["role"] for m in seen["messages"]] == ["user", "assistant", "user"]


# ---------- daily check-in ----------

async def test_no_checkin_tells_the_family(service, clock):
    p = service.profile()
    p.last_activity = (clock["t"] - timedelta(days=1)).isoformat()  # last heard yesterday
    service.store.save("profile", p, id="main")
    clock["t"] = clock["t"].replace(hour=10, minute=30)
    assert not [a for a in await service.run_checks() if a.kind == "no_checkin"]  # still within the hour
    clock["t"] = clock["t"].replace(hour=11, minute=5)
    alert = next(a for a in await service.run_checks() if a.kind == "no_checkin")
    assert alert.level == "warning" and "hasn't heard from them since" in alert.detail


async def test_morning_activity_counts_as_checked_in(service, clock):
    clock["t"] = clock["t"].replace(hour=8)
    service.touch_activity()
    clock["t"] = clock["t"].replace(hour=11, minute=30)
    assert not [a for a in await service.run_checks() if a.kind == "no_checkin"]


def test_simulator_asks_at_checkin_time(service, monkeypatch):
    current = now().replace(hour=10, minute=1)
    monkeypatch.setattr(web, "now", lambda: current)
    rings = web.due_rings(service, SimpleNamespace(language="en", rung=set(), patient="Margaret"))
    assert any("Kinvesper checking in" in a.text for a in rings)


# ---------- phone calls ----------

async def test_emergency_rings_the_caregiver_now(service, calls):
    p = service.profile()
    p.caregiver_phone = "+919811112222"
    service.store.save("profile", p, id="main")
    result = await service.emergency("I fell in the kitchen")
    assert calls and calls[0][0] == "+919811112222" and "I fell in the kitchen" in calls[0][1]
    assert service.store.load("alert", result.alert_id, service_module.Alert).called_at
    await service.run_checks()
    assert len(calls) == 1  # once


async def test_unanswered_urgent_alert_rings_after_a_few_minutes(service, calls, clock):
    p = service.profile()
    p.caregiver_phone = "+919811112222"
    service.store.save("profile", p, id="main")
    await service.check_in("unwell", "chest pain since morning")
    await service.run_checks()
    assert not calls
    clock["t"] += timedelta(minutes=6)
    await service.run_checks()
    assert len(calls) == 1 and "isn't feeling well" in calls[0][1]


async def test_no_call_without_a_calling_number(service):
    assert not notify.calls_enabled() and not await notify.call("+919811112222", "hello")


# ---------- repeated questions ----------

async def test_asking_again_and_again_is_noticed(service):
    for _ in range(3):
        await service.note_repeat("schedule")
    assert not [a for a in service.alerts() if a.kind == "pattern"]
    await service.note_repeat("schedule")
    await service.note_repeat("schedule")
    alerts = [a for a in service.alerts() if a.kind == "pattern"]
    assert len(alerts) == 1 and "asked about the medicine schedule 4 times today" in alerts[0].title
    assert alerts[0].level == "info" and "memory" in alerts[0].detail


async def test_double_dose_attempts_warn_sooner(service):
    await service.note_repeat("double_dose")
    await service.note_repeat("double_dose")
    alert = next(a for a in service.alerts() if a.kind == "pattern")
    assert alert.level == "warning" and "tried to log a dose that was already taken" in alert.title


# ---------- family messages ----------

def test_family_message_is_heard_once(service):
    service.send_family_message("I'll visit on Sunday with the kids!")
    with pytest.raises(SettingsError):
        service.send_family_message("   ")
    first = service.take_family_messages()
    assert first.message == "You have a message from Priya: I'll visit on Sunday with the kids!"
    assert first.messages[0].heard_at
    again = service.take_family_messages()
    assert again.message.startswith("No new messages from Priya. The last one, from Priya, said:")


def test_simulator_reads_family_messages(service, monkeypatch):
    monkeypatch.setattr(web, "now", lambda: now().replace(hour=13, minute=13))
    service.send_family_message("Call me when you're up")
    rings = web.due_rings(service, SimpleNamespace(language="en", rung=set(), patient="Margaret"))
    assert [(a.ring, a.text) for a in rings] == [("message", "Margaret, you have a message from Priya: Call me when you're up")]
    assert not service.family_messages(unheard_only=True)


# ---------- companionship ----------

def test_conversation_and_memories(service):
    starter = service.conversation_starter()
    assert starter.question in starter.message
    assert not service.save_memory("Nice.").saved
    saved = service.save_memory("Grew up in Shimla, walking to school past apple orchards every morning.", starter.topic)
    assert saved.saved and "Priya can read it" in saved.message
    assert service.conversation_starter().topic != starter.topic  # a new topic next time
    assert service.dashboard()["memories"][0]["text"].startswith("Grew up in Shimla")


# ---------- doctor summary ----------

async def test_doctor_summary(service):
    report = await service.doctor_report(14)
    assert report["days"] == 14 and report["adherence_pct"] is not None
    assert {m["name"] for m in report["medications"]} == {"Lisinopril", "Metformin", "Atorvastatin", "Levothyroxine"}
    assert report["monitored_days"] == 7  # the demo household has a week of history: earlier days aren't "missed"
    assert report["summary"].startswith("Over the last 7 days Margaret took") and report["questions"]
    evening = next(b for b in report["by_time"] if b["time"] == "20:00")
    assert evening["scheduled"] >= evening["taken"]


# ---------- offline voice ----------

def test_offline_router_knows_the_new_features():
    meds = [{"name": "Lisinopril", "purpose": "blood pressure"}]
    assert route("Can I take ibuprofen with my pills?", meds, "Priya") == ("check_medicine_interactions", {"medicine": "ibuprofen"})
    assert route("Is it safe to take aspirin?", meds, "Priya")[0] == "check_medicine_interactions"
    assert route("Can I take my Lisinopril now?", meds, "Priya")[0] == "get_medication_schedule"
    assert route("Any messages for me?", meds, "Priya") == ("get_family_messages", {})
    assert route("Let's chat", meds, "Priya") == ("start_conversation", {})


async def test_voice_tools_end_to_end(connect, service, monkeypatch):
    monkeypatch.setattr(voice.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    session = await sessions.create()
    try:
        reply = await session.turn("Can I take Advil with my pills?")
        assert reply.tool_calls[0].name == "check_medicine_interactions" and "Lisinopril" in reply.reply
        service.send_family_message("Dinner at 7!")
        assert "Dinner at 7!" in (await session.turn("Any messages for me?")).reply
        assert "I'd love to hear about your life" in (await session.turn("Let's chat")).reply
        for _ in range(4):
            await session.turn("What pills do I need today?")
        assert any(a.kind == "pattern" for a in service.alerts())
    finally:
        await sessions.close_all()


# ---------- Echo Show ----------

def _show(request: dict) -> dict:
    system = {**SYSTEM, "device": {"supportedInterfaces": {"Alexa.Presentation.APL": {"runtime": {"maxVersion": "2023.3"}}}}}
    return {"session": {"sessionId": "show-1", "new": False}, "context": {"System": system}, "request": {"locale": "en-US", **request}}


async def test_echo_show_screen_and_taken_button(service, connect, monkeypatch):
    monkeypatch.setattr(voice.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    bridge = AlexaBridge(sessions, service)
    try:
        launch = await bridge.handle(_show({"type": "LaunchRequest"}))
        screen = next(d for d in launch["response"]["directives"] if d["type"] == "Alexa.Presentation.APL.RenderDocument")
        items = screen["datasources"]["screen"]["items"]
        assert screen["datasources"]["screen"]["title"] == "Margaret's medicines today" and items
        pending = next((i for i in items if i["canTake"]), None)
        if pending:  # depends on the time of day the test runs
            touched = await bridge.handle(_show({"type": "Alexa.Presentation.APL.UserEvent", "arguments": ["taken", pending["medication"]]}))
            assert "logged" in touched["response"]["outputSpeech"]["text"]
        plain = await bridge.handle({**_show({"type": "LaunchRequest"}), "context": {"System": SYSTEM}})
        assert "directives" not in plain["response"]  # no screen, no document
    finally:
        await sessions.close_all()


async def test_echo_reads_family_messages_on_launch(service, connect, monkeypatch):
    monkeypatch.setattr(voice.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    bridge = AlexaBridge(sessions, service)
    service.send_family_message("Happy birthday, Mum!")
    try:
        reply = await bridge.handle({"session": {"sessionId": "s9", "new": True}, "context": {"System": SYSTEM},
                                     "request": {"type": "LaunchRequest", "locale": "en-US"}})
        assert reply["response"]["outputSpeech"]["text"].startswith("Margaret, you have a message from Priya: Happy birthday, Mum!")
    finally:
        await sessions.close_all()


# ---------- dashboard API ----------

def test_dashboard_feature_endpoints(fresh_server):
    base = fresh_server
    assert httpx.get(f"{base}/dashboard/doctor").status_code == 200
    report = httpx.get(f"{base}/api/reports/doctor?days=14").json()
    assert report["days"] == 14 and report["questions"]
    sent = httpx.post(f"{base}/api/messages", json={"text": "See you soon"})
    assert sent.status_code == 200 and sent.json()["sender"] == "Priya"
    assert httpx.post(f"{base}/api/messages", json={"text": ""}).status_code == 400
    assert httpx.get(f"{base}/api/dashboard").json()["family_messages"][0]["text"] == "See you soon"
    scan = httpx.post(f"{base}/api/medications/scan", content=b"\xff\xd8", headers={"Content-Type": "image/jpeg"})
    assert scan.status_code == 422 and "needs Claude" in scan.json()["error"]
    assert httpx.post(f"{base}/api/ask", json={"question": "How is she?"}).json()["answer"].startswith("Ask Kinvesper needs Claude")
    assert httpx.post(f"{base}/api/ask", json={}).status_code == 400


async def test_new_medicine_isnt_missed_before_it_was_added(service, clock):
    clock["t"] = clock["t"].replace(hour=15)
    view = await service.settings_view()
    new = {"name": "Vitamin D", "dose": "1000 IU", "times": ["08:00", "20:00"], "pills_remaining": 60}
    await service.update_settings({**view["profile"], "caregiver_email": "a@b.co"}, [*view["medications"], new])
    assert [i.time for i in service.schedule("Vitamin D").items] == ["20:00"]  # 8 AM was before it was added
    clock["t"] = clock["t"].replace(hour=23)
    await service.run_checks()
    titles = [a.title for a in service.alerts() if a.kind == "missed_dose"]
    assert "Margaret missed the 8 PM Vitamin D" in titles and "Margaret missed the 8 AM Vitamin D" not in titles
    assert not any("Vitamin D" in m for m in service._missed_labels(7) if not m.endswith("8 PM Vitamin D"))


# ---------- Twilio phone calls ----------

SID, TOKEN = "AC" + "1" * 32, "f" * 32


@pytest.fixture
def twilio(monkeypatch):
    """Twilio set up, with its REST API in memory."""
    import dataclasses
    import json as _json  # noqa: F401
    from urllib.parse import parse_qs

    placed = []

    def api(request: httpx.Request) -> httpx.Response:
        assert request.url.path in (f"/2010-04-01/Accounts/{SID}/Calls.json", f"/2010-04-01/Accounts/{SID}/Messages.json")
        placed.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(201, json={"sid": "CA123", "status": "queued"})

    config = dataclasses.replace(notify.settings, twilio_credentials=f"{SID}:{TOKEN}:+15005550006",
                                 public_url="https://kinvesper.example.com")
    monkeypatch.setattr(notify, "settings", config)
    monkeypatch.setattr(service_module, "settings", config)
    monkeypatch.setattr("kinvesper.dashboard.settings", config)
    monkeypatch.setattr(notify, "_twilio_transport", httpx.MockTransport(api))
    return placed


def _sign(url: str, params: dict) -> str:
    import base64
    import hashlib
    import hmac
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(TOKEN.encode(), data.encode(), hashlib.sha1).digest()).decode()


async def test_twilio_call_reads_the_alert_and_offers_press_1(service, twilio):
    p = service.profile()
    p.caregiver_phone = "+919811112222"
    service.store.save("profile", p, id="main")
    result = await service.emergency("I fell in the kitchen")
    call = next(c for c in twilio if "Twiml" in c)
    assert call["To"] == "+919811112222" and call["From"] == "+15005550006"
    assert "I fell in the kitchen" in call["Twiml"] and "Press 1" in call["Twiml"]
    assert f'action="https://kinvesper.example.com/api/calls/{result.alert_id}/ack"' in call["Twiml"]
    assert notify.calls_enabled()


def test_press_1_acknowledges_only_when_signed_by_twilio(service, twilio):
    from starlette.testclient import TestClient

    from kinvesper.dashboard import dashboard_routes
    from starlette.applications import Starlette

    alert = __import__("asyncio").run(service.raise_alert("urgent", "emergency", "EMERGENCY", "fell", sms=False))
    client = TestClient(Starlette(routes=dashboard_routes(service)))
    path = f"/api/calls/{alert.id}/ack"
    params = {"Digits": "1", "CallSid": "CA123"}
    forged = client.post(path, data=params, headers={"X-Twilio-Signature": "nope"})
    assert forged.status_code == 403 and not service.store.load("alert", alert.id, service_module.Alert).acknowledged
    signed = client.post(path, data=params, headers={"X-Twilio-Signature": _sign(f"https://kinvesper.example.com{path}", params)})
    assert signed.status_code == 200 and "marked as handled" in signed.text
    assert service.store.load("alert", alert.id, service_module.Alert).acknowledged


async def test_twilio_trial_refusal_is_not_fatal(service, twilio, monkeypatch):
    monkeypatch.setattr(notify, "_twilio_transport", httpx.MockTransport(
        lambda r: httpx.Response(400, json={"message": "The number is unverified. Trial accounts may only make calls to verified numbers."})))
    assert not await notify.call("+919811112222", "hello")


@pytest.fixture
def whatsapp(twilio, monkeypatch):
    import dataclasses
    config = dataclasses.replace(notify.settings, twilio_credentials=f"{SID}:{TOKEN}:+15005550006:+14155238886")
    monkeypatch.setattr(notify, "settings", config)
    monkeypatch.setattr(service_module, "settings", config)
    monkeypatch.setattr("kinvesper.dashboard.settings", config)
    return twilio


async def test_alerts_also_go_out_on_whatsapp(service, whatsapp):
    p = service.profile()
    p.caregiver_phone = "+919811112222"
    service.store.save("profile", p, id="main")
    alert = await service.raise_alert("warning", "missed_dose", "Missed Metformin", "The 8 AM dose wasn't taken.")
    msg = next(m for m in whatsapp if "Body" in m)
    assert msg["To"] == "whatsapp:+919811112222" and msg["From"] == "whatsapp:+14155238886"
    assert "*Kinvesper: Missed Metformin*" in msg["Body"] and "Reply OK" in msg["Body"]
    assert alert.notified and notify.whatsapp_enabled()
    assert (await service.settings_view())["whatsapp"] == "+14155238886"


def test_whatsapp_is_off_without_a_sender(twilio):
    assert not notify.whatsapp_enabled() and notify.calls_enabled()


def _whatsapp_client(service):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from kinvesper.dashboard import dashboard_routes
    client = TestClient(Starlette(routes=dashboard_routes(service)))

    def reply(text: str, sender: str = "+919811112222", signed: bool = True):
        params = {"From": f"whatsapp:{sender}", "Body": text, "MessageSid": "SM1"}
        sig = _sign("https://kinvesper.example.com/api/whatsapp/incoming", params) if signed else "nope"
        return client.post("/api/whatsapp/incoming", data=params, headers={"X-Twilio-Signature": sig})
    return reply


def test_whatsapp_replies_acknowledge_report_and_pass_messages_on(service, whatsapp):
    import asyncio
    p = service.profile()
    p.caregiver_phone = "+919811112222"
    service.store.save("profile", p, id="main")
    alert = asyncio.run(service.raise_alert("urgent", "emergency", "EMERGENCY: fell", "fell", sms=False))
    reply = _whatsapp_client(service)

    assert reply("OK", signed=False).status_code == 403
    assert not service.store.load("alert", alert.id, service_module.Alert).acknowledged

    r = reply("Ok!")
    assert r.status_code == 200 and "Marked as handled: EMERGENCY: fell" in r.text and "<Message>" in r.text
    assert service.store.load("alert", alert.id, service_module.Alert).acknowledged

    assert "doses taken" in reply("Status").text and "No open alerts" in reply("status").text

    r = reply("Love you Mum, see you on Sunday")
    assert "read your message" in r.text
    msg = service.family_messages()[0]
    assert msg.text == "Love you Mum, see you on Sunday" and msg.sender == p.caregiver_name

    stranger = reply("OK", sender="+15550001111")
    assert "isn't a Kinvesper contact" in stranger.text and len(service.family_messages()) == 1


async def test_whatsapp_falls_back_to_the_template_outside_the_24_hour_window(service, monkeypatch):
    import dataclasses
    import json
    from urllib.parse import parse_qs

    sent = []

    def api(request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        sent.append(form)
        if "Body" in form:
            return httpx.Response(400, json={"code": 63016, "message": "Failed to send freeform message outside the window"})
        return httpx.Response(201, json={"sid": "SM1"})

    config = dataclasses.replace(notify.settings, twilio_credentials=f"{SID}:{TOKEN}::+14155238886:HX{'a' * 32}")
    monkeypatch.setattr(notify, "settings", config)
    monkeypatch.setattr(notify, "_twilio_transport", httpx.MockTransport(api))
    assert not notify.calls_enabled() and notify.whatsapp_enabled()
    assert await notify.send("Kinvesper: Missed Metformin", "The 8 AM dose\nwasn't taken.", phone="+919811112222", ack=True)
    template = sent[-1]
    assert template["ContentSid"] == "HX" + "a" * 32 and "Body" not in template
    assert json.loads(template["ContentVariables"]) == {"1": "Missed Metformin", "2": "The 8 AM dose wasn't taken."}


def test_test_alert_also_rings_the_caregiver_and_press_1_works(service, twilio):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from kinvesper.dashboard import dashboard_routes
    p = service.profile()
    p.caregiver_phone = "+919811112222"
    service.store.save("profile", p, id="main")
    client = TestClient(Starlette(routes=dashboard_routes(service)))
    r = client.post("/api/settings/test-alert").json()
    assert r["called"] and r["sent"]
    call = next(c for c in twilio if "Twiml" in c)
    assert "testing alert calls" in call["Twiml"] and "Press 1" in call["Twiml"]
    alert = next(a for a in service.alerts() if a.kind == "test")
    path, params = f"/api/calls/{alert.id}/ack", {"Digits": "1"}
    said = client.post(path, data=params, headers={"X-Twilio-Signature": _sign(f"https://kinvesper.example.com{path}", params)})
    assert "Alert calls are working" in said.text
