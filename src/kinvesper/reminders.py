"""Pill reminders the Echo speaks itself, through the Alexa Reminders API.

One daily reminder per dose time ("Margaret, it's time for your Metformin and Lisinopril..."), created
with the skill request's API token once the person allows reminders (by voice: "set up my pill
reminders"). The medicine list lives in Kinvesper, so whenever it changes in Settings the reminders
are rebuilt on the next visit to the skill. One record per Alexa account remembers what was created.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx

from .config import now, settings
from .i18n import hindi_time, joined, lang, t
from .models import Medication, Profile
from .service import spoken_time
from .store import Store

log = logging.getLogger(__name__)

SCOPE = "alexa::alerts:reminders:skill:readwrite"
PATH = "/v1/alerts/reminders"
TIMEOUT_S = 4

Outcome = Literal["ok", "unchanged", "no_permission", "error"]


@dataclass
class SyncResult:
    outcome: Outcome
    times: list[str]  # HH:MM, the reminders now set


def tz_name() -> str:
    return getattr(settings.tz, "key", None) or "UTC"


def doses_by_time(meds: list[Medication]) -> dict[str, list[Medication]]:
    by_time: dict[str, list[Medication]] = {}
    for m in meds:
        for time in m.times:
            by_time.setdefault(time, []).append(m)
    return dict(sorted(by_time.items()))


def reminder_text(profile: Profile, meds: list[Medication], locale: str = "en-US") -> str:
    listed = joined([f"{m.name}, {m.dose}" for m in meds], locale)
    notes = " ".join(f"{m.name}: {m.instructions.rstrip('.')}." for m in meds if m.instructions)
    return t(locale, "reminder", name=profile.preferred_name, medicines=listed, notes=notes).replace("  ", " ")


def reminder_body(text: str, time: str, locale: str) -> dict:
    hour, minute = time.split(":")
    today = now().strftime("%Y-%m-%d")
    return {
        "requestTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000"),
        "trigger": {
            "type": "SCHEDULED_ABSOLUTE",
            "timeZoneId": tz_name(),
            "recurrence": {
                "startDateTime": f"{today}T{time}:00.000",
                "recurrenceRules": [f"FREQ=DAILY;BYHOUR={int(hour)};BYMINUTE={int(minute)};BYSECOND=0;INTERVAL=1;"],
            },
        },
        "alertInfo": {"spokenInfo": {"content": [{"locale": locale, "text": text[:250]}]}},
        "pushNotification": {"status": "ENABLED"},
    }


def spoken_times(times: list[str], locale: str = "en-US") -> str:
    return joined([hindi_time(x) if lang(locale) == "hi" else spoken_time(x) for x in times], locale)


class EchoReminders:
    def __init__(self, store: Store, transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self._transport = transport

    @staticmethod
    def _key(user_id: str) -> str:
        return hashlib.sha256(user_id.encode()).hexdigest()[:32]

    def _client(self, system: dict) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=system.get("apiEndpoint", "https://api.amazonalexa.com"), timeout=TIMEOUT_S,
            headers={"Authorization": f"Bearer {system.get('apiAccessToken', '')}"}, transport=self._transport)

    async def sync(self, system: dict, profile: Profile, meds: list[Medication], locale: str,
                   force: bool = False) -> SyncResult:
        """Make the account's Kinvesper reminders match the medicine list."""
        user = ((system.get("user") or {}).get("userId")) or ""
        if not user or not system.get("apiAccessToken"):
            return SyncResult("error", [])
        groups = doses_by_time(meds)
        texts = {time: reminder_text(profile, ms, locale) for time, ms in groups.items()}
        signature = hashlib.sha256(json.dumps([texts, locale, tz_name()], sort_keys=True).encode()).hexdigest()
        record = self.store.get("echo_reminders", self._key(user)) or {}
        if record.get("signature") == signature and not force:
            return SyncResult("unchanged", list(groups))
        async with self._client(system) as http:
            try:
                for token in record.get("tokens", []):  # ours from last time; gone already is fine
                    r = await http.delete(f"{PATH}/{token}")
                    if r.status_code in (401, 403):
                        return SyncResult("no_permission", [])
                tokens = []
                for time, text in texts.items():
                    r = await http.post(PATH, json=reminder_body(text, time, locale))
                    if r.status_code in (401, 403):
                        return SyncResult("no_permission", [])
                    r.raise_for_status()
                    tokens.append(r.json()["alertToken"])
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.warning("Couldn't set the Echo reminders: %s", e)
                return SyncResult("error", [])
        self.store.put("echo_reminders", self._key(user), {"signature": signature, "tokens": tokens})
        log.info("Echo reminders set for %s", ", ".join(groups))
        return SyncResult("ok", list(groups))

    async def clear(self, system: dict) -> Outcome:
        user = ((system.get("user") or {}).get("userId")) or ""
        record = self.store.get("echo_reminders", self._key(user)) or {}
        async with self._client(system) as http:
            try:
                for token in record.get("tokens", []):
                    r = await http.delete(f"{PATH}/{token}")
                    if r.status_code in (401, 403):
                        return "no_permission"
            except httpx.HTTPError as e:
                log.warning("Couldn't remove the Echo reminders: %s", e)
                return "error"
        self.store.delete("echo_reminders", self._key(user))
        return "ok"
