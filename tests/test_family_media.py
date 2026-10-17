"""Voice notes and photos from the family's Telegram chat: played and shown on the Echo, and her
recorded answer sent back as a voice note. Against a fake Bot API."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import math
import struct
import wave

import av
import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kinvesper import alexa, llm, media, speech, telegram
from kinvesper.alexa import AlexaBridge
from kinvesper.dashboard import dashboard_routes
from kinvesper.voice import VoiceSessions
from kinvesper.web import due_rings

TOKEN = "123456789:AAH" + "x" * 32
CHAT = 555001


def tone(seconds: float = 1.0) -> bytes:
    """A short WAV, standing in for a recording."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(int(16000 * seconds))))
    return out.getvalue()


VOICE_NOTE = asyncio.run(media.to_voice_note(tone()))
PHOTO = b"\xff\xd8\xff\xe0fake-jpeg"


@pytest.fixture
def bot(monkeypatch):
    calls = []

    def api(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(f"/file/bot{TOKEN}/"):
            calls.append(("download", path))
            return httpx.Response(200, content=VOICE_NOTE if path.endswith(".oga") else PHOTO)
        method = path.rsplit("/", 1)[1]
        try:
            params = json.loads(request.content or b"{}")
        except ValueError:  # multipart: sendVoice
            params = {"multipart": request.content}
        calls.append((method, params))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "kinvesper_test_bot", "first_name": "Kinvesper"}})
        if method == "getFile":
            ext = ".oga" if params["file_id"].startswith("voice") else ".jpg"
            return httpx.Response(200, json={"ok": True, "result": {"file_path": f"files/{params['file_id']}{ext}"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    monkeypatch.setattr(telegram, "settings", dataclasses.replace(telegram.settings, telegram_bot_token=TOKEN))
    monkeypatch.setattr(media, "settings", dataclasses.replace(media.settings, telegram_bot_token=TOKEN))
    monkeypatch.setattr(telegram, "_transport", httpx.MockTransport(api))
    monkeypatch.setattr(telegram, "_username", None)
    media._cache.clear()
    return calls


def sent(calls, method="sendMessage"):
    return [p for m, p in calls if m == method]


def linked(service):
    p = service.profile()
    service.store.save("profile", p.model_copy(update={"caregiver_telegram": str(CHAT), "caregiver_telegram_name": "Priya"}), id="main")


def update(**message) -> dict:
    return {"update_id": 1, "message": {"message_id": 70, "chat": {"id": CHAT, "type": "private"},
                                        "from": {"id": CHAT, "first_name": "Priya"}, **message}}


def test_a_voice_note_is_played_and_its_question_answered_back(service, bot, monkeypatch):
    linked(service)
    monkeypatch.setattr(speech, "stt_available", lambda: True)

    async def transcribe(audio, vocabulary):
        assert audio == VOICE_NOTE and "Margaret" in vocabulary.hint
        return "Mum, did you eat your lunch?"
    monkeypatch.setattr(speech, "transcribe", transcribe)

    asyncio.run(service.telegram_update(update(voice={"file_id": "voice-1", "duration": 4, "mime_type": "audio/ogg"})))
    assert "play your voice message to Margaret" in sent(bot)[-1]["text"] and "answer here" in sent(bot)[-1]["text"]
    m = service.family_messages()[0]
    assert (m.media, m.file_id, m.duration, m.question, m.text) == ("voice", "voice-1", 4, True, "Mum, did you eat your lunch?")

    # the simulator: the intro is spoken, then her daughter's own voice plays
    from kinvesper.voice import VoiceSession
    session = VoiceSession()
    session.patient = "Margaret"
    ring = due_rings(service, session)[-1]
    assert ring.text == "Margaret, Priya sent you a voice message." and ring.audio_url.startswith(f"/media/{m.id}?")
    assert ring.sender == "Priya" and "answer_family_question" in session.notes[-1]

    # the link plays as MP3, and only with its signature
    client = TestClient(Starlette(routes=dashboard_routes(service)))
    assert client.get(f"/media/{m.id}").status_code == 403
    r = client.get(ring.audio_url)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    assert av.open(io.BytesIO(r.content)).streams.audio[0].codec_context.name.startswith("mp3")

    # her answer goes back as text, then her recorded voice as a voice note under it
    assert asyncio.run(service.answer_family_question("Yes, I had rice and dal.")).sent
    assert asyncio.run(service.send_answer_voice(tone(0.5)))
    voice = sent(bot, "sendVoice")[-1]["multipart"]
    assert b'name="voice"; filename="answer.ogg"' in voice and b"OggS" in voice and b'"message_id": 70' in voice
    assert service.family_messages()[0].answer_voice
    assert not asyncio.run(service.send_answer_voice(tone(0.5)))  # once per answer


def test_a_photo_is_described_and_shown(service, bot, monkeypatch):
    linked(service)

    async def structured(system, content, schema, max_tokens=2048):
        if isinstance(content, list):  # the photo
            assert content[0]["type"] == "image" and "eyesight" in system
            return {"description": "Priya and two children smiling on a beach."}
        return {"question": True, "say": "Do you remember this beach?"}
    monkeypatch.setattr(llm, "structured", structured)

    asyncio.run(service.telegram_update(update(caption="does she remember this beach?", photo=[
        {"file_id": "photo-small", "width": 320}, {"file_id": "photo-big", "width": 1280}, {"file_id": "photo-huge", "width": 2560}])))
    assert "show your photo on Margaret's Echo Show" in sent(bot)[-1]["text"]
    m = service.family_messages()[0]
    assert m.media == "photo" and m.file_id == "photo-big" and m.question
    assert m.text == "Priya and two children smiling on a beach. Do you remember this beach?"
    assert service.take_family_messages().message == f"Priya sent a photo: {m.text}"

    client = TestClient(Starlette(routes=dashboard_routes(service)))
    r = client.get(media.url(m.id))
    assert r.status_code == 200 and r.content == PHOTO and r.headers["content-type"] == "image/jpeg"


def test_long_voice_notes_are_turned_away(service, bot):
    linked(service)
    asyncio.run(service.telegram_update(update(voice={"file_id": "voice-2", "duration": 600})))
    assert "under 3 minutes" in sent(bot)[-1]["text"] and not service.family_messages()


def test_expired_media_links_are_refused(service):
    assert not media.signature_ok("msg_x", "1", media.sign("msg_x", 1))
    assert not media.signature_ok("msg_x", "9999999999", "forged")


@pytest.fixture
async def bridge(connect, monkeypatch, service):
    monkeypatch.setattr(alexa.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    yield AlexaBridge(sessions, service)
    await sessions.close_all()


async def test_on_alexa_the_voice_note_plays_and_the_photo_fills_the_screen(bridge, service, bot):
    from kinvesper.models import FamilyMessage
    service.store.save("message", FamilyMessage(sender="Priya", text="See you Sunday", created_at="2026-10-17T09:00:00+05:30",
                                                media="voice", file_id="voice-3"))
    service.store.save("message", FamilyMessage(sender="Priya", text="Priya at the beach.", created_at="2026-10-17T09:01:00+05:30",
                                                media="photo", file_id="photo-3"))
    show = {"Alexa.Presentation.APL": {}}
    hello = await bridge.handle({"session": {"sessionId": "s-media", "new": True}, "request": {"type": "LaunchRequest"},
                                 "context": {"System": {"device": {"supportedInterfaces": show}}}})
    speech_out = hello["response"]["outputSpeech"]
    assert speech_out["type"] == "SSML" and speech_out["ssml"].startswith("<speak>Margaret, Priya sent you a voice message.")
    assert '<audio src="http' in speech_out["ssml"] and "/media/msg_" in speech_out["ssml"] and "&amp;s=" in speech_out["ssml"]
    assert "Priya sent you a photo. Priya at the beach." in speech_out["ssml"]
    screen = hello["response"]["directives"][-1]
    assert screen["token"] == "kinvesper-photo" and screen["datasources"]["photo"]["caption"] == "From Priya"
    assert "/media/msg_" in screen["datasources"]["photo"]["url"]
