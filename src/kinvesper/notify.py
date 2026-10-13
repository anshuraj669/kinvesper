"""Caregiver notifications over Amazon SNS, to the contact saved in the profile. Falls back to logging.

- Phone: a text straight to the number (sns.publish PhoneNumber=...). A new AWS account's SMS sandbox
  only delivers to verified numbers.
- Email: SNS emails a topic's confirmed subscribers. Saving an email in the dashboard subscribes it to
  the alerts topic (SNS_TOPIC_ARN); Amazon sends a confirmation link, and alerts arrive once it's clicked.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal

from .config import settings

log = logging.getLogger(__name__)

PHONE = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EmailStatus = Literal["confirmed", "pending", "not_subscribed", "no_topic", "unknown"]


def _sns():
    import boto3

    return boto3.client("sns", region_name=settings.aws_region)


def _publish(subject: str, body: str, phone: str) -> bool:
    if not (settings.sns_topic_arn or phone):
        log.info("[caregiver notification] %s: %s", subject, body)
        return False
    sns, sent = _sns(), False
    if settings.sns_topic_arn:  # the caregiver's email, and anyone subscribed at deploy time
        sns.publish(TopicArn=settings.sns_topic_arn, Subject=subject[:99], Message=body)
        sent = True
    if phone:
        try:
            sns.publish(PhoneNumber=phone, Message=f"{subject}\n{body}"[:1500])
            sent = True
        except Exception as e:  # e.g. a number not verified in the SMS sandbox: the email still went
            log.warning("SMS to the caregiver failed: %s", e)
    return sent


async def send(subject: str, body: str, phone: str = "") -> bool:
    """Returns True when a real message was handed to SNS."""
    try:
        return await asyncio.to_thread(_publish, subject, body, phone)
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


def _change_email(old: str, new: str) -> None:
    sns = _sns()
    subs = _subscriptions(sns)
    if old and old.lower() != new.lower():
        for s in subs:  # a pending subscription can't be removed; it expires on its own
            if s["Endpoint"].lower() == old.lower() and s["SubscriptionArn"].startswith("arn:"):
                sns.unsubscribe(SubscriptionArn=s["SubscriptionArn"])
    if new and not any(s["Endpoint"].lower() == new.lower() and s["SubscriptionArn"].startswith("arn:") for s in subs):
        sns.subscribe(TopicArn=settings.sns_topic_arn, Protocol="email", Endpoint=new)  # sends the confirmation email


async def change_email(old: str, new: str) -> None:
    """Point the alerts at a new caregiver email (or none). New addresses get Amazon's confirmation email."""
    if not settings.sns_topic_arn or not (old or new):
        return
    try:
        await asyncio.to_thread(_change_email, old, new)
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
