"""Caregiver notifications over Amazon SNS (topic or direct SMS). Falls back to logging."""

from __future__ import annotations

import asyncio
import logging

from .config import settings

log = logging.getLogger(__name__)


def _publish(subject: str, body: str) -> bool:
    if not (settings.sns_topic_arn or settings.caregiver_sms):
        log.info("[caregiver notification] %s: %s", subject, body)
        return False
    import boto3

    sns = boto3.client("sns", region_name=settings.aws_region)
    if settings.sns_topic_arn:
        sns.publish(TopicArn=settings.sns_topic_arn, Subject=subject[:99], Message=body)
    else:
        sns.publish(PhoneNumber=settings.caregiver_sms, Message=f"{subject}\n{body}")
    return True


async def send(subject: str, body: str) -> bool:
    """Returns True when a real message was delivered to SNS."""
    try:
        return await asyncio.to_thread(_publish, subject, body)
    except Exception as e:  # never let a notification failure break a care action
        log.warning("SNS publish failed: %s", e)
        return False
