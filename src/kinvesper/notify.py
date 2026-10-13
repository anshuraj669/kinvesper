"""Caregiver notifications over Amazon SNS, to the contact saved in the profile. Falls back to logging.

- Phone: a text straight to the number (sns.publish PhoneNumber=...). A new AWS account's SMS sandbox
  only delivers to verified numbers.
- Email: SNS emails a topic's confirmed subscribers. Saving an email in the dashboard subscribes it to
  the alerts topic (SNS_TOPIC_ARN); Amazon sends a confirmation link, and alerts arrive once it's clicked.
  Each email subscribes with a filter for its audience: the caregiver gets every alert, the backup
  contact only escalations. (Contacts subscribed at deploy time have no filter and get everything.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Literal

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


async def send(subject: str, body: str, phone: str = "", audience: Audience = "caregiver") -> bool:
    """Text `phone` and email the audience's subscriber. Returns True when a real message was handed to SNS."""
    try:
        return await asyncio.to_thread(_publish, subject, body, phone, audience)
    except Exception as e:  # never let a notification failure break a care action
        log.warning("SNS publish failed: %s", e)
        return False


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
