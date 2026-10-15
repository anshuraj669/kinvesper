"""Reminders the Echo speaks itself, through the Alexa Reminders and Timers APIs.

One daily reminder per dose time ("Margaret, it's time for your Metformin and Lisinopril..."), created
with the skill request's API token once the person allows reminders (by voice: "set up my pill
reminders"). The medicine list lives in Kinvesper, so whenever it changes in Settings the reminders
are rebuilt on the next visit to the skill. One record per Alexa account remembers what was created.

One-off reminders ("remind me to call Priya at 5") are saved in Kinvesper by the voice agent's tools,
then pushed to the Echo. An important one is an alarm: a heads-up reminder 30 minutes before, and,
when it's due within 2 hours (the Timers API's limit), a timer that rings until "Alexa, stop".
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx

from .config import now, settings
from .i18n import action, hindi_time, joined, lang, t
from .models import Medication, Profile, Reminder
from .service import spoken_time
from .store import Store

log = logging.getLogger(__name__)

SCOPE = "alexa::alerts:reminders:skill:readwrite"
TIMER_SCOPE = "alexa::alerts:timers:skill:readwrite"
PATH = "/v1/alerts/reminders"
TIMER_PATH = "/v1/alerts/timers"
TIMEOUT_S = 4
HEADS_UP = timedelta(minutes=30)
MAX_TIMER = timedelta(hours=2)  # the Timers API's limit

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


def ringing(profile: Profile, meds: list[Medication], reminders: list[Reminder], since: datetime,
            current: datetime, locale: str = "en-US") -> list[tuple[str, str, bool]]:
    """What an Echo would say between `since` and `current`: dose reminders, one-off reminders and an
    important one's heads-up, as (key, text, is_alarm). The voice simulator rings these itself."""
    out: list[tuple[str, str, bool]] = []
    name = profile.preferred_name
    if profile.checkin_time:
        h, m = (int(x) for x in profile.checkin_time.split(":"))
        at = current.replace(hour=h, minute=m, second=0, microsecond=0)
        if since < at <= current:
            out.append((f"checkin:{at.date()}", t(locale, "checkin_prompt", name=name), False))
    for time, group in doses_by_time(meds).items():
        h, m = (int(x) for x in time.split(":"))
        at = current.replace(hour=h, minute=m, second=0, microsecond=0)
        if since < at <= current:
            out.append((f"pills:{at.date()}:{time}", reminder_text(profile, group, locale), False))
    for r in reminders:
        if r.status == "cancelled":
            continue
        at = datetime.fromisoformat(r.at).astimezone(settings.tz)
        if r.important and since < at - HEADS_UP <= current:
            out.append((f"{r.id}:heads_up", t(locale, "heads_up", name=name, text=r.text, action=action(r.text)), False))
        if since < at <= current:
            out.append((r.id, t(locale, "alarm" if r.important else "oneoff", name=name, text=r.text, action=action(r.text)), r.important))
    return out


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


def one_off_body(text: str, at: datetime, locale: str) -> dict:
    return {
        "requestTime": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000"),
        "trigger": {"type": "SCHEDULED_ABSOLUTE", "scheduledTime": at.strftime("%Y-%m-%dT%H:%M:%S.000"),
                    "timeZoneId": tz_name()},
        "alertInfo": {"spokenInfo": {"content": [{"locale": locale, "text": text[:250]}]}},
        "pushNotification": {"status": "ENABLED"},
    }


def timer_body(text: str, duration: timedelta, locale: str) -> dict:
    return {
        "duration": f"PT{max(1, int(duration.total_seconds()))}S",
        "timerLabel": text[:256],
        "creationBehavior": {"displayExperience": {"visibility": "VISIBLE"}},
        "triggeringBehavior": {
            "operation": {"type": "ANNOUNCE", "textToAnnounce": [{"locale": locale, "text": text[:250]}]},
            "notificationConfig": {"playAudible": True},
        },
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
        if profile.checkin_time:  # the daily "I'm okay" check-in, asked by the Echo itself
            prompt = t(locale, "checkin_prompt", name=profile.preferred_name)
            texts[profile.checkin_time] = f"{prompt} {texts[profile.checkin_time]}" if profile.checkin_time in texts else prompt
            texts = dict(sorted(texts.items()))
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
        log.info("Echo reminders set for %s", ", ".join(texts))
        return SyncResult("ok", list(groups))

    async def push(self, system: dict, profile: Profile, reminders: list[Reminder], locale: str) -> Outcome:
        """Put Kinvesper's upcoming one-off reminders on this Alexa account's Echo, and take cancelled
        ones off. Safe to call often: each reminder is pushed once."""
        user = ((system.get("user") or {}).get("userId")) or ""
        if not user or not system.get("apiAccessToken"):
            return "error"
        key, current = self._key(user), now()
        record: dict = self.store.get("echo_oneoff", key) or {}
        upcoming = {r.id: r for r in reminders if r.status == "scheduled"}
        outcome: Outcome = "ok"
        async with self._client(system) as http:
            try:
                for rid in [rid for rid in record if rid not in upcoming]:  # cancelled or done
                    entry = record.pop(rid)
                    for token in entry.get("tokens", []):
                        await http.delete(f"{PATH}/{token}")
                    if entry.get("timer"):
                        await http.delete(f"{TIMER_PATH}/{entry['timer']}")
                for r in upcoming.values():
                    at = datetime.fromisoformat(r.at).astimezone(settings.tz)
                    if r.id in record or at <= current:
                        continue
                    entry = {"tokens": []}
                    name = profile.preferred_name
                    times = [(at, t(locale, "alarm" if r.important else "oneoff", name=name, text=r.text, action=action(r.text)))]
                    if r.important and at - current > HEADS_UP + timedelta(minutes=5):
                        times.append((at - HEADS_UP, t(locale, "heads_up", name=name, text=r.text, action=action(r.text))))
                    for when_, text in times:
                        resp = await http.post(PATH, json=one_off_body(text, when_, locale))
                        if resp.status_code in (401, 403):
                            outcome = "no_permission"
                            break
                        resp.raise_for_status()
                        entry["tokens"].append(resp.json()["alertToken"])
                    if outcome == "no_permission":
                        break
                    if r.important and at - current <= MAX_TIMER:  # rings until "Alexa, stop"
                        resp = await http.post(TIMER_PATH, json=timer_body(times[0][1], at - current, locale))
                        if resp.status_code < 300:
                            entry["timer"] = resp.json().get("id")
                        else:
                            log.info("No ringing timer for %s (HTTP %s); the reminder still plays", r.id, resp.status_code)
                    record[r.id] = entry
            except (httpx.HTTPError, KeyError, ValueError) as e:
                log.warning("Couldn't put reminders on the Echo: %s", e)
                outcome = "error"
        self.store.put("echo_oneoff", key, record)
        return outcome

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
