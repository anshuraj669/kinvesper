"""Caregiver notifications over Amazon SNS, to the contact saved in the profile. Falls back to logging.

- Urgent alerts also ring the caregiver's phone: a spoken call through Twilio (TWILIO_*; pressing 1
  acknowledges the alert), or AWS End User Messaging SMS and Voice (VOICE_CALL_FROM, one-way). With
  neither, no calls.
- WhatsApp, through Twilio (TWILIO_WHATSAPP_FROM: the sandbox number +14155238886 on a trial, where each
  phone first sends the sandbox's "join ..." code). The caregiver can reply: see service.whatsapp_reply.

- Phone: a text straight to the number (sns.publish PhoneNumber=...). A new AWS account's SMS sandbox
  only delivers to verified numbers.
- Email: SNS emails a topic's confirmed subscribers. Saving an email in the dashboard subscribes it to
  the alerts topic (SNS_TOPIC_ARN); Amazon sends a confirmation link, and alerts arrive once it's clicked.
  Each email subscribes with a filter for its audience: the caregiver gets every alert, the backup
  contact only escalations. (Contacts subscribed at deploy time have no filter and get everything.)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
from typing import Literal
from xml.sax.saxutils import escape

import httpx

from .config import settings

log = logging.getLogger(__name__)

PHONE = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EmailStatus = Literal["confirmed", "pending", "not_subscribed", "no_topic", "unknown"]
Audience = Literal["caregiver", "backup"]


def _sns():
    import boto3

    return boto3.client("sns", region_name=settings.aws_region)


def _publish(subject: str, body: str, phone: str, audience: Audience) -> bool:
    if not (settings.sns_topic_arn or phone):
        log.info("[%s notification] %s: %s", audience, subject, body)
        return False
    sns, sent = _sns(), False
    if settings.sns_topic_arn:  # the audience's email, and anyone subscribed at deploy time
        sns.publish(TopicArn=settings.sns_topic_arn, Subject=subject[:99], Message=body,
                    MessageAttributes={"audience": {"DataType": "String", "StringValue": audience}})
        sent = True
    if phone:
        try:
            sns.publish(PhoneNumber=phone, Message=f"{subject}\n{body}"[:1500])
            sent = True
        except Exception as e:  # e.g. a number not verified in the SMS sandbox: the email still went
            log.warning("SMS to the caregiver failed: %s", e)
    return sent


async def send(subject: str, body: str, phone: str = "", audience: Audience = "caregiver", ack: bool = False) -> bool:
    """Text `phone` (by SMS, and on WhatsApp when set up) and email the audience's subscriber. With `ack`,
    the WhatsApp message says a reply of OK marks it handled. True when a real message was handed over."""
    try:
        sent = await asyncio.to_thread(_publish, subject, body, phone, audience)
    except Exception as e:  # never let a notification failure break a care action
        log.warning("SNS publish failed: %s", e)
        sent = False
    if phone and whatsapp_enabled():
        tail = "\n\n_Reply OK when you're on it, STATUS for today, or anything else to have it read out on the Echo._" if ack else ""
        sent = await whatsapp(phone, f"*{subject}*\n{body}{tail}") or sent
    return sent


def _subscriptions(sns) -> list[dict]:
    subs, token = [], None
    while True:
        page = sns.list_subscriptions_by_topic(TopicArn=settings.sns_topic_arn, **({"NextToken": token} if token else {}))
        subs += [s for s in page["Subscriptions"] if s["Protocol"] == "email"]
        if not (token := page.get("NextToken")):
            return subs


def _change_email(old: str, new: str, audience: Audience) -> None:
    sns = _sns()
    subs = _subscriptions(sns)
    if old and old.lower() != new.lower():
        for s in subs:  # a pending subscription can't be removed; it expires on its own
            if s["Endpoint"].lower() == old.lower() and s["SubscriptionArn"].startswith("arn:"):
                sns.unsubscribe(SubscriptionArn=s["SubscriptionArn"])
    if new and not any(s["Endpoint"].lower() == new.lower() and s["SubscriptionArn"].startswith("arn:") for s in subs):
        sns.subscribe(TopicArn=settings.sns_topic_arn, Protocol="email", Endpoint=new,  # sends the confirmation email
                      Attributes={"FilterPolicy": json.dumps({"audience": [audience]})})


async def change_email(old: str, new: str, audience: Audience = "caregiver") -> None:
    """Point an audience's alerts at a new email (or none). New addresses get Amazon's confirmation email."""
    if not settings.sns_topic_arn or not (old or new):
        return
    try:
        await asyncio.to_thread(_change_email, old, new, audience)
    except Exception as e:
        log.warning("Couldn't update the caregiver email subscription: %s", e)


async def email_status(email: str) -> EmailStatus:
    if not settings.sns_topic_arn:
        return "no_topic"
    if not email:
        return "not_subscribed"
    try:
        subs = await asyncio.to_thread(lambda: _subscriptions(_sns()))
    except Exception as e:
        log.warning("Couldn't list the alert subscriptions: %s", e)
        return "unknown"
    mine = [s for s in subs if s["Endpoint"].lower() == email.lower()]
    if any(s["SubscriptionArn"].startswith("arn:") for s in mine):
        return "confirmed"
    return "pending" if mine else "not_subscribed"


TWILIO_API = "https://api.twilio.com/2010-04-01"
VOICE = "Polly.Joanna"  # Twilio speaks with Amazon Polly voices too
_twilio_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a fake Twilio


def _twilio_parts() -> tuple[str, str, str, str]:
    """(account SID, auth token, calling number, WhatsApp number), from TWILIO_CREDENTIALS
    ("AC...:token:+1...[:+1...]") or the separate variables. Missing parts are empty."""
    parts = [settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from, settings.twilio_whatsapp_from]
    if settings.twilio_credentials.count(":") >= 2:
        given = [x.strip() for x in settings.twilio_credentials.split(":")]
        parts = [*given[:4], *[""] * (4 - len(given[:4]))]
        parts[3] = parts[3] or settings.twilio_whatsapp_from
    sid, token, sender, wa = parts
    return sid, token, sender, wa.removeprefix("whatsapp:").replace(" ", "")


def twilio_account() -> tuple[str, str] | None:
    sid, token, _, _ = _twilio_parts()
    return (sid, token) if sid.startswith("AC") and token else None


def twilio() -> tuple[str, str, str] | None:
    """(account SID, auth token, from number) when Twilio calls are set up."""
    sid, token, sender, _ = _twilio_parts()
    return (sid, token, sender) if twilio_account() and PHONE.match(sender) else None


def whatsapp_from() -> str:
    """The Twilio WhatsApp sender (E.164), or "" when WhatsApp isn't set up."""
    _, _, _, wa = _twilio_parts()
    return wa if twilio_account() and PHONE.match(wa) else ""


