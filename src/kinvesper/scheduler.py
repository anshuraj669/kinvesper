"""Proactive checks on a timer: missed doses, inactivity, low stock.

Runs inside the server every SCHEDULER_INTERVAL_S seconds (0 turns it off). Deployed with more than
one instance, turn the loop off and trigger `kinvesper --check` from Amazon EventBridge Scheduler
instead. Alerts are de-duplicated, so overlapping runs never double-text the caregiver.
"""

from __future__ import annotations

import asyncio
import logging

from .config import settings
from .service import CareService

log = logging.getLogger(__name__)


async def check_once(service: CareService) -> int:
    raised = await service.run_checks()
    for a in raised:
        log.info("Alert raised: [%s] %s", a.level, a.title)
    return len(raised)


async def run_forever(service: CareService, interval: float | None = None) -> None:
    interval = settings.scheduler_interval_s if interval is None else interval
    if interval <= 0:
        log.info("Scheduler off (SCHEDULER_INTERVAL_S=0)")
        return
    log.info("Scheduler on: checks every %ss", interval)
    while True:
        try:
            await check_once(service)
        except Exception:
            log.exception("Scheduled check failed")
        await asyncio.sleep(interval)
