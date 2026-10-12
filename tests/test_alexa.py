"""The classic Alexa skill bridge: Amazon's request checks, and conversations through the voice agent."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import Store

from kinvesper import alexa, voice
from kinvesper.alexa import AlexaBridge, InvalidRequest, RequestVerifier, check_cert_url
from kinvesper.voice import VoiceSessions

SKILL = "amzn1.ask.skill.test"
CERT_URL = "https://s3.amazonaws.com/echo.api/echo-api-cert.pem"
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("url", [
    "https://s3.amazonaws.com/echo.api/echo-api-cert.pem",
    "https://s3.amazonaws.com:443/echo.api/echo-api-cert.pem",
    "https://s3.amazonaws.com/echo.api/../echo.api/echo-api-cert.pem",
    "HTTPS://s3.amazonaws.com/echo.api/echo-api-cert.pem",
    "https://S3.AMAZONAWS.COM/echo.api/echo-api-cert.pem",
])
def test_cert_url_accepted(url):
    check_cert_url(url)


@pytest.mark.parametrize("url", [  # Amazon's examples of URLs to reject
    "http://s3.amazonaws.com/echo.api/echo-api-cert.pem",
    "https://notamazon.com/echo.api/echo-api-cert.pem",
    "https://s3.amazonaws.com/EcHo.aPi/echo-api-cert.pem",
    "https://s3.amazonaws.com/invalid.path/echo-api-cert.pem",
    "https://s3.amazonaws.com/echo.api/../evil/echo-api-cert.pem",
    "https://s3.amazonaws.com:563/echo.api/echo-api-cert.pem",
    "",
])
def test_cert_url_rejected(url):
    with pytest.raises(InvalidRequest):
        check_cert_url(url)


def _cert(subject, issuer_key, key, issuer=None, *, ca=False, san=None):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer or name).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=30))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if ca:
        builder = builder.add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
    else:
        builder = (builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False))
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture
def signed():
    """A fake Amazon: a root CA, a signing cert for `san`, and a request signer."""
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root = _cert("Test Root", root_key, root_key, ca=True)

    def make(san="echo-api.amazon.com"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        leaf = _cert(san, root_key, key, issuer=root.subject, san=san)
        pem = leaf.public_bytes(serialization.Encoding.PEM)

        async def fetch(_url):
            return pem

        verifier = RequestVerifier(SKILL, fetch=fetch, now=lambda: NOW, roots=Store([root]))

        def request(skill=SKILL, at=NOW):
            payload = {"version": "1.0", "context": {"System": {"application": {"applicationId": skill}}},
                       "request": {"type": "LaunchRequest", "timestamp": at.isoformat().replace("+00:00", "Z")}}
            body = json.dumps(payload).encode()
            sig = base64.b64encode(key.sign(body, padding.PKCS1v15(), hashes.SHA256())).decode()
            return {"signaturecertchainurl": CERT_URL, "signature-256": sig}, body, payload

        return verifier, request

    return make


async def test_verifier_accepts_amazon_request(signed):
    verifier, request = signed()
    await verifier.verify(*request())


async def test_verifier_rejects_forgeries(signed):
    verifier, request = signed()
    headers, body, payload = request()
    with pytest.raises(InvalidRequest, match="signature"):
        await verifier.verify(headers, body.replace(b"LaunchRequest", b"IntentRequest"), payload)
    with pytest.raises(InvalidRequest, match="stale"):
        await verifier.verify(*request(at=NOW - timedelta(minutes=3)))
    with pytest.raises(InvalidRequest, match="wrong skill"):
        await verifier.verify(*request(skill="amzn1.ask.skill.someone-else"))
    with pytest.raises(InvalidRequest, match="certificate URL"):
        await verifier.verify({**headers, "signaturecertchainurl": "https://evil.example/echo.api/c.pem"}, body, payload)

    impostor, impostor_request = signed(san="evil.example.com")
    with pytest.raises(InvalidRequest, match="untrusted"):
        await impostor.verify(*impostor_request())


# ---------- conversations ----------


def _intent(name, sid="alexa-session-1", new=False, utterance=None):
    intent = {"name": name}
    if utterance is not None:
        intent["slots"] = {"utterance": {"name": "utterance", "value": utterance}}
    return {"session": {"sessionId": sid, "new": new}, "request": {"type": "IntentRequest", "intent": intent}}


def _said(reply):
    return reply["response"]["outputSpeech"]["text"]


@pytest.fixture
async def bridge(connect, monkeypatch):
    monkeypatch.setattr(voice.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    yield AlexaBridge(sessions)
    await sessions.close_all()


async def test_conversation(bridge, service):
    hello = await bridge.handle({"session": {"sessionId": "alexa-session-1", "new": True}, "request": {"type": "LaunchRequest"}})
    assert "Priya" in _said(hello) and hello["response"]["shouldEndSession"] is False

    pills = await bridge.handle(_intent("CatchAllIntent", utterance="did I take my pills"))
    assert "doses today" in _said(pills)
    assert pills["response"]["shouldEndSession"] is False

    ask = await bridge.handle(_intent("CatchAllIntent", utterance="tell Priya I'm going to the doctor"))
    assert "Priya" in _said(ask) and ask["response"]["reprompt"]
    done = await bridge.handle(_intent("AMAZON.YesIntent"))
    assert "sent that to Priya" in _said(done)
    assert any(a.kind == "caregiver_message" for a in service.alerts())

    bye = await bridge.handle(_intent("AMAZON.StopIntent"))
    assert bye["response"]["shouldEndSession"] is True
    assert not bridge._by_alexa


async def test_one_shot_ends_session(bridge):
    reply = await bridge.handle(_intent("CatchAllIntent", sid="s2", new=True, utterance="what pills do I need today"))
    assert "doses today" in _said(reply)
    assert reply["response"]["shouldEndSession"] is True


async def test_stop_declines_pending_message(bridge, service):
    await bridge.handle(_intent("CatchAllIntent", sid="s3", utterance="tell Priya I'm fine"))
    await bridge.handle(_intent("AMAZON.CancelIntent", sid="s3"))
    assert not any(a.kind == "caregiver_message" for a in service.alerts())


async def test_session_ended_closes(bridge):
    await bridge.handle(_intent("AMAZON.HelpIntent", sid="s4"))
    assert "s4" in bridge._by_alexa
    await bridge.handle({"session": {"sessionId": "s4"}, "request": {"type": "SessionEndedRequest"}})
    assert "s4" not in bridge._by_alexa


async def test_slow_answer_is_kept_for_the_next_turn(bridge, monkeypatch):
    original = voice.VoiceSession._respond_offline

    async def slow(self, text):
        await asyncio.sleep(0.3)
        return await original(self, text)

    monkeypatch.setattr(voice.VoiceSession, "_respond_offline", slow)
    monkeypatch.setattr(alexa, "REPLY_DEADLINE_S", 0.1)
    wait = await bridge.handle(_intent("CatchAllIntent", sid="s5", new=True, utterance="did I take my pills"))
    assert _said(wait) == alexa.STILL_WORKING and wait["response"]["shouldEndSession"] is False

    monkeypatch.setattr(alexa, "REPLY_DEADLINE_S", 5)
    answer = await bridge.handle(_intent("CatchAllIntent", sid="s5", utterance="okay"))
    assert "doses today" in _said(answer)
    assert not bridge._late


async def test_welcome_offers_anything_with_an_llm(bridge, monkeypatch):
    launch = {"session": {"sessionId": "s6", "new": True}, "request": {"type": "LaunchRequest"}}
    assert "anything else" not in _said(await bridge.handle(launch))
    monkeypatch.setattr(alexa.llm, "enabled", lambda: True)
    assert "ask me anything else" in _said(await bridge.handle(launch))


def test_endpoint_off_without_skill_id():
    assert alexa.alexa_routes(VoiceSessions()) == []