def whatsapp_enabled() -> bool:
    return bool(whatsapp_from())


def calls_enabled() -> bool:
    return bool(twilio() or settings.voice_call_from)


def twiml(text: str, ack_url: str = "") -> str:
    """What the call says: the alert, then (with an acknowledge link) "press 1", then the alert again."""
    say = f'<Say voice="{VOICE}">{escape(text)}</Say>'
    if not ack_url:
        return f'<Response>{say}<Pause length="1"/><Say voice="{VOICE}">Again:</Say>{say}</Response>'
    ask = f'<Say voice="{VOICE}">Press 1 to let Kinvesper know you are on it.</Say>'
    return (f'<Response><Gather numDigits="1" timeout="8" method="POST" action="{escape(ack_url)}">{say}{ask}</Gather>'
            f'<Gather numDigits="1" timeout="8" method="POST" action="{escape(ack_url)}"><Say voice="{VOICE}">Again:</Say>{say}{ask}</Gather>'
            f'<Say voice="{VOICE}">Goodbye.</Say></Response>')


def twilio_signature_ok(url: str, params: dict[str, str], signature: str) -> bool:
    """Twilio signs each request: base64 HMAC-SHA1, keyed by the auth token, of the URL followed by the
    POST parameters sorted by name (each name then its value)."""
    creds = twilio_account()
    if not creds or not signature:
        return False
    data = url + "".join(k + params[k] for k in sorted(params))
    expected = base64.b64encode(hmac.new(creds[1].encode(), data.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected, signature)


async def _twilio_call(phone: str, text: str, ack_url: str) -> None:
    sid, token, sender = twilio()
    async with httpx.AsyncClient(timeout=10, auth=(sid, token), transport=_twilio_transport) as http:
        r = await http.post(f"{TWILIO_API}/Accounts/{sid}/Calls.json",
                            data={"To": phone, "From": sender, "Twiml": twiml(text, ack_url)})
    if r.status_code >= 300:  # e.g. a trial account calling a number that isn't verified
        raise RuntimeError(f"Twilio {r.status_code}: {r.json().get('message', r.text[:200]) if r.content else ''}")


async def whatsapp(phone: str, text: str) -> bool:
    """A WhatsApp message to `phone` from the Twilio WhatsApp sender. True when Twilio accepted it."""
    sender, creds = whatsapp_from(), twilio_account()
    if not (sender and creds and PHONE.match(phone)):
        return False
    sid, token = creds
    try:
        async with httpx.AsyncClient(timeout=10, auth=(sid, token), transport=_twilio_transport) as http:
            r = await http.post(f"{TWILIO_API}/Accounts/{sid}/Messages.json",
                                data={"To": f"whatsapp:{phone}", "From": f"whatsapp:{sender}", "Body": text[:1500]})
        if r.status_code >= 300:  # e.g. a phone that hasn't joined the sandbox
            raise RuntimeError(f"Twilio {r.status_code}: {r.json().get('message', r.text[:200]) if r.content else ''}")
        return True
    except Exception as e:
        log.warning("WhatsApp to ...%s failed: %s", phone[-4:], e)
        return False


def message_twiml(text: str) -> str:
    """A WhatsApp reply, as Twilio expects it from the incoming-message webhook."""
    return f"<Response><Message>{escape(text)}</Message></Response>" if text else "<Response/>"


def _aws_call(phone: str, text: str) -> None:
    import boto3

    ssml = f'<speak>{escape(text)}<break time="1s"/>Again: {escape(text)}</speak>'  # said twice: people miss the start
    boto3.client("pinpoint-sms-voice-v2", region_name=settings.aws_region).send_voice_message(
        DestinationPhoneNumber=phone, OriginationIdentity=settings.voice_call_from,
        MessageBody=ssml, MessageBodyTextType="SSML", VoiceId="JOANNA")


async def call(phone: str, text: str, ack_url: str = "") -> bool:
    """Ring `phone` and read `text` aloud (with Twilio, pressing 1 posts to `ack_url`). True when placed."""
    if not phone or not calls_enabled():
        log.info("[call skipped: %s] %s", "no number to call" if not phone else "no calling service set up", text)
        return False
    try:
        if twilio():
            await _twilio_call(phone, text, ack_url)
        else:
            await asyncio.to_thread(_aws_call, phone, text)
        return True
    except Exception as e:  # e.g. a destination not verified in the trial or sandbox: the text and email still went
        log.warning("Phone call to ...%s failed: %s", phone[-4:], e)
        return False
