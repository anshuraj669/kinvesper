"""Telegram alerts: linking a chat, alerts with buttons, and replies, against a fake Bot API."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import timedelta

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kinvesper import llm, telegram
from kinvesper.config import now
from kinvesper.dashboard import dashboard_routes
from kinvesper.models import Alert, TelegramLink
from kinvesper.service import ANSWER_NOW, SettingsError, heard_messages

TOKEN = "123456789:AAH" + "x" * 32
CHAT, STRANGER = 555001, 999999


@pytest.fixture
def bot(monkeypatch):
    """The Telegram Bot API in memory: every call is recorded as (method, params)."""
    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        method = request.url.path.rsplit("/", 1)[1]
        params = json.loads(request.content or b"{}")
        calls.append((method, params))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "kinvesper_test_bot", "first_name": "Kinvesper"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    monkeypatch.setattr(telegram, "settings", dataclasses.replace(telegram.settings, telegram_bot_token=TOKEN))
    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(api))
    monkeypatch.setattr(telegram, "_username", None)
    return calls


def sent(calls, method="sendMessage"):
    return [p for m, p in calls if m == method]


def webhook(service):
    client = TestClient(Starlette(routes=dashboard_routes(service)))

    def post(update: dict, secret: str | None = None):
        secret = telegram.webhook_secret(TOKEN) if secret is None else secret
        return client.post("/api/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
    return client, post


def message(text: str, chat: int = CHAT) -> dict:
    return {"update_id": 1, "message": {"message_id": 7, "text": text, "chat": {"id": chat, "type": "private"},
                                        "from": {"id": chat, "first_name": "Priya", "last_name": "Sharma"}}}


def button(data: str, chat: int = CHAT) -> dict:
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data, "from": {"id": chat},
                                               "message": {"message_id": 42, "chat": {"id": chat, "type": "private"}}}}


def link(service, post) -> None:
    url = asyncio.run(service.telegram_link("caregiver"))["url"]
    assert url.startswith("https://t.me/kinvesper_test_bot?start=")
    assert post(message(f"/start {url.split('start=')[1]}")).status_code == 200


def test_connect_telegram_links_the_chat_once(service, bot):
    _, post = webhook(service)
    result = asyncio.run(service.telegram_link("caregiver"))
    code = result["url"].split("start=")[1]
    assert result["bot"] == "kinvesper_test_bot" and result["minutes"] == 30

    post(message(f"/start {code}"))
    p = service.profile()
    assert p.caregiver_telegram == str(CHAT) and p.caregiver_telegram_name == "Priya Sharma"
    welcome = sent(bot)[-1]
    assert welcome["chat_id"] == str(CHAT) and "Connected, Priya Sharma" in welcome["text"]
    assert asyncio.run(service.settings_view())["telegram"] == {
        "enabled": True, "bot": "kinvesper_test_bot", "caregiver": "Priya Sharma", "backup": ""}

    post(message(f"/start {code}", chat=STRANGER))  # the code works once
    assert "expired or was already used" in sent(bot)[-1]["text"]
    assert service.profile().caregiver_telegram == str(CHAT)


def test_an_expired_link_doesnt_connect(service, bot):
    _, post = webhook(service)
    old = (now() - timedelta(minutes=31)).isoformat()
    service.store.save("tglink", TelegramLink(id="oldcode", audience="caregiver", created_at=old), id="oldcode")
    post(message("/start oldcode"))
    assert not service.profile().caregiver_telegram and "expired" in sent(bot)[-1]["text"]


def test_webhook_needs_telegrams_secret(service, bot):
    _, post = webhook(service)
    assert post(message("/start x"), secret="wrong").status_code == 403
    assert post(message("/start x"), secret="").status_code == 403
    assert not sent(bot)


def test_alerts_arrive_with_buttons_and_im_on_it_handles_them(service, bot):
    _, post = webhook(service)
    link(service, post)
    alert = asyncio.run(service.raise_alert("urgent", "emergency", "EMERGENCY: Margaret fell", "I fell in the kitchen"))
    msg = sent(bot)[-1]
    assert msg["chat_id"] == str(CHAT) and msg["parse_mode"] == "HTML"
    assert msg["text"].startswith("🚨 <b>EMERGENCY: Margaret fell</b>") and "I fell in the kitchen" in msg["text"]
    assert msg["reply_markup"]["inline_keyboard"][0][0] == {"text": "✅ I'm on it", "callback_data": f"ack:{alert.id}"}
    assert alert.notified

    post(button(f"ack:{alert.id}"))
    assert service.store.load("alert", alert.id, Alert).acknowledged
    assert sent(bot, "answerCallbackQuery")[-1]["text"] == "Marked as handled"
    edited = sent(bot, "editMessageReplyMarkup")[-1]
    assert edited["message_id"] == 42 and edited["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Handled (Priya)"

    post(button(f"ack:{alert.id}"))
    assert sent(bot, "answerCallbackQuery")[-1]["text"] == "Already handled"


def test_replies_status_ok_and_messages_for_the_echo(service, bot):
    _, post = webhook(service)
    link(service, post)
    alert = asyncio.run(service.raise_alert("warning", "missed_dose", "Missed Metformin", "8 AM dose", sms=False))

    post(message("/status"))
    assert "doses taken" in sent(bot)[-1]["text"] and "Missed Metformin" in sent(bot)[-1]["text"]
    post(message("ok"))
    assert "Marked as handled: Missed Metformin" in sent(bot)[-1]["text"]
    assert service.store.load("alert", alert.id, Alert).acknowledged

    post(message("Love you Mum, see you on Sunday & bring <cake>"))
    assert "read your message" in sent(bot)[-1]["text"]
    family = service.family_messages()[0]
    assert family.text == "Love you Mum, see you on Sunday & bring <cake>" and family.sender == service.profile().caregiver_name

    post(button("status"))
    assert "doses taken" in sent(bot)[-1]["text"]


def test_strangers_cant_use_the_bot(service, bot):
    _, post = webhook(service)
    link(service, post)
    alert = asyncio.run(service.raise_alert("urgent", "emergency", "EMERGENCY", "fell", sms=False))
    post(message("ok", chat=STRANGER))
    assert "isn't connected" in sent(bot)[-1]["text"]
    post(button(f"ack:{alert.id}", chat=STRANGER))
    assert not service.store.load("alert", alert.id, Alert).acknowledged
    assert not service.family_messages()


def test_stop_and_disconnect(service, bot):
    client, post = webhook(service)
    link(service, post)
    post(message("/stop"))
    assert not service.profile().caregiver_telegram and "disconnected" in sent(bot)[-1]["text"]

    link(service, post)
    r = client.post("/api/telegram/unlink", json={"audience": "caregiver"})
    assert r.status_code == 200 and r.json()["telegram"]["caregiver"] == ""


def test_backup_gets_escalations_and_the_caregiver_the_weekly_report(service, bot):
    _, post = webhook(service)
    link(service, post)
    url = asyncio.run(service.telegram_link("backup"))["url"]
    post(message(f"/start {url.split('start=')[1]}", chat=STRANGER))
    p = service.profile()
    service.store.save("profile", p.model_copy(update={"backup_name": "Ravi"}), id="main")
    alert = asyncio.run(service.raise_alert("urgent", "scam", "Likely scam call", "A caller asked for her PIN", sms=False))
    asyncio.run(service._escalate(now() + timedelta(minutes=30)))
    escalation = sent(bot)[-1]
    assert escalation["chat_id"] == str(STRANGER) and "Please check on" in escalation["text"]
    assert "Likely scam call" in escalation["text"] and f"ack:{alert.id}" in json.dumps(escalation["reply_markup"])

    asyncio.run(service.send_weekly_report())
    report = sent(bot)[-1]
    assert report["chat_id"] == str(CHAT) and report["text"].startswith("📊 <b>Weekly report")


def test_off_without_a_token(service):
    assert not telegram.enabled()
    with pytest.raises(SettingsError, match="isn't set up"):
        asyncio.run(service.telegram_link("caregiver"))


def test_connect_is_refused_when_the_bots_chats_go_to_another_server(service, bot, monkeypatch):
    async def deployed():
        return "https://kinvesper.example.com"
    monkeypatch.setattr(telegram, "delivered_elsewhere", deployed)
    with pytest.raises(SettingsError, match="kinvesper.example.com/dashboard/settings"):
        asyncio.run(service.telegram_link("caregiver"))


def test_delivered_elsewhere_reads_the_webhook(bot, monkeypatch):
    def api(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"url": "https://kinvesper.example.com/api/telegram/webhook"}})
    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(api))
    assert asyncio.run(telegram.delivered_elsewhere()) == "https://kinvesper.example.com"
    monkeypatch.setattr(telegram, "settings", dataclasses.replace(telegram.settings, public_url="https://kinvesper.example.com"))
    assert asyncio.run(telegram.delivered_elsewhere()) == ""


def test_a_question_from_telegram_gets_her_answer_back(service, bot):
    _, post = webhook(service)
    link(service, post)
    post(message("Ask her does she take her pills today ?"))
    assert "send you the answer here" in sent(bot)[-1]["text"]
    asked = service.family_messages()[0]
    assert asked.question and asked.telegram_chat == str(CHAT) and asked.telegram_message_id == 7
    assert asked.text == "Does she take her pills today ?" and asked.asked == "Ask her does she take her pills today ?"

    heard = service.take_family_messages()
    assert heard.message == "Priya is asking: Does she take her pills today ?"
    said, note = heard_messages(heard.messages, "en-US", "Margaret")
    assert said == "Margaret, Priya is asking: Does she take her pills today ?" and "answer_family_question" in note

    result = asyncio.run(service.answer_family_question("Yes, I took my pills."))
    assert result.sent and "sent your answer to Priya" in result.message
    reply = sent(bot)[-1]
    assert reply["chat_id"] == str(CHAT) and reply["reply_parameters"]["message_id"] == 7
    assert "Margaret answered" in reply["text"] and "You asked: Ask her does she take her pills today ?" in reply["text"]
    assert reply["text"].endswith("Yes, I took my pills.")
    assert not asyncio.run(service.answer_family_question("Also, it's sunny")).sent  # one answer per question


def test_only_questions_wait_for_an_answer(service, bot):
    _, post = webhook(service)
    link(service, post)
    post(message("Love you Mum, see you on Sunday"))
    assert "send you the answer" not in sent(bot)[-1]["text"]
    heard = service.take_family_messages()
    assert heard_messages(heard.messages, "en-US", "Margaret")[1] == ""
    assert not asyncio.run(service.answer_family_question("Love you too")).sent


def test_claude_rewords_the_question_for_her(service, bot, monkeypatch):
    async def structured(system, content, schema, max_tokens=2048):
        assert "Margaret" in system and content == "ask mum if she slept well"
        return {"question": True, "say": "Did you sleep well?"}
    monkeypatch.setattr(llm, "structured", structured)
    _, post = webhook(service)
    link(service, post)
    post(message("ask mum if she slept well"))
    assert service.family_messages()[0].text == "Did you sleep well?"
    assert "will ask Margaret “Did you sleep well?”" in sent(bot)[-1]["text"]


def test_messaging_the_asker_right_after_the_question_answers_it(service, bot):
    _, post = webhook(service)
    link(service, post)
    post(message("Did you eat lunch?"))
    service.take_family_messages()
    assert service.family_question(within=ANSWER_NOW, sender="Priya")
    assert not service.family_question(within=ANSWER_NOW, sender="Ravi")
