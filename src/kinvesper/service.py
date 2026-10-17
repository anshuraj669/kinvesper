"""CareService: all care logic. MCP tools, the dashboard API and the scheduler call into this."""

from __future__ import annotations

import base64
import contextlib
import difflib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from html import escape

import anyio

from . import (
    contacts,
    i18n,
    interactions,
    llm,
    media,
    notify,
    scam,
    speech,
    telegram,
    when,
)
from .config import now, settings
from .models import (
    Alert,
    AlertKind,
    AlertLevel,
    CareSummary,
    CheckIn,
    CheckInResult,
    ContactResult,
    ConversationStarter,
    DayAdherence,
    DoseLog,
    FamilyMessage,
    InteractionResult,
    LogDoseResult,
    Medication,
    Memory,
    MemoryResult,
    MessagesResult,
    NotifyResult,
    Profile,
    Refill,
    RefillResult,
    Reminder,
    ReminderResult,
    SafeWordResult,
    ScamAssessment,
    ScannedMedicine,
    ScanResult,
    ScheduleItem,
    ScheduleResult,
    TelegramLink,
    TrustedContact,
)
from .store import Store

FILLER = {"my", "the", "a", "pill", "pills", "medicine", "medication", "meds", "tablet", "tablets", "dose", "for", "i", "take"}
SERIOUS = {
    "fell": "reported a fall",
    "fall": "reported a fall",
    "chest pain": "chest pain",
    "can't breathe": "trouble breathing",
    "short of breath": "trouble breathing",
    "trouble breathing": "trouble breathing",
    "dizz": "dizziness",  # stems: substring match covers dizzy / dizziness, confused / confusion
    "lightheaded": "dizziness",
    "light-headed": "dizziness",
    "confus": "confusion",
}
MILD = {"lonely": "feeling lonely", "sad": "feeling sad", "headache": "headache", "nausea": "nausea", "tired": "fatigue", "pain": "pain"}
DUE_WINDOW = timedelta(minutes=30)  # a dose counts as "due" from 30 min before its time
MATCH_WINDOW = timedelta(hours=4)  # a logged dose satisfies a slot within 4 hours of it
DELIVERY_TIME = timedelta(hours=3)
AWAKE = ("08:00", "21:00")  # inactivity is only worth a text during waking hours
OPEN_REFILL = {"requested", "contacting_pharmacy", "verifying", "approved"}


def _hhmm(t: str) -> tuple[int, int]:
    h, m = t.split(":")
    return int(h), int(m)


def _at(day: datetime, t: str) -> datetime:
    h, m = _hhmm(t)
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def spoken_time(t: str) -> str:
    h, m = _hhmm(t)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"


def _spoken_dt(iso: str) -> str:
    return spoken_time(datetime.fromisoformat(iso).strftime("%H:%M"))


def clean_message(text: str, caregiver: str) -> str:
    """Tidy a message the voice assistant passes along: undo JSON escapes some models leave in
    ("I\\u0027ll" -> "I'll") and drop a leading "Tell Priya (that)" or "Let Priya know (that)"."""
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text).strip().strip('"\'').strip()
    name = re.escape(caregiver)
    rest = re.sub(rf"^(please\s+)?(tell\s+{name}|let\s+{name}\s+know)(\s+that)?[\s,:]+", "", text, flags=re.IGNORECASE)
    return (rest[:1].upper() + rest[1:]) if rest else text


TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
PROFILE_FIELDS = ("patient_name", "preferred_name", "caregiver_name", "caregiver_relation",
                  "caregiver_phone", "caregiver_email", "backup_name", "backup_phone", "backup_email", "pharmacy_name",
                  "emergency_number", "checkin_time")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
LINK_LIFETIME = timedelta(minutes=30)
ANSWER_WAIT = timedelta(hours=12)  # how long a question from Telegram waits for its answer
ANSWER_NOW = timedelta(minutes=10)  # a message for the asker this soon after hearing the question answers it
QUESTION_START = re.compile(r"^(ask|did|do|does|are|is|was|were|have|has|had|how|what|when|where|which|who|why|can|could|"
                            r"will|would|should|shall|may)\b", re.IGNORECASE)
ASK_PREFIX = re.compile(r"^(please\s+)?(ask|check with)\s+(her|him|them|mum|mom|mother|dad|father|grandma|grandpa|nana|"
                        r"nani|dadi|\w+)\s*(if|whether|about)?\s*[,:-]?\s*", re.IGNORECASE)
RELAY_SYSTEM = """\
A family member typed a message in a chat for {name}, an older adult, to be read aloud to them by their
Echo. Decide whether it asks {name} something that needs an answer back ("Ask her if she ate", "did you
sleep well?", "how was the doctor?"). Then write `say`: the words to read to {name}, spoken to them
directly and kept short. Reword only messages written about {name} or as an instruction to relay ("ask
her does she take her pills today" becomes "Did you take your pills today?", "tell mum I love her"
becomes "I love you"); otherwise keep the message exactly as typed, in its own language."""
RELAY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["question", "say"],
                "properties": {"question": {"type": "boolean"}, "say": {"type": "string"}}}
ACK_WORDS = {"ok", "okay", "k", "1", "yes", "on it", "i'm on it", "im on it", "got it", "done", "handled", "seen", "👍"}
STATUS_WORDS = {"status", "update", "today", "how is she", "how is he", "how is mum", "how is mom", "how is dad", "?"}
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
# Companionship: gentle questions about their life, one at a time. What they share is kept for the family.
STARTERS = [
    ("childhood", "Where did you grow up, and what was your home like?"),
    ("food", "What's a dish from your childhood that you still remember the taste of?"),
    ("work", "What work did you do, and what did you enjoy most about it?"),
    ("festivals", "How did your family celebrate festivals or holidays when you were young?"),
    ("music", "What songs did you love when you were young?"),
    ("friends", "Who was your best friend growing up, and what did you get up to together?"),
    ("family", "What's a favourite memory of your children when they were small?"),
    ("travel", "What's the most beautiful place you've ever been?"),
    ("proud", "What are you most proud of in your life?"),
    ("advice", "What advice would you give your grandchildren?"),
]
REPEAT_TOPICS = {  # questions asked again and again in one day, a gentle early sign worth a look
    "schedule": "asked about the medicine schedule",
    "reminders": "asked which reminders are set",
    "double_dose": "tried to log a dose that was already taken",
}
SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "medicines": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "dose": {"type": "string"}, "purpose": {"type": "string"},
                "times": {"type": "array", "items": {"type": "string"}}, "instructions": {"type": "string"},
                "pills_remaining": {"type": "integer"},
            },
            "required": ["name", "dose", "purpose", "times", "instructions", "pills_remaining"],
            "additionalProperties": False}},
        "notes": {"type": "string"},
    },
    "required": ["medicines", "notes"], "additionalProperties": False,
}
SCAN_SYSTEM = """You read a photo of a prescription, a pharmacy label or a medicine box for a family setting up their parent's
medicine reminders. List every medicine you can read. For each:
- name: the medicine name as printed (brand or generic), without the strength
- dose: the strength per dose, e.g. "10 mg", "500 mg", "1 tablet"
- purpose: what it's for in two or three plain words if printed or well known (e.g. "blood pressure"), else ""
- times: daily times in 24-hour HH:MM. Once daily or "OD" = ["08:00"] (morning) or ["21:00"] (bedtime/night,
  "HS"); twice ("BD", "BID") = ["08:00", "20:00"]; three times ("TDS", "TID") = ["08:00", "14:00", "20:00"];
  four times ("QID") = ["08:00", "12:00", "16:00", "20:00"]; before breakfast = ["07:00"]; as needed ("SOS", "PRN") = []
- instructions: short directions printed, e.g. "take with food" (else "")
- pills_remaining: the quantity dispensed if printed, else 0
In notes, say in one or two plain sentences anything to double-check (hard-to-read parts, "as needed" medicines,
a course with an end date). If the photo isn't a prescription or medicine label, return no medicines and say so in
notes. Never guess a medicine you can't read."""
ASK_SYSTEM = """You are Kinvesper, answering a family caregiver's questions about the parent they care for, from the care data
below. Answer in two to five plain sentences, warmly and factually, with numbers where they help. Use only the
data: if it doesn't cover the question (Kinvesper doesn't track sleep, meals or steps, for example), say so and
mention what it does track. Never diagnose; for health worries suggest talking to the doctor. Plain text, no
markdown. Say dates as weekdays ("on Tuesday") when they're within the week."""


log = logging.getLogger(__name__)
MAX_VOICE_S = 180
MOOD_SCORE = {"great": 5, "good": 4, "okay": 3, "low": 2, "unwell": 1}
# Someone claiming to be family or a friend on the phone: the moment to ask for the family's safe word.
CLAIMS_FAMILY = re.compile(r"grand(son|daughter|child|kid|ma|pa)|\b(son|daughter|nephew|niece|brother|sister|cousin|"
                           r"uncle|aunt|relative|family|friend|beta|beti|it'?s me)\b", re.IGNORECASE)
PHOTO_SYSTEM = """You describe a photo a family member sent to {name}, an older adult whose Echo will say it aloud (their
eyesight may be poor). One or two short, warm sentences about who and what is in it, e.g. "It's Priya and
the two children on a beach, all smiling." Don't guess names you can't know; say "a little boy" instead.
Plain text."""
TRENDS_CACHE_S = 120


class SettingsError(ValueError):
    """A settings form value that can't be saved; the message is shown to the caregiver."""


class CareService:
    def __init__(self, store: Store):
        self.store = store

    # ---------- profile & activity ----------

    def profile(self) -> Profile:
        doc = self.store.get("profile", "main")
        if not doc:
            raise RuntimeError("No profile. Seed demo data with `kinvesper --seed`.")
        return Profile.model_validate(doc)

    def touch_activity(self) -> None:
        p = self.profile()
        p.last_activity = now().isoformat()
        self.store.save("profile", p, id="main")
        date = p.last_activity[:10]  # per-day count: how much they talk to Kinvesper is a wellbeing signal
        day = self.store.get("activity", date) or {"date": date, "count": 0}
        day["count"] += 1
        self.store.put("activity", date, day)

    # ---------- medications ----------

    def medications(self) -> list[Medication]:
        meds = [m for m in self.store.load_all("med", Medication) if m.active]
        return sorted(meds, key=lambda m: (min(m.times), m.name))

    def find_medication(self, query: str) -> Medication | None:
        meds = self.medications()
        words = [w for w in re.findall(r"[a-z']+", query.lower()) if w not in FILLER]
        q = " ".join(words)
        if not q:
            return None
        for m in meds:
            if m.name.lower() in q or q in m.name.lower():
                return m
        # purpose match ("blood pressure pill" -> Lisinopril)
        best, best_score = None, 0
        for m in meds:
            purpose_words = set(re.findall(r"[a-z]+", m.purpose.lower()))
            score = len(purpose_words & set(words))
            if score > best_score:
                best, best_score = m, score
        if best:
            return best
        # misspellings ("lisinoprill")
        names = {m.name.lower(): m for m in meds}
        for w in words:
            close = difflib.get_close_matches(w, names, n=1, cutoff=0.7)
            if close:
                return names[close[0]]
        return None

    def add_medication(self, med: Medication) -> Medication:
        self.store.save("med", med)
        return med

    def _monitored_since(self) -> str | None:
        """The first day Kinvesper was in use (its first dose, check-in or conversation). Days before it
        aren't counted as missed doses."""
        dates = [d.date for d in self.store.load_all("dose", DoseLog)] + [c.at[:10] for c in self.store.load_all("checkin", CheckIn)]
        if last := self.profile().last_activity:
            dates.append(last[:10])
        return min(dates) if dates else None

    @staticmethod
    def _expected(m: Medication, slot: datetime, since: str | None) -> bool:
        """Was this dose due: on a monitored day, and after the medicine was added to the list?"""
        if since and slot.date().isoformat() < since:
            return False
        return not m.added_at or slot >= datetime.fromisoformat(m.added_at)

    # ---------- one-off reminders and alarms ----------

    def reminders(self, upcoming_only: bool = True) -> list[Reminder]:
        items = self.store.load_all("reminder", Reminder)
        if upcoming_only:
            items = [r for r in items if r.status == "scheduled"]
        return sorted(items, key=lambda r: r.at)

    async def add_reminder(self, text: str, at: str = "", important: bool = False,
                           in_minutes: int | None = None) -> ReminderResult:
        current = now()
        try:
            due = current + timedelta(minutes=in_minutes) if in_minutes else when.local(datetime.fromisoformat(at))
        except ValueError:
            return ReminderResult(message="I didn't catch the time. When should I remind you?")
        if current - timedelta(minutes=2) < due <= current + timedelta(seconds=30):
            due = current + timedelta(minutes=1)  # "in a minute", worked out from a clock that shows no seconds
        text = text.strip().rstrip(".")
        if not text:
            return ReminderResult(message="What should I remind you about?")
        if due <= current + timedelta(seconds=30):
            return ReminderResult(message="That time has already passed. When should I remind you?")
        if due > current + timedelta(days=366):
            return ReminderResult(message="I can only set reminders up to a year ahead.")
        r = Reminder(text=text, at=due.isoformat(timespec="minutes"), important=important,
                     created_at=current.isoformat())
        self.store.save("reminder", r)
        said = when.spoken(due, current)
        p = self.profile()
        also = (f"I'll remind you 30 minutes before as well, and let {p.caregiver_name} know at the time."
                if due - current > timedelta(minutes=35) else f"I'll let {p.caregiver_name} know at the time as well.")
        msg = f"Okay, important alarm set {said}: {text}. {also}" if important else f"Okay, I'll remind you {said}: {text}."
        return ReminderResult(reminder=r, message=msg)

    def list_reminders(self) -> ReminderResult:
        items, current = self.reminders(), now()
        if not items:
            return ReminderResult(message="You don't have any reminders set.")
        said = [f"{'an important alarm for ' if r.important else ''}{r.text} {when.spoken(when.local(datetime.fromisoformat(r.at)), current)}"
                for r in items[:5]]
        more = f", and {len(items) - 5} more" if len(items) > 5 else ""
        return ReminderResult(reminders=items, message=f"You have {len(items)} reminder{'s' * (len(items) > 1)}: "
                              + "; ".join(said) + more + ".")

    def cancel_reminder(self, query: str = "", reminder_id: str = "") -> ReminderResult:
        items = self.reminders()
        if reminder_id:
            match = next((r for r in items if r.id == reminder_id), None)
        else:
            words = set(re.findall(r"[a-z]+", query.lower())) - FILLER - {"reminder", "alarm", "cancel", "delete", "remove", "my"}
            scored = sorted(((len(words & set(re.findall(r"[a-z]+", r.text.lower()))), r) for r in items),
                            key=lambda x: -x[0])
            match = scored[0][1] if scored and scored[0][0] else (items[0] if len(items) == 1 else None)
        if match is None:
            return ReminderResult(reminders=items, message="I couldn't find that reminder. " + self.list_reminders().message)
        match.status = "cancelled"
        self.store.save("reminder", match)
        return ReminderResult(reminder=match, message=f"Okay, I've cancelled the reminder for {match.text}.")

    async def _due_reminders(self, current: datetime) -> None:
        """Due reminders are done; for an important one the caregiver hears about it, once."""
        p = self.profile()
        for r in self.reminders():
            due = when.local(datetime.fromisoformat(r.at))
            if due > current:
                continue
            r.status = "done"
            if r.important and not r.caregiver_notified:
                await self.raise_alert(
                    "warning", "reminder", f"{p.preferred_name}'s important reminder: {r.text}",
                    f"Set for {when.spoken(due, current).removeprefix('at ')}. You may want to check in with {p.preferred_name}.",
                    dedupe_key=f"reminder:{r.id}", sms=True)
                r.caregiver_notified = True
            self.store.save("reminder", r)

    # ---------- settings (caregiver dashboard) ----------

    async def settings_view(self) -> dict:
        p = self.profile()
        return {
            "profile": {k: getattr(p, k) for k in PROFILE_FIELDS},
            "medications": [m.model_dump() for m in self.medications()],
            "contacts": [c.model_dump() for c in self.store.load_all("contact", TrustedContact)],
            "email_status": await notify.email_status(p.caregiver_email),
            "backup_email_status": await notify.email_status(p.backup_email),
            "escalate_after_min": settings.escalate_after_min,
            "low_stock_threshold": settings.low_stock_threshold,
            "checkin_grace_min": settings.checkin_grace_min,
            "sms": bool(p.caregiver_phone),
            "calls": notify.calls_enabled(),
            "telegram": {"enabled": telegram.enabled(), "bot": await telegram.bot_username() if telegram.enabled() else "",
                         "caregiver": p.caregiver_telegram_name if p.caregiver_telegram else "",
                         "backup": p.backup_telegram_name if p.backup_telegram else ""},
            "scan": llm.enabled(),
            "safe_word": self.safe_word(),  # the caregiver set it; only this guarded page shows it
        }

    async def update_settings(self, profile: dict, medications: list[dict], trusted: list[dict] | None = None) -> dict:
        """Replace the household's profile and medicine list. Medicines left out are retired, not
        deleted, so their dose history stays. Raises SettingsError for a value that can't be saved."""
        old = self.profile()
        fields = {k: str(profile.get(k, getattr(old, k)) or "").strip() for k in PROFILE_FIELDS}
        for k in ("patient_name", "preferred_name", "caregiver_name"):
            if not fields[k]:
                raise SettingsError(f"{k.replace('_', ' ').capitalize()} is required.")
        for who in ("caregiver", "backup"):
            fields[f"{who}_phone"] = re.sub(r"[\s\-().]", "", fields[f"{who}_phone"])
            if fields[f"{who}_phone"] and not notify.PHONE.match(fields[f"{who}_phone"]):
                raise SettingsError(f"The {who}'s phone must include the country code, like +15551234567.")
            if fields[f"{who}_email"] and not notify.EMAIL.match(fields[f"{who}_email"]):
                raise SettingsError(f"The {who}'s email address doesn't look right.")
        if fields["checkin_time"]:
            fields["checkin_time"] = fields["checkin_time"].zfill(5)
            if not TIME.match(fields["checkin_time"]):
                raise SettingsError("The daily check-in time must be 24-hour HH:MM, like 10:00.")
        if not re.fullmatch(r"\d{2,6}", fields["emergency_number"]):
            raise SettingsError("The emergency number should be the short local number, like 112, 911 or 999.")
        if (fields["backup_phone"] or fields["backup_email"] or old.backup_telegram) and not fields["backup_name"]:
            raise SettingsError("Add the backup contact's name.")
        if not (fields["caregiver_phone"] or fields["caregiver_email"] or old.caregiver_telegram):
            raise SettingsError("Add a phone number, an email or Telegram, so alerts reach the caregiver.")

        current = {m.id: m for m in self.store.load_all("med", Medication)}
        meds = [self._medication(m, current) for m in medications]
        if not meds:
            raise SettingsError("Add at least one medicine.")
        saved_contacts = None if trusted is None else [self._contact(c) for c in trusted]

        self.store.save("profile", old.model_copy(update=fields), id="main")
        kept = {m.id for m in meds}
        for m in meds:
            self.store.save("med", m)
        for m in current.values():
            if m.active and m.id not in kept:
                self.store.save("med", m.model_copy(update={"active": False}))
        if saved_contacts is not None:
            for c in self.store.load_all("contact", TrustedContact):
                if c.id not in {x.id for x in saved_contacts}:
                    self.store.delete("contact", c.id)
            for c in saved_contacts:
                self.store.save("contact", c)
        await notify.change_email(old.caregiver_email, fields["caregiver_email"])
        await notify.change_email(old.backup_email, fields["backup_email"], audience="backup")
        before = {interactions.base_name(m.name) for m in current.values() if m.active}
        return {**await self.settings_view(), "added": [m.name for m in meds if interactions.base_name(m.name) not in before]}

    async def send_test_alert(self) -> tuple[Alert, bool]:
        """A test text and email, and (with calls set up) a test call; pressing 1 on it marks the test handled."""
        p = self.profile()
        alert = await self.raise_alert(
            "info", "test", "Test alert",
            f"This is how Kinvesper will reach you, {p.caregiver_name}, when something needs your attention.", sms=True)
        called = await notify.call(
            p.caregiver_phone, f"Hello {p.caregiver_name}, this is Kinvesper testing alert calls. Nothing is wrong with "
            f"{p.preferred_name}. When there's an emergency, a call like this reads out what happened.", self._ack_url(alert))
        return alert, called

    @staticmethod
    def _contact(data: dict) -> TrustedContact:
        name = str(data.get("name") or "").strip()
        phone = re.sub(r"[\s\-().]", "", str(data.get("phone") or ""))
        if not name:
            raise SettingsError("Every trusted contact needs a name.")
        if not re.fullmatch(rf"\+?\d{{{contacts.MIN_DIGITS},15}}", phone):
            raise SettingsError(f"{name}: the phone number should be digits, with the country code if it has one.")
        values = {"name": name, "relation": str(data.get("relation") or "").strip(), "phone": phone}
        return TrustedContact(id=data["id"], **values) if data.get("id") else TrustedContact(**values)

    @staticmethod
    def _medication(data: dict, current: dict[str, Medication]) -> Medication:
        name = str(data.get("name") or "").strip()
        if not name:
            raise SettingsError("Every medicine needs a name.")
        times = data.get("times") or []
        if isinstance(times, str):
            times = times.replace(",", " ").split()
        times = sorted({t.strip().zfill(5) for t in times})
        if not times or not all(TIME.match(t) for t in times):
            raise SettingsError(f"{name}: times must be 24-hour HH:MM, like 08:00 or 20:30.")
        try:
            pills = int(data.get("pills_remaining", 30))
        except (TypeError, ValueError):
            pills = -1
        if pills < 0:
            raise SettingsError(f"{name}: pills left must be a whole number.")
        values = {
            "name": name, "dose": str(data.get("dose") or "").strip(), "times": times, "pills_remaining": pills,
            "purpose": str(data.get("purpose") or "").strip(), "instructions": str(data.get("instructions") or "").strip(),
            "active": True,
        }
        if not values["dose"]:
            raise SettingsError(f"{name}: add the dose, like 10 mg.")
        existing = current.get(str(data.get("id") or ""))
        return existing.model_copy(update=values) if existing else Medication(**values, added_at=now().isoformat())

    # ---------- doses & schedule ----------

    def _logs_for(self, date: str) -> dict[tuple[str, str], DoseLog]:
        return {(d.medication_id, d.slot): d for d in self.store.load_all("dose", DoseLog) if d.date == date}

    def _status(self, slot_dt: datetime, current: datetime, taken: bool) -> str:
        if taken:
            return "taken"
        if current > slot_dt + timedelta(minutes=settings.missed_dose_grace_min):
            return "missed"
        if current >= slot_dt - DUE_WINDOW:
            return "due"
        return "upcoming"

    def schedule(self, medication: str | None = None) -> ScheduleResult:
        current = now()
        date = current.date().isoformat()
        logs = self._logs_for(date)
        meds = self.medications()
        if medication:
            match = self.find_medication(medication)
            meds = [match] if match else []
        items = []
        for m in meds:
            for t in m.times:
                log = logs.get((m.id, t))
                if not log and not self._expected(m, _at(current, t), None):
                    continue  # added to the list after this time today: first due tomorrow
                items.append(
                    ScheduleItem(
                        medication=m.name,
                        dose=m.dose,
                        purpose=m.purpose,
                        time=t,
                        status=self._status(_at(current, t), current, log is not None),
                        taken_at=log.taken_at if log else None,
                        instructions=m.instructions,
                    )
                )
        items.sort(key=lambda i: (i.time, i.medication))
        low = [m.name for m in meds if m.pills_remaining <= settings.low_stock_threshold]
        return ScheduleResult(date=date, items=items, low_stock=low, spoken_summary=self._speak_schedule(items, low, medication))

    def _speak_schedule(self, items: list[ScheduleItem], low: list[str], medication: str | None) -> str:
        if medication and not items:
            return f"I couldn't find a medication called {medication} on your list."
        if medication:
            parts = []
            for i in items:
                if i.status == "taken":
                    parts.append(f"Yes, you took your {spoken_time(i.time)} {i.medication} at {_spoken_dt(i.taken_at)}.")
                elif i.status == "missed":
                    parts.append(f"You haven't taken your {spoken_time(i.time)} {i.medication} yet, and it's overdue.")
                elif i.status == "due":
                    parts.append(f"Your {spoken_time(i.time)} {i.medication} is due now and you haven't taken it yet.")
                else:
                    parts.append(f"Your next {i.medication} is at {spoken_time(i.time)}.")
            text = " ".join(parts)
        else:
            pending = [i for i in items if i.status in ("due", "missed")]
            upcoming = [i for i in items if i.status == "upcoming"]
            taken = sum(i.status == "taken" for i in items)
            text = f"You've taken {taken} of {len(items)} doses today."
            if pending:
                text += " Still to take now: " + ", ".join(f"{i.medication} from {spoken_time(i.time)}" for i in pending) + "."
            if upcoming:
                n = upcoming[0]
                text += f" Next up is {n.medication} at {spoken_time(n.time)}."
        if low:
            text += f" You're running low on {', '.join(low)}. I can order a refill."
        return text

    def log_dose(self, medication: str, taken_at: datetime | None = None, source: str = "voice") -> LogDoseResult:
        med = self.find_medication(medication)
        if not med:
            names = ", ".join(m.name for m in self.medications())
            return LogDoseResult(status="not_found", message=f"I couldn't find {medication}. Your medications are {names}.")
        current = taken_at or now()
        date = current.date().isoformat()
        logs = self._logs_for(date)

        slots = sorted(med.times, key=lambda t: abs(_at(current, t) - current))
        near = [t for t in slots if abs(_at(current, t) - current) <= MATCH_WINDOW]
        open_slots = [t for t in near if (med.id, t) not in logs]
        # an overdue dose from earlier today also counts
        overdue = [t for t in med.times if (med.id, t) not in logs and _at(current, t) < current and t not in open_slots]

        if not open_slots and not overdue:
            # `slots` is ordered by closeness to now, so this is the most relevant dose already taken today
            taken = [t for t in slots if (med.id, t) in logs and _at(current, t) - MATCH_WINDOW <= current]
            if taken:
                prev = logs[(med.id, taken[0])]
                return LogDoseResult(
                    status="already_taken",
                    medication=med.name,
                    slot=taken[0],
                    taken_at=prev.taken_at,
                    pills_remaining=med.pills_remaining,
                    message=(
                        f"You already took your {spoken_time(taken[0])} {med.name} at {_spoken_dt(prev.taken_at)}. "
                        "Please don't take another one. Taking a double dose could be unsafe."
                    ),
                )
            nxt = min(med.times, key=lambda t: (_at(current, t) - current) % timedelta(days=1))
            return LogDoseResult(
                status="not_due",
                medication=med.name,
                message=f"Your {med.name} isn't due right now. The next dose is at {spoken_time(nxt)}.",
            )

        slot = (open_slots or overdue)[0]
        self.store.save("dose", DoseLog(medication_id=med.id, date=date, slot=slot, taken_at=current.isoformat(), source=source))
        med.pills_remaining = max(0, med.pills_remaining - med.pills_per_dose)
        self.store.save("med", med)
        low = med.pills_remaining <= settings.low_stock_threshold
        msg = f"Got it. I've logged your {spoken_time(slot)} {med.name}."
        if med.instructions:
            msg += f" Remember: {med.instructions}."
        if low:
            msg += f" You have {med.pills_remaining} left, so I'd suggest ordering a refill."
        return LogDoseResult(
            status="logged", medication=med.name, slot=slot, taken_at=current.isoformat(),
            pills_remaining=med.pills_remaining, low_stock=low, message=msg,
        )

    # ---------- refills ----------

    def refills(self) -> list[Refill]:
        return sorted(self.store.load_all("refill", Refill), key=lambda r: r.created_at, reverse=True)

    async def request_refill(
        self,
        medication: str,
        fulfillment: str = "pickup",
        on_status: Callable[[Refill], Awaitable[None]] | None = None,
        step_seconds: float = 0,
    ) -> RefillResult:
        """Walk a refill through a simulated pharmacy. `on_status` is called after every step."""
        med = self.find_medication(medication)
        if not med:
            names = ", ".join(m.name for m in self.medications())
            return RefillResult(message=f"I couldn't find {medication}. Your medications are {names}.")
        for r in self.refills():
            if r.medication_id == med.id and r.status in OPEN_REFILL:
                return RefillResult(refill=r, message=f"A refill for {med.name} is already on its way. {r.status_message}.")

        pharmacy = self.profile().pharmacy_name
        stamp = now().isoformat()
        refill = Refill(medication_id=med.id, medication_name=med.name, fulfillment=fulfillment,
                        status_message=f"Refill requested for {med.name}", created_at=stamp, updated_at=stamp)
        steps = [
            ("contacting_pharmacy", f"Sending the prescription to {pharmacy}"),
            ("verifying", "The pharmacist is checking the prescription"),
            ("approved", f"Approved. {pharmacy} is preparing {med.name}"),
            ("ready", f"{med.name} is ready for pickup" if fulfillment == "pickup" else f"{med.name} is out for delivery"),
        ]
        try:
            self.store.save("refill", refill)
            if on_status:
                await on_status(refill)
            for status, text in steps:
                await anyio.sleep(step_seconds)
                refill.status, refill.status_message, refill.updated_at = status, text, now().isoformat()
                self.store.save("refill", refill)
                if on_status:
                    await on_status(refill)
        except BaseException:
            refill.status, refill.status_message = "failed", "The refill request was interrupted"
            self.store.save("refill", refill)
            raise

        if fulfillment == "pickup":
            refill.ready_by = refill.updated_at
            msg = f"Good news! Your {med.name} refill is ready to pick up at {pharmacy}."
        else:
            ready = now() + DELIVERY_TIME
            refill.ready_by = ready.isoformat()
            msg = f"Good news! {pharmacy} is delivering your {med.name}. It should arrive by {spoken_time(ready.strftime('%H:%M'))}."
        self.store.save("refill", refill)
        where = "ready for pickup" if fulfillment == "pickup" else "out for delivery"
        await self.raise_alert("info", "refill", f"{med.name} refill {where}", msg, dedupe_key=f"refill:{refill.id}")
        return RefillResult(refill=refill, message=msg)

    # ---------- alerts ----------

    def alerts(self, include_acknowledged: bool = True) -> list[Alert]:
        items = self.store.load_all("alert", Alert)
        if not include_acknowledged:
            items = [a for a in items if not a.acknowledged]
        return sorted(items, key=lambda a: a.created_at, reverse=True)

    async def raise_alert(
        self, level: AlertLevel, kind: AlertKind, title: str, detail: str = "",
        dedupe_key: str | None = None, sms: bool | None = None,
    ) -> Alert:
        if dedupe_key:
            for a in self.store.load_all("alert", Alert):
                if a.dedupe_key == dedupe_key:
                    return a
        alert = Alert(created_at=now().isoformat(), level=level, kind=kind, title=title, detail=detail, dedupe_key=dedupe_key)
        if sms if sms is not None else level != "info":
            p = self.profile()
            sent = await notify.send(f"Kinvesper: {title}", f"{detail}\n— about {p.patient_name}", phone=p.caregiver_phone)
            alert.notified = await self._telegram_alert(alert, p.caregiver_telegram) or sent
        self.store.save("alert", alert)
        return alert

    def acknowledge(self, alert_id: str) -> Alert | None:
        alert = self.store.load("alert", alert_id, Alert)
        if alert:
            if not alert.acknowledged:
                alert.acknowledged_at = now().isoformat()
            alert.acknowledged = True
            self.store.save("alert", alert)
        return alert

    async def message_caregiver(self, message: str, urgent: bool = False, confirmed_by_user: bool | None = None) -> NotifyResult:
        p = self.profile()
        alert = await self.raise_alert(
            "urgent" if urgent else "warning", "caregiver_message",
            f"Message from {p.preferred_name}", message, sms=True,
        )
        return NotifyResult(sent=True, alert_id=alert.id, confirmed_by_user=confirmed_by_user,
                            message=f"Okay, I've sent that to {p.caregiver_name}.")

    async def emergency(self, description: str) -> NotifyResult:
        p = self.profile()
        alert = await self.raise_alert("urgent", "emergency", f"EMERGENCY: {p.preferred_name} needs help", description, sms=True)
        await self._ring(alert)  # an emergency doesn't wait: the caregiver's phone rings now
        return NotifyResult(
            sent=True,
            alert_id=alert.id,
            message=(
                f"I've sent an urgent alert to {p.caregiver_name}. If you're hurt, can't breathe, or have chest pain, "
                f"please call {p.emergency_number} right away. Stay where you are and stay calm."
            ),
        )

    # ---------- scams ----------

    def trusted_contacts(self) -> list[TrustedContact]:
        """The saved contacts, plus the caregiver (whose number is trusted too)."""
        saved = sorted(self.store.load_all("contact", TrustedContact), key=lambda c: c.name.lower())
        p = self.profile()
        if p.caregiver_phone and not any(contacts.same_number(c.phone, p.caregiver_phone) for c in saved):
            saved.append(TrustedContact(id="caregiver", name=p.caregiver_name, relation=p.caregiver_relation,
                                        phone=p.caregiver_phone))
        return saved

    def find_contact(self, query: str) -> ContactResult:
        c = contacts.find(query, self.trusted_contacts())
        if c is None:
            names = ", ".join(x.name for x in self.trusted_contacts()) or "none yet"
            return ContactResult(found=False, message=f"I don't have that number saved. Your trusted contacts are: {names}.")
        return ContactResult(found=True, contact=c, message=f"{c.name}'s number is {contacts.spoken(c.phone)}.")

    async def check_scam(self, description: str, caller_number: str = "") -> ScamAssessment:
        result = await scam.assess(description)
        verdict, who, flags = contacts.check(description, self.trusted_contacts(), caller_number)
        result.number_check, result.trusted_contact = verdict, who
        result.red_flags += flags
        if who:
            number = contacts.spoken(who.phone)
            if verdict == "impersonation":
                result.risk = "high"
                result.advice = (f"Please hang up. That call didn't come from {who.name}'s number. If you want to check, "
                                 f"call {who.name} yourself on {number}. Don't send money or share any details.")
            elif verdict == "matches":
                result.advice += (f" The number is {who.name}'s, but callers can fake that. If they ask for money or "
                                  f"passwords, hang up and call {who.name} back on {number}.")
            elif verdict == "claims":
                result.advice += f" If you're not sure, hang up and call {who.name} on the number you trust: {number}."
        elif verdict == "unknown" and result.risk != "low":
            result.advice += " That number isn't one of your trusted contacts."
        gave_word = re.search(r"safe ?word|code ?word|secret word|password", description, re.IGNORECASE)
        if self.safe_word() and not gave_word and (CLAIMS_FAMILY.search(description) or verdict in ("claims", "impersonation")):
            result.advice += (" Ask them for your family's safe word. Don't say it first or give a hint. If they "
                              "can't tell you, hang up: it isn't really them.")
            result.red_flags.append("Claims to be family or a friend: ask for the family safe word")
        if result.risk in ("medium", "high"):  # anything suspicious reaches the caregiver
            p = self.profile()
            high = result.risk == "high"
            await self.raise_alert(
                "urgent" if high else "warning", "scam",
                f"{'Likely scam' if high else 'Suspicious contact'} targeting {p.preferred_name}",
                f"{description}\nRed flags: {'; '.join(result.red_flags) or 'none named'}",
            )
            result.family_alerted = True
            result.advice += f" I've also let {p.caregiver_name} know so they can check in with you."
        return result

    # ---------- family safe word ----------

    def safe_word(self) -> str:
        """The family's secret word for checking that a caller really is family. Kept out of the profile,
        so it never reaches the voice model, the MCP resources or the Echo."""
        return str((self.store.get("secret", "safe_word") or {}).get("word") or "")

    @staticmethod
    def check_safe_word_setting(word: str) -> str:
        word = " ".join(word.split())
        if word and not (3 <= len(word) <= 40):
            raise SettingsError("The safe word should be 3 to 40 letters, an easy word for the family to remember.")
        return word

    def set_safe_word(self, word: str) -> None:
        if word := self.check_safe_word_setting(word):
            self.store.put("secret", "safe_word", {"word": word, "set_at": now().isoformat()})
        else:
            self.store.delete("secret", "safe_word")

    async def check_safe_word(self, heard: str) -> SafeWordResult:
        """Whether a caller said the family's safe word. Never says the word itself."""
        word, p = self.safe_word(), self.profile()
        if not word:
            return SafeWordResult(set=False, message=(
                f"Your family hasn't chosen a safe word yet. Hang up and call {p.caregiver_name} on the number you "
                "already have. If they're really in trouble, they'll understand."))
        said = re.sub(r"[^\w\s]", "", heard.lower()).split()
        target = re.sub(r"[^\w\s]", "", word.lower()).split()
        joined, want = " ".join(said), " ".join(target)
        if not said:
            return SafeWordResult(set=True, message="What word did the caller say?")
        close = difflib.SequenceMatcher(None, joined, want).ratio() >= 0.85 or (
            len(want) >= 4 and want in joined and len(joined) <= len(want) + 12)
        if close:
            return SafeWordResult(set=True, matches=True, message=(
                "Yes, that's your family's safe word. Even so, if they ask for money, check with "
                f"{p.caregiver_name} before sending anything."))
        await self.raise_alert("urgent", "scam", f"Caller failed the safe-word check with {p.preferred_name}",
                               f"Someone claiming to be family or a friend gave the wrong safe word (“{heard[:80]}”).")
        return SafeWordResult(set=True, matches=False, family_alerted=True, message=(
            f"No, that's not your family's word. Please hang up now: it's very likely a scam. Don't send money or share "
            f"any details. I've let {p.caregiver_name} know."))

    # ---------- wellbeing ----------

    async def check_in(self, mood: str, notes: str = "") -> CheckInResult:
        text = f"{mood} {notes}".lower()
        serious = sorted({v for k, v in SERIOUS.items() if k in text})
        mild = sorted({v for k, v in MILD.items() if re.search(rf"\b{re.escape(k)}\b", text)} - set(serious))
        concerns = serious + mild
        self.store.save("checkin", CheckIn(at=now().isoformat(), mood=mood, notes=notes, concerns=concerns))
        p = self.profile()
        alerted = False
        if serious or mood == "unwell":
            await self.raise_alert(
                "urgent" if serious else "warning", "checkin",
                f"{p.preferred_name} isn't feeling well", f"Mood: {mood}. {notes}".strip(),
            )
            alerted = True
        elif mood == "low":
            await self.raise_alert("info", "checkin", f"{p.preferred_name} is feeling low today", notes)

        if serious:
            msg = f"I'm sorry to hear that. I've told {p.caregiver_name}. If it gets worse or you feel unsafe, please call {p.emergency_number}."
        elif mood in ("great", "good"):
            msg = "That's wonderful to hear! I've noted it."
        elif mood == "unwell":
            msg = f"I'm sorry you're not feeling well. I've let {p.caregiver_name} know. Would you like me to remind you to rest and drink some water?"
        else:
            msg = "Thanks for telling me. I've noted it. Would you like me to call someone or play something cheerful?"
        return CheckInResult(recorded=True, concerns=concerns, caregiver_alerted=alerted, message=msg)

    # ---------- proactive checks (scheduler) ----------

    async def run_checks(self) -> list[Alert]:
        """Missed doses, inactivity and low stock. Safe to run often: every alert is de-duplicated."""
        current = now()
        date = current.date().isoformat()
        p = self.profile()
        seen = {a.dedupe_key for a in self.alerts()}
        raised: list[Alert] = []

        async def alert(*args, dedupe_key: str, **kwargs) -> None:
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                raised.append(await self.raise_alert(*args, dedupe_key=dedupe_key, **kwargs))

        logs = self._logs_for(date)
        for m in self.medications():
            for t in m.times:
                if ((m.id, t) not in logs and current > _at(current, t) + timedelta(minutes=settings.missed_dose_grace_min)
                        and self._expected(m, _at(current, t), None)):
                    await alert(
                        "warning", "missed_dose", f"{p.preferred_name} missed the {spoken_time(t)} {m.name}",
                        f"{m.name} {m.dose} was due at {spoken_time(t)} and hasn't been logged.",
                        dedupe_key=f"missed:{date}:{m.id}:{t}",
                    )
            open_refill = any(r.medication_id == m.id and r.status in OPEN_REFILL for r in self.refills())
            if m.pills_remaining <= settings.low_stock_threshold and not open_refill:
                await alert(
                    "info", "low_stock", f"{m.name} is running low",
                    f"{m.pills_remaining} pills left. Remind {p.preferred_name} to order a refill.",
                    dedupe_key=f"low:{date}:{m.id}",
                )

        if p.last_activity and AWAKE[0] <= current.strftime("%H:%M") <= AWAKE[1]:
            idle = current - datetime.fromisoformat(p.last_activity)
            if idle >= timedelta(hours=settings.inactivity_hours):
                hours = int(idle.total_seconds() // 3600)
                await alert(
                    "warning", "inactivity", f"No activity from {p.preferred_name} in {hours} hours",
                    f"Last interaction at {_spoken_dt(p.last_activity)}. Consider giving {p.preferred_name} a call.",
                    dedupe_key=f"inactivity:{p.last_activity}",  # once per quiet spell
                )

        if p.checkin_time:
            asked = _at(current, p.checkin_time)
            heard = p.last_activity and datetime.fromisoformat(p.last_activity) >= asked - timedelta(hours=4)
            if current >= asked + timedelta(minutes=settings.checkin_grace_min) and not heard:
                last = f"since {_spoken_dt(p.last_activity)} {'today' if p.last_activity[:10] == date else 'yesterday' if p.last_activity[:10] == (current - timedelta(days=1)).date().isoformat() else 'some days ago'}" if p.last_activity else "yet"
                await alert(
                    "warning", "no_checkin", f"No check-in from {p.preferred_name} today",
                    f"Kinvesper asked {p.preferred_name} at {spoken_time(p.checkin_time)} to say they're okay and hasn't "
                    f"heard from them {last}. Please give {p.preferred_name} a call.",
                    dedupe_key=f"nocheckin:{date}", sms=True,
                )

        await self._call_unanswered(current)
        await self._escalate(current)
        await self._due_reminders(current)
        if trend := await self.check_trends():
            raised.append(trend)
        week = current.isocalendar()
        if (DAYS[current.weekday()] == settings.report_day and current.strftime("%H:%M") >= settings.report_time
                and f"report:{week.year}-W{week.week}" not in seen):
            raised.append(await self.send_weekly_report())
        return raised

    async def _escalate(self, current: datetime) -> None:
        """Urgent alerts nobody acknowledged in time go to the backup contact, once."""
        p = self.profile()
        if not (p.backup_phone or p.backup_email or p.backup_telegram):
            return
        cutoff = (current - timedelta(minutes=settings.escalate_after_min)).isoformat()
        for a in self.alerts(include_acknowledged=False):
            if a.level != "urgent" or a.escalated_at or a.created_at > cutoff:
                continue
            minutes = int((current - datetime.fromisoformat(a.created_at)).total_seconds() // 60)
            await notify.send(
                f"Kinvesper: please check on {p.preferred_name}",
                f"{p.backup_name}, this urgent alert about {p.patient_name} hasn't been answered by "
                f"{p.caregiver_name} for {minutes} minutes:\n\n{a.title}\n{a.detail}\n\n"
                f"Please check on {p.preferred_name} or reach {p.caregiver_name}.",
                phone=p.backup_phone, audience="backup")
            await self._telegram_alert(a, p.backup_telegram, f"Please check on {p.preferred_name}",
                                       f"{p.caregiver_name} hasn't answered this urgent alert for {minutes} minutes:\n"
                                       f"{a.title}\n{a.detail}")
            await notify.call(p.backup_phone, self._call_text(a, p, f"{p.caregiver_name} hasn't answered it."), self._ack_url(a))
            a.escalated_at = current.isoformat()
            self.store.save("alert", a)

    @staticmethod
    def _call_text(a: Alert, p: Profile, extra: str = "") -> str:
        detail = a.detail.split("\n")[0].strip()
        return (f"This is Kinvesper, with an urgent alert about {p.patient_name}. {a.title.rstrip('.')}. "
                f"{detail.rstrip('.') + '. ' if detail else ''}{extra + ' ' if extra else ''}Please check on {p.preferred_name} now.")

    @staticmethod
    def _ack_url(a: Alert) -> str:
        """Where "press 1" on the call goes (needs the server's public address)."""
        return f"{settings.public_url.rstrip('/')}/api/calls/{a.id}/ack" if settings.public_url.startswith("https://") else ""

    async def _ring(self, a: Alert) -> None:
        """Call the caregiver about an urgent alert, once."""
        p = self.profile()
        if a.called_at or not p.caregiver_phone:
            return
        if await notify.call(p.caregiver_phone, self._call_text(a, p), self._ack_url(a)):
            a.called_at = now().isoformat()
            self.store.save("alert", a)

    async def _call_unanswered(self, current: datetime) -> None:
        """A text is easy to miss: urgent alerts still open after CALL_AFTER_MIN minutes ring the caregiver's phone."""
        cutoff = (current - timedelta(minutes=settings.call_after_min)).isoformat()
        for a in self.alerts(include_acknowledged=False):
            if a.level == "urgent" and not a.called_at and a.created_at <= cutoff:
                await self._ring(a)

    async def send_weekly_report(self) -> Alert:
        """Email the caregiver this week's report (adherence, missed doses, moods, suspicious calls)."""
        current, p = now(), self.profile()
        s = await self.summary(7)
        trends = self.wellbeing_trends()
        week = current.isocalendar()
        start = (current - timedelta(days=6)).strftime("%b %-d")
        scams = sum(a.kind == "scam" for a in self.alerts() if a.created_at >= (current - timedelta(days=7)).isoformat())
        missed = "\n".join(f"  - {m}" for m in s.missed[:6]) + (f"\n  - and {len(s.missed) - 6} more" if len(s.missed) > 6 else "")
        body = (
            f"Hi {p.caregiver_name},\n\n{s.narrative}\n\n"
            f"This week ({start} to {current.strftime('%b %-d')})\n"
            f"- Doses taken: {s.doses_taken} of {s.doses_scheduled} ({round(s.adherence_pct)}%)\n"
            f"- Missed doses: {len(s.missed)}{chr(10) + missed if s.missed else ''}\n"
            f"- Moods at check-in: {', '.join(s.recent_moods) or 'no check-ins'}\n"
            f"- Suspicious calls or messages: {scams}\n"
            f"- Alerts still open: {s.open_alerts}\n"
            f"- Wellbeing trend: {trends['summary']}{''.join(chr(10) + '  - ' + x['text'] for x in trends['signals'])}\n\n"
            f"Open the Kinvesper dashboard for details.\n— Kinvesper"
        )
        alert = Alert(created_at=current.isoformat(), level="info", kind="report",
                      title=f"Weekly report for {p.caregiver_name}", detail=s.narrative,
                      dedupe_key=f"report:{week.year}-W{week.week}")
        sent = await notify.send(f"Kinvesper weekly report: {p.preferred_name}", body)
        alert.notified = await telegram.send(p.caregiver_telegram, f"📊 <b>Weekly report: {escape(p.preferred_name)}</b>\n\n"
                                             + escape(body.split("\n", 2)[-1].rsplit("\nOpen the Kinvesper", 1)[0])) or sent
        alert.acknowledged = True  # informational: nothing to act on
        self.store.save("alert", alert)
        return alert

    # ---------- Telegram ----------

    async def telegram_link(self, audience: str) -> dict:
        """A one-time "Connect Telegram" link for the caregiver or the backup contact."""
        if audience not in ("caregiver", "backup"):
            raise SettingsError("Connect Telegram for the caregiver or the backup contact.")
        if not telegram.enabled():
            raise SettingsError("Telegram isn't set up on this server yet (see telegram/README.md).")
        if elsewhere := await telegram.delivered_elsewhere():
            raise SettingsError(f"The bot's chats go to {elsewhere}, so pressing Start there wouldn't reach this "
                                f"server. Connect from {elsewhere}/dashboard/settings, or run make telegram-local "
                                "to use the bot with this one.")
        current = now()
        for old in self.store.load_all("tglink", TelegramLink):  # expired or replaced codes
            if old.audience == audience or old.created_at < (current - LINK_LIFETIME).isoformat():
                self.store.delete("tglink", old.id)
        code = secrets.token_urlsafe(12)
        self.store.save("tglink", TelegramLink(id=code, audience=audience, created_at=current.isoformat()), id=code)
        if not (url := await telegram.link_url(code)):
            raise SettingsError("Couldn't reach Telegram just now. Please try again.")
        return {"url": url, "bot": await telegram.bot_username(), "minutes": int(LINK_LIFETIME.total_seconds() // 60)}

    async def telegram_unlink(self, audience: str) -> dict:
        if audience not in ("caregiver", "backup"):
            raise SettingsError("Disconnect Telegram for the caregiver or the backup contact.")
        p = self.profile()
        chat = getattr(p, f"{audience}_telegram", "")
        self.store.save("profile", p.model_copy(update={f"{audience}_telegram": "", f"{audience}_telegram_name": ""}), id="main")
        if chat:
            await telegram.send(chat, "You've been disconnected from Kinvesper alerts. Connect again any time from Settings.")
        return await self.settings_view()

    def _telegram_who(self, chat_id: str) -> tuple[str, str] | None:
        """(audience, name) for a linked chat."""
        p = self.profile()
        if chat_id and chat_id == p.caregiver_telegram:
            return "caregiver", p.caregiver_name
        if chat_id and chat_id == p.backup_telegram:
            return "backup", p.backup_name or "family"
        return None

    async def telegram_update(self, update: dict) -> None:
        """One update from the bot: a /start link code, a button press, or a message from a linked chat."""
        if query := update.get("callback_query"):
            return await self._telegram_button(query)
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return
        chat_id, text = str(chat.get("id", "")), (msg.get("text") or "").strip()
        if text.startswith("/start"):
            return await self._telegram_start(chat_id, text.partition(" ")[2].strip(), telegram.name_of(msg.get("from") or {}))
        who = self._telegram_who(chat_id)
        if not who:
            await telegram.send(chat_id, "This chat isn't connected to Kinvesper yet. In the Kinvesper dashboard, open "
                                         "<b>Settings</b> and press <b>Connect Telegram</b>.")
            return
        if text.split("@")[0].lower() == "/stop":
            await self.telegram_unlink(who[0])
            return
        if msg.get("voice") or msg.get("audio") or msg.get("photo"):
            answer = await self._telegram_media(chat_id, who[1], msg)
            await telegram.send(chat_id, answer, telegram.buttons([("📋 Status", "status")]))
            return
        if not text:
            await telegram.send(chat_id, "I can pass on typed messages, voice messages and photos.")
            return
        answer = await self.contact_reply(who[1], text, chat=chat_id, message_id=msg.get("message_id"))
        await telegram.send(chat_id, escape(answer), telegram.buttons([("📋 Status", "status")]))

    async def _telegram_start(self, chat_id: str, code: str, tg_name: str) -> None:
        p, current = self.profile(), now()
        link = self.store.load("tglink", code, TelegramLink) if code else None
        if link and link.created_at >= (current - LINK_LIFETIME).isoformat():
            self.store.delete("tglink", link.id)
            p = p.model_copy(update={f"{link.audience}_telegram": chat_id, f"{link.audience}_telegram_name": tg_name})
            self.store.save("profile", p, id="main")
            when_ = ("" if link.audience == "caregiver"
                     else f" when {escape(p.caregiver_name)} hasn't answered an urgent one")
            await telegram.send(chat_id, (
                f"✅ <b>Connected, {escape(tg_name)}.</b>\nKinvesper will send alerts about {escape(p.patient_name)} "
                f"here{when_}.\n\nOn an alert, tap <b>✅ I'm on it</b> to mark it handled. Tap <b>📋 Status</b> for "
                f"today, or type a message and Kinvesper will read it to {escape(p.preferred_name)} on the Echo."),
                telegram.buttons([("📋 Status", "status")]))
            return
        if self._telegram_who(chat_id):
            await telegram.send(chat_id, "You're already connected. Type a message for "
                                         f"{escape(p.preferred_name)}, or tap Status.", telegram.buttons([("📋 Status", "status")]))
        elif code:
            await telegram.send(chat_id, "That link has expired or was already used. In Kinvesper's <b>Settings</b>, "
                                         "press <b>Connect Telegram</b> for a new one.")
        else:
            await telegram.send(chat_id, "Hello! This is <b>Kinvesper</b>, the family side of an Alexa care companion. "
                                         "To get alerts here, open the Kinvesper dashboard, go to <b>Settings</b> and "
                                         "press <b>Connect Telegram</b>.")

    async def _telegram_button(self, query: dict) -> None:
        msg = query.get("message") or {}
        chat_id, data = str((msg.get("chat") or {}).get("id", "")), query.get("data", "")
        who = self._telegram_who(chat_id)
        if not who:
            await telegram.call("answerCallbackQuery", callback_query_id=query["id"], text="This chat isn't connected to Kinvesper.")
            return
        if data.startswith("ack:"):
            alert = self.store.load("alert", data[4:], Alert)
            already = bool(alert and alert.acknowledged)
            if alert and not already:
                self.acknowledge(alert.id)
            note = "Already handled" if already else "Marked as handled" if alert else "That alert is gone"
            await telegram.call("answerCallbackQuery", callback_query_id=query["id"], text=note)
            if alert:
                await telegram.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg.get("message_id"),
                                    reply_markup=telegram.buttons([(f"✅ Handled ({who[1]})" if not already else "✅ Handled", "noop"),
                                                                   ("📋 Status", "status")]))
            return
        await telegram.call("answerCallbackQuery", callback_query_id=query["id"])
        if data == "status":
            await telegram.send(chat_id, escape(self.status_text()), telegram.buttons([("📋 Status", "status")]))

    def status_text(self) -> str:
        p, day = self.profile(), self.schedule()
        taken = sum(i.status == "taken" for i in day.items)
        missed = [i.medication for i in day.items if i.status == "missed"]
        open_alerts = [a for a in self.alerts(include_acknowledged=False) if a.level != "info"]
        checkin = (self.checkins(1) or [None])[-1]
        lines = [f"{p.preferred_name} today: {taken} of {len(day.items)} doses taken"
                 + (f", missed {', '.join(missed)}." if missed else ".")]
        if checkin:
            lines.append(f"Last check-in: feeling {checkin.mood}.")
        lines.append(f"Open alerts: {'; '.join(a.title for a in open_alerts[:3])}." if open_alerts else "No open alerts.")
        return "\n".join(lines)

    async def contact_reply(self, who: str, text: str, chat: str = "", message_id: int | None = None) -> str:
        """A message from the caregiver or the backup contact. OK acknowledges the newest open alert,
        STATUS sums up today, HELP explains, anything else is read out on the Echo."""
        p, said = self.profile(), " ".join(text.split())
        word = said.lower().split("@")[0].lstrip("/").strip(" .!")
        if word in ACK_WORDS:
            open_alerts = [a for a in self.alerts(include_acknowledged=False) if a.level != "info"]
            if not open_alerts:
                return "Thanks. There are no open alerts right now."
            a, rest = open_alerts[0], len(open_alerts) - 1
            self.acknowledge(a.id)
            return (f"Thanks {who}. Marked as handled: {a.title}."
                    + (f" {rest} more {'is' if rest == 1 else 'are'} still open in the dashboard." if rest else ""))
        if word in STATUS_WORDS:
            return self.status_text()
        if word == "help":
            return (f"Tap ✅ I'm on it on an alert (or send OK) to mark it handled. Send STATUS for today. "
                    f"Anything else you type is read to {p.preferred_name} on the Echo. Send /stop to stop alerts here.")
        if not chat:
            self.send_family_message(said, sender=who)
            return f"Got it. Kinvesper will read your message to {p.preferred_name} the next time they talk to Alexa."
        question, say = await self._relay_wording(said, p.preferred_name)
        msg = self.send_family_message(say, sender=who)
        if question:
            msg = msg.model_copy(update={"question": True, "asked": said, "telegram_chat": chat,
                                         "telegram_message_id": message_id})
            self.store.save("message", msg)
            return (f"Got it. Kinvesper will ask {p.preferred_name} “{say}” the next time they talk to Alexa, "
                    "and send you the answer here.")
        if say != said:
            msg = msg.model_copy(update={"asked": said})
            self.store.save("message", msg)
        return f"Got it. Kinvesper will read your message to {p.preferred_name} the next time they talk to Alexa."

    async def _telegram_media(self, chat_id: str, who: str, msg: dict) -> str:
        """A voice note or photo from a linked chat, for the Echo: the voice note is played as it is, a photo is
        shown on an Echo Show and described aloud. Either one can ask her something, and the answer comes back."""
        p, name = self.profile(), escape(self.profile().preferred_name)
        reply_to = msg.get("message_id")
        if voice := (msg.get("voice") or msg.get("audio")):
            if int(voice.get("duration") or 0) > MAX_VOICE_S:
                return "That voice message is a bit long. Please keep it under 3 minutes."
            audio, transcript = await telegram.download(voice["file_id"]), ""
            if audio is None:
                return "Sorry, I couldn't fetch that voice message from Telegram. Please try again."
            if speech.stt_available():
                vocabulary = speech.Vocabulary(p.preferred_name, p.caregiver_name, tuple(m.name for m in self.medications()))
                with contextlib.suppress(speech.SpeechUnavailable):
                    transcript = await speech.transcribe(audio, vocabulary)
            question = bool(transcript) and (await self._relay_wording(transcript, p.preferred_name))[0]
            self.store.save("message", FamilyMessage(
                sender=who, text=transcript or "A voice message", created_at=now().isoformat(), media="voice",
                file_id=voice["file_id"], duration=int(voice.get("duration") or 0), question=question,
                asked=transcript if question else "", telegram_chat=chat_id if question else "",
                telegram_message_id=reply_to if question else None))
            return (f"🎙 Got it. Kinvesper will play your voice message to {name} the next time they talk to Alexa"
                    + (", and send you the answer here." if question else "."))
        sizes = sorted(msg.get("photo") or [], key=lambda s: s.get("width", 0))
        photo = next((s for s in reversed(sizes) if s.get("width", 0) <= 1280), sizes[-1] if sizes else None)
        if not photo:
            return "Sorry, I couldn't read that photo."
        caption = " ".join((msg.get("caption") or "").split())
        image = await telegram.download(photo["file_id"])
        described = await self._describe_photo(image, p.preferred_name) if image else ""
        question, say = await self._relay_wording(caption, p.preferred_name) if caption else (False, "")
        self.store.save("message", FamilyMessage(
            sender=who, text=" ".join(x for x in (described, say) if x) or "A photo", created_at=now().isoformat(),
            media="photo", file_id=photo["file_id"], question=question, asked=caption,
            telegram_chat=chat_id if question else "", telegram_message_id=reply_to if question else None))
        return (f"📷 Got it. Kinvesper will show your photo on {name}'s Echo Show and describe it to them the next time "
                "they talk to Alexa" + (", and send you the answer here." if question else "."))

    async def _describe_photo(self, image: bytes, name: str) -> str:
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                "data": base64.b64encode(image).decode()}},
                   {"type": "text", "text": "Describe this photo for them."}]
        out = await llm.structured(PHOTO_SYSTEM.format(name=name), content, {
            "type": "object", "additionalProperties": False, "required": ["description"],
            "properties": {"description": {"type": "string"}}}, max_tokens=300)
        return " ".join(str((out or {}).get("description") or "").split())[:400]

    async def family_media(self, message_id: str) -> tuple[bytes, str] | None:
        """A family message's photo (JPEG) or voice note (as MP3), fetched from Telegram."""
        m = self.store.load("message", message_id, FamilyMessage)
        if not m or not m.media or not m.file_id:
            return None
        if hit := media.cached(m.id):
            return hit
        data = await telegram.download(m.file_id)
        if data is None:
            return None
        try:
            out = (data, "image/jpeg") if m.media == "photo" else (await media.to_mp3(data), "audio/mpeg")
        except Exception:  # an undecodable file
            log.exception("Couldn't convert the voice message")
            return None
        media.remember(m.id, *out)
        return out

    async def send_answer_voice(self, audio: bytes) -> bool:
        """Their recorded answer (simulator) to a question from Telegram, as a voice note under the text answer."""
        cutoff = (now() - timedelta(minutes=3)).isoformat()
        m = next((m for m in self.family_messages()
                  if m.answered_at and m.answered_at >= cutoff and m.telegram_chat and not m.answer_voice), None)
        if m is None:
            return False
        try:
            ogg = await media.to_voice_note(audio)
        except Exception:  # an undecodable recording
            log.exception("Couldn't convert the recorded answer")
            return False
        if not await telegram.send_voice(m.telegram_chat, ogg, f"🎙 {escape(self.profile().preferred_name)}, in their own voice",
                                         reply_to=m.telegram_message_id):
            return False
        self.store.save("message", m.model_copy(update={"answer_voice": True}))
        return True

    async def _relay_wording(self, text: str, name: str) -> tuple[bool, str]:
        """Whether a typed message asks the person something, and the words to read out to them."""
        out = await llm.structured(RELAY_SYSTEM.format(name=name), text, RELAY_SCHEMA, max_tokens=300)
        if out and isinstance(out.get("say"), str) and out["say"].strip():
            return bool(out.get("question")), " ".join(out["say"].split())[:500]
        question = "?" in text or bool(QUESTION_START.match(text))
        say = ASK_PREFIX.sub("", text) if re.match(r"^(please\s+)?(ask|check with)\b", text, re.IGNORECASE) else text
        return question, (say[:1].upper() + say[1:]) or text

    def family_question(self, within: timedelta = ANSWER_WAIT, sender: str = "", question_id: str = "") -> FamilyMessage | None:
        """The question from Telegram that the person has heard but not answered yet: the one with that id,
        else the newest."""
        since = (now() - within).isoformat()
        waiting = [m for m in self.family_messages() if m.question and m.heard_at and m.heard_at >= since
                   and not m.answered_at and (not sender or m.sender == sender)]
        return next((m for m in waiting if m.id == question_id), None) or next(iter(waiting), None)

    async def answer_family_question(self, answer: str, question_id: str = "") -> NotifyResult:
        """The person's answer to a question the family asked from Telegram, sent back to that chat."""
        answer = " ".join(answer.split())
        if not answer:
            return NotifyResult(sent=False, message="What would you like me to tell them?")
        q = self.family_question(question_id=question_id)
        if q is None:
            return NotifyResult(sent=False, message="There's no question from the family waiting for an answer.")
        p = self.profile()
        sent = await telegram.send(q.telegram_chat, f"💬 <b>{escape(p.preferred_name)} answered</b>\n"
                                                    f"<i>You asked: {escape(q.asked or q.text)}</i>\n\n{escape(answer)}",
                                   telegram.buttons([("📋 Status", "status")]), reply_to=q.telegram_message_id)
        self.store.save("message", q.model_copy(update={"answer": answer[:500], "answered_at": now().isoformat()}))
        if not sent:  # Telegram unreachable: the caregiver still gets it the usual way
            return await self.message_caregiver(f"Answer to “{q.asked or q.text}”: {answer}")
        return NotifyResult(sent=True, message=f"Okay, I've sent your answer to {q.sender}.")

    async def _telegram_alert(self, alert: Alert, chat: str, title: str = "", detail: str = "") -> bool:
        if not chat:
            return False
        p = self.profile()
        markup = telegram.alert_buttons(alert.id) if alert.level != "info" or alert.kind == "test" else None
        return await telegram.send(chat, telegram.alert_html(alert.level, title or alert.title, detail or alert.detail, p.patient_name), markup)

    # ---------- medicine interactions ----------

    async def check_interactions(self, medicine: str, with_medicine: str = "") -> InteractionResult:
        """Voice: "can I take ibuprofen with my pills?" Checks against all their medicines (or just `with_medicine`)
        and tells the family when there's something to watch."""
        medicine = medicine.strip()
        others = [with_medicine.strip()] if with_medicine.strip() else [m.name for m in self.medications()]
        others = [o for o in others if interactions.base_name(o) != interactions.base_name(medicine)]
        result = await interactions.check([medicine], others)
        if result.interactions:
            p, worst = self.profile(), result.interactions[0]
            await self.raise_alert(
                "warning" if worst.severity == "serious" else "info", "interaction",
                f"{p.preferred_name} asked about taking {medicine}",
                "\n".join(f"{' + '.join(i.medicines)} ({i.severity}): {i.explanation}" for i in result.interactions),
                dedupe_key=f"interaction:{now().date().isoformat()}:{interactions.base_name(medicine)}", sms=False)
        return result

    async def check_new_medicines(self, names: list[str]) -> InteractionResult:
        """Settings: the medicines just added, against everything else they take. Findings go on the dashboard."""
        names = [n.strip() for n in names if n.strip()]
        new = {interactions.base_name(n) for n in names}
        others = [m.name for m in self.medications() if interactions.base_name(m.name) not in new]
        result = await interactions.check(names, others)
        for i in result.interactions:
            await self.raise_alert(
                "warning" if i.severity == "serious" else "info", "interaction",
                f"Possible interaction: {' and '.join(i.medicines)}", f"{i.explanation} Please check with the pharmacist or doctor.",
                dedupe_key=f"interaction:{'+'.join(sorted(map(interactions.base_name, i.medicines)))}", sms=False)
        return result

    # ---------- prescription photos ----------

    async def scan_prescription(self, image: bytes, media_type: str) -> ScanResult:
        """Read a photo of a prescription or medicine label into medicines for the caregiver to review (nothing is
        saved here). Raises SettingsError when it can't be read."""
        if not llm.enabled():
            raise SettingsError("Reading photos needs Claude, which isn't set up on this server.")
        if media_type not in IMAGE_TYPES:
            raise SettingsError("Send a JPEG, PNG, GIF or WebP photo.")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image).decode()}},
            {"type": "text", "text": "Read the medicines in this photo."},
        ]
        data = await llm.structured(SCAN_SYSTEM, content, SCAN_SCHEMA, max_tokens=2048)
        if data is None:
            raise SettingsError("Sorry, I couldn't read that photo. Try again with the label flat, in good light.")
        meds = []
        for m in data.get("medicines", []):
            times = sorted({t.strip().zfill(5) for t in m.get("times", []) if TIME.match(t.strip().zfill(5))})
            meds.append(ScannedMedicine(
                name=m.get("name", "").strip(), dose=m.get("dose", "").strip(), purpose=m.get("purpose", "").strip(),
                times=times, instructions=m.get("instructions", "").strip(),
                pills_remaining=m.get("pills_remaining") or None))
        meds = [m for m in meds if m.name]
        result = ScanResult(medicines=meds, notes=data.get("notes", "").strip())
        if meds:
            current = {interactions.base_name(m.name) for m in self.medications()}
            new = [m.name for m in meds if interactions.base_name(m.name) not in current]
            if new:
                result.interactions = await interactions.check(new, [m.name for m in self.medications()])
        return result

    # ---------- ask Kinvesper (caregiver) ----------

    def _care_snapshot(self, days: int = 14) -> str:
        """Everything Kinvesper knows that a caregiver might ask about, as compact text for Claude."""
        current, p = now(), self.profile()
        cutoff = (current - timedelta(days=days)).isoformat()
        meds = self.medications()
        logs = [d for d in self.store.load_all("dose", DoseLog) if d.taken_at >= cutoff]
        names = {m.id: m.name for m in self.store.load_all("med", Medication)}
        lines = [
            f"Now: {current.strftime('%A %d %B %Y, %H:%M')} ({getattr(settings.tz, 'key', 'local time')}).",
            f"Person: {p.patient_name} (called {p.preferred_name}). Caregiver: {p.caregiver_name} ({p.caregiver_relation}).",
            f"Last talked to Kinvesper: {p.last_activity or 'never'}. Daily check-in time: {p.checkin_time or 'off'}.",
            "Medicines: " + "; ".join(f"{m.name} {m.dose} at {', '.join(m.times)} for {m.purpose or '?'}"
                                      f"{', ' + m.instructions if m.instructions else ''}, {m.pills_remaining} left" for m in meds),
            "Adherence by day (taken/scheduled): " + ", ".join(f"{d.date} {d.taken}/{d.scheduled}" for d in self.adherence(days)),
            "Missed doses: " + ("; ".join(self._missed_labels(days)) or "none"),
            "Doses logged (time taken vs scheduled slot): " + "; ".join(
                f"{d.date} {names.get(d.medication_id, '?')} slot {d.slot} taken {d.taken_at[11:16]}" for d in sorted(logs, key=lambda d: d.taken_at)[-60:]),
            "Check-ins: " + ("; ".join(f"{c.at[:16]} mood {c.mood}{' - ' + c.notes if c.notes else ''}"
                                      f"{' concerns: ' + ', '.join(c.concerns) if c.concerns else ''}" for c in self.checkins(days)) or "none"),
            "Alerts: " + ("; ".join(f"{a.created_at[:16]} [{a.level}] {a.title}{' - ' + a.detail.splitlines()[0] if a.detail else ''}"
                                    f"{' (acknowledged)' if a.acknowledged else ''}" for a in self.alerts() if a.created_at >= cutoff) or "none"),
            "Upcoming reminders: " + ("; ".join(f"{r.at} {r.text}{' (important)' if r.important else ''}" for r in self.reminders()) or "none"),
            "Refills: " + ("; ".join(f"{r.created_at[:10]} {r.medication_name}: {r.status_message}" for r in self.refills()[:5]) or "none"),
            "Family messages: " + ("; ".join(f"{m.created_at[:16]} from {m.sender}: {m.text} ({'heard ' + m.heard_at[:16] if m.heard_at else 'not heard yet'})"
                                             for m in self.family_messages()[:5]) or "none"),
            "Memories shared: " + ("; ".join(f"{m.at[:10]} ({m.topic}) {m.text[:200]}" for m in self.memories()[:6]) or "none"),
            "Repeated questions today: " + (", ".join(f"{REPEAT_TOPICS.get(k, k)} x{v}" for k, v in (self.store.get("asks", current.date().isoformat()) or {}).items()) or "none"),
        ]
        return "\n".join(lines)

    async def ask(self, question: str, history: list[dict] | None = None) -> str:
        """Answer a caregiver's question ("how has Mum been this week?") from the care data."""
        if not llm.enabled():
            return "Ask Kinvesper needs Claude, which isn't set up on this server. The cards on this page have the details."
        messages = []
        for turn in (history or [])[-4:]:
            if turn.get("q") and turn.get("a"):
                messages += [{"role": "user", "content": str(turn["q"])[:500]}, {"role": "assistant", "content": str(turn["a"])[:2000]}]
        messages.append({"role": "user", "content": question[:500]})
        system = [{"type": "text", "text": ASK_SYSTEM}, {"type": "text", "text": "Care data:\n" + self._care_snapshot()}]
        try:
            response = await llm.create_message(system=system, messages=messages, max_tokens=600)
        except Exception as e:  # anthropic.APIError, network
            return f"Sorry, I couldn't answer that just now ({type(e).__name__}). Please try again."
        return llm.text_of(response) or "Sorry, I don't have an answer to that."

    # ---------- family messages (dashboard -> Echo) ----------

    def family_messages(self, unheard_only: bool = False) -> list[FamilyMessage]:
        items = sorted(self.store.load_all("message", FamilyMessage), key=lambda m: m.created_at, reverse=True)
        return [m for m in items if not m.heard_at] if unheard_only else items

    def send_family_message(self, text: str, sender: str = "") -> FamilyMessage:
        text = text.strip()
        if not text:
            raise SettingsError("Write a message first.")
        msg = FamilyMessage(sender=sender or self.profile().caregiver_name, text=text[:500], created_at=now().isoformat())
        self.store.save("message", msg)
        return msg

    def take_family_messages(self) -> MessagesResult:
        """The messages not heard yet, read out now (and marked heard)."""
        unheard = sorted(self.family_messages(unheard_only=True), key=lambda m: m.created_at)
        p = self.profile()
        if not unheard:
            last = next(iter(self.family_messages()), None)
            said = f" The last one, from {last.sender}, said: {last.text}" if last else ""
            return MessagesResult(message=f"No new messages from {p.caregiver_name}.{said}")
        stamp = now().isoformat()
        for m in unheard:
            m.heard_at = stamp
            self.store.save("message", m)
        if len(unheard) == 1:
            return MessagesResult(messages=unheard, message=_heard_one(unheard[0]))
        said = " Next: ".join(_heard_one(m, first=False) for m in unheard)
        return MessagesResult(messages=unheard, message=f"You have {len(unheard)} messages. First, {said}")

    # ---------- companionship ----------

    def memories(self) -> list[Memory]:
        return sorted(self.store.load_all("memory", Memory), key=lambda m: m.at, reverse=True)

    def conversation_starter(self) -> ConversationStarter:
        """A gentle question about their life, one they haven't answered yet."""
        told = {m.topic for m in self.memories()}
        fresh = [s for s in STARTERS if s[0] not in told] or STARTERS
        topic, question = fresh[now().timetuple().tm_yday % len(fresh)]
        name = self.profile().preferred_name
        return ConversationStarter(topic=topic, question=question,
                                   message=f"I'd love to hear about your life, {name}. {question}")

    def save_memory(self, text: str, topic: str = "") -> MemoryResult:
        text = text.strip()
        if len(text) < 15:
            return MemoryResult(saved=False, message="Tell me a little more about it.")
        memory = Memory(at=now().isoformat(), topic=topic.strip().lower()[:40], text=text[:2000])
        self.store.save("memory", memory)
        p = self.profile()
        return MemoryResult(saved=True, memory=memory,
                            message=f"What a lovely memory. I've saved it so {p.caregiver_name} can read it too.")

    # ---------- repeated questions ----------

    async def note_repeat(self, topic: str) -> None:
        """Count a question per day; when it keeps coming back, tell the family gently, once a day."""
        date = now().date().isoformat()
        counts = self.store.get("asks", date) or {}
        counts[topic] = counts.get(topic, 0) + 1
        self.store.put("asks", date, counts)
        double = topic == "double_dose"
        if counts[topic] != (2 if double else settings.repeat_threshold):
            return
        p = self.profile()
        what = REPEAT_TOPICS.get(topic, topic)
        detail = (f"Kinvesper told {p.preferred_name} not to take it again each time. You may want to check the pill box together."
                  if double else
                  f"Asking again and again can just be habit or worry, but if it's new for {p.preferred_name} it can be an early "
                  "sign of memory trouble. You might give them a call, and mention it to their doctor if it keeps happening.")
        await self.raise_alert("warning" if double else "info", "pattern",
                               f"{p.preferred_name} {what} {counts[topic]} times today", detail,
                               dedupe_key=f"pattern:{date}:{topic}", sms=double)

    # ---------- doctor visit summary ----------

    async def doctor_report(self, days: int = 30) -> dict:
        """A one-page summary to take to the doctor: medicines and how reliably they're taken, missed doses by
        time of day, mood, symptoms mentioned, falls and other alerts, and questions worth asking."""
        current, p = now(), self.profile()
        cutoff = (current - timedelta(days=days)).isoformat()
        taken = {(d.date, d.medication_id, d.slot) for d in self.store.load_all("dose", DoseLog)}
        meds, per_med, by_time = self.medications(), [], {}
        since = self._monitored_since()
        for m in meds:
            due = got = 0
            for back in range(days - 1, -1, -1):
                day = current - timedelta(days=back)
                for t in m.times:
                    if current <= _at(day, t) + timedelta(minutes=settings.missed_dose_grace_min) or not self._expected(m, _at(day, t), since):
                        continue
                    due += 1
                    hit = (day.date().isoformat(), m.id, t) in taken
                    got += hit
                    slot = by_time.setdefault(t, [0, 0])
                    slot[0] += 1
                    slot[1] += hit
            per_med.append({"name": m.name, "dose": m.dose, "times": m.times, "purpose": m.purpose,
                            "instructions": m.instructions, "taken": got, "scheduled": due,
                            "pct": round(100 * got / due) if due else None})
        checkins = self.checkins(days)
        moods = {k: sum(c.mood == k for c in checkins) for k in ("great", "good", "okay", "low", "unwell")}
        symptoms = [{"at": c.at, "concerns": c.concerns, "notes": c.notes} for c in checkins if c.concerns]
        events = [a.model_dump(include={"created_at", "level", "kind", "title", "detail"}) for a in self.alerts()
                  if a.created_at >= cutoff and a.kind in ("emergency", "checkin", "scam", "pattern", "interaction", "no_checkin", "inactivity")]
        scheduled = sum(x["scheduled"] for x in per_med)
        overall = round(100 * sum(x["taken"] for x in per_med) / scheduled) if scheduled else None
        monitored = (current.date() - datetime.fromisoformat(since).date()).days + 1 if since else 0
        report = {
            "generated_at": current.isoformat(), "days": days, "monitored_days": min(days, monitored),
            "patient": {"name": p.patient_name, "preferred_name": p.preferred_name},
            "caregiver": {"name": p.caregiver_name, "relation": p.caregiver_relation},
            "adherence_pct": overall, "medications": per_med,
            "by_time": [{"time": t, "scheduled": v[0], "taken": v[1], "pct": round(100 * v[1] / v[0]) if v[0] else None}
                        for t, v in sorted(by_time.items())],
            "moods": moods, "symptoms": symptoms, "events": events,
        }
        weak = [x for x in report["by_time"] if x["pct"] is not None and x["pct"] < 80]
        summary = (f"Over the last {min(days, monitored)} days {p.preferred_name} took {overall}% of scheduled doses"
                   + (f", with most misses at {', '.join(spoken_time(x['time']) for x in weak)}." if weak else ".")
                   + (f" Symptoms mentioned at check-ins: {', '.join(sorted({c for s in symptoms for c in s['concerns']}))}." if symptoms else ""))
        questions = ["Can the medicine times be simplified, or doses combined?"] if weak else []
        questions += [f"Is the {x['name']} dose still right?" for x in per_med if x["pct"] is not None and x["pct"] < 70][:2]
        if symptoms:
            questions.append("Could any of the symptoms mentioned be side effects of the current medicines?")
        written = await llm.structured(
            "You prepare a short summary for an older adult's doctor visit, from home monitoring data collected by "
            "Kinvesper, a voice assistant. Write `summary`: 3-5 neutral, factual sentences a doctor can scan in "
            "seconds (adherence, patterns by time of day, mood, symptoms reported, falls or emergencies). Then "
            "`questions`: 3-5 short questions the family could ask the doctor, based only on the data. No diagnosis, "
            "no advice to change medicines.",
            json.dumps(report), {"type": "object", "properties": {"summary": {"type": "string"},
                                 "questions": {"type": "array", "items": {"type": "string"}}},
                                 "required": ["summary", "questions"], "additionalProperties": False}, max_tokens=1200)
        report["summary"] = (written or {}).get("summary") or summary
        report["questions"] = (written or {}).get("questions") or questions or ["Is everything on the current list still needed?"]
        return report

    # ---------- wellbeing trends ----------

    def wellbeing_trends(self, weeks: int = 5) -> dict:
        """This week next to the weeks before: doses, mood, repeated questions and how much they talk to
        Kinvesper. Slow changes across these are easy to miss day to day. An observation for the family,
        not a diagnosis. Cached for a few minutes: the dashboard asks every 15 seconds."""
        cache = getattr(self, "_trends", None)
        if cache and cache[0] > time.monotonic():
            return cache[1]
        current, p = now(), self.profile()
        by_day = self.adherence(weeks * 7)
        since = (current - timedelta(days=weeks * 7 - 1)).date().isoformat()
        checkins = [c for c in self.store.load_all("checkin", CheckIn) if c.at[:10] >= since]
        activity = {d.get("date"): d.get("count", 0) for d in self.store.list("activity") if (d.get("date") or "") >= since}
        out = []
        for w in range(weeks):
            days = by_day[w * 7:(w + 1) * 7]
            dates = [d.date for d in days]
            scheduled, taken = sum(d.scheduled for d in days), sum(d.taken for d in days)
            moods = [MOOD_SCORE[c.mood] for c in checkins if c.at[:10] in dates]
            repeats = sum(sum(v for v in (self.store.get("asks", d) or {}).values() if isinstance(v, int)) for d in dates)
            out.append({
                "start": dates[0], "end": dates[-1],
                "adherence": round(100 * taken / scheduled) if scheduled else None,
                "mood": round(sum(moods) / len(moods), 1) if moods else None,
                "low_days": sum(m <= 2 for m in moods),
                "repeats": repeats,
                "talks": sum(activity.get(d, 0) for d in dates),
            })
        this, before = out[-1], out[:-1]

        def base(key: str) -> float | None:
            vals = [w[key] for w in before if w[key] is not None]
            return sum(vals) / len(vals) if vals else None

        name, signals = p.preferred_name, []
        if (b := base("adherence")) is not None and this["adherence"] is not None and b - this["adherence"] >= 8:
            signals.append({"key": "adherence", "text": f"Took {this['adherence']}% of doses this week, down from about {round(b)}%."})
        moods = [w["mood"] for w in out[-3:]]
        if (b := base("mood")) is not None and this["mood"] is not None and (b - this["mood"] >= 0.5 or this["low_days"] >= 2):
            falling = len(moods) == 3 and None not in moods and moods[0] > moods[1] > moods[2]
            signals.append({"key": "mood", "text": (f"Mood at check-in has dropped {'3 weeks running' if falling else 'this week'}"
                                                    f"{f', with {this['low_days']} low day' + 's' * (this['low_days'] != 1) if this['low_days'] else ''}.")})
        if (b := base("repeats")) is not None and this["repeats"] >= 3 and this["repeats"] >= 2 * b + 1:
            signals.append({"key": "repeats", "text": f"Asked the same questions {this['repeats']} times this week, usually about {round(b)}."})
        if (b := base("talks")) is not None and b >= 5 and this["talks"] <= 0.6 * b:
            signals.append({"key": "talks", "text": f"Talked to Kinvesper {this['talks']} times this week, usually about {round(b)}."})
        level = "visit" if len(signals) >= 2 else "watch" if signals else "steady"
        summary = {
            "steady": f"Steady. Nothing has changed much for {name} compared with the last few weeks.",
            "watch": f"One thing to keep an eye on for {name} this week.",
            "visit": f"A few things have changed for {name} together. Worth a visit or a call, and a word with the doctor if it continues.",
        }[level]
        result = {"weeks": out, "signals": signals, "level": level, "summary": summary,
                  "note": "Patterns from Kinvesper's data, not a diagnosis."}
        self._trends = (time.monotonic() + TRENDS_CACHE_S, result)
        return result

    async def check_trends(self) -> Alert | None:
        """Once a week, tell the family when several signals change together (a gentle, early heads-up)."""
        trends = self.wellbeing_trends()
        if trends["level"] != "visit":
            return None
        week = now().isocalendar()
        key = f"trend:{week.year}-W{week.week}"
        if any(a.dedupe_key == key for a in self.alerts()):
            return None
        p = self.profile()
        return await self.raise_alert("warning", "pattern", f"{p.preferred_name}: worth a visit this week",
                                      " ".join(s["text"] for s in trends["signals"]) + " " + trends["note"],
                                      dedupe_key=key)

    # ---------- impact ----------

    def impact(self, days: int = 30) -> dict:
        """What Kinvesper did for the family lately, in numbers."""
        current = now()
        cutoff = (current - timedelta(days=days)).isoformat()
        alerts = [a for a in self.alerts() if a.created_at >= cutoff]
        by_day = self.adherence(days)
        scheduled, taken = sum(d.scheduled for d in by_day), sum(d.taken for d in by_day)
        on_time, dates = 0, {d.date for d in by_day}
        for d in self.store.load_all("dose", DoseLog):
            if d.date in dates:
                with contextlib.suppress(ValueError):
                    on_time += abs(datetime.fromisoformat(d.taken_at) - _at(datetime.fromisoformat(d.taken_at), d.slot)) <= timedelta(hours=1)
        on_time = min(on_time, taken)
        urgent = [a for a in alerts if a.level == "urgent" and a.kind != "test"]
        waits = sorted((datetime.fromisoformat(a.acknowledged_at) - datetime.fromisoformat(a.created_at)).total_seconds() / 60
                       for a in urgent if a.acknowledged_at)
        messages = [m for m in self.family_messages() if m.created_at >= cutoff]
        since = cutoff[:10]
        return {
            "days": days,
            "scams_caught": sum(a.kind == "scam" for a in alerts),
            "doses_taken": taken, "doses_scheduled": scheduled,
            "adherence": round(100 * taken / scheduled) if scheduled else None,
            "on_time": on_time,
            "conversations": sum(d.get("count", 0) for d in self.store.list("activity") if (d.get("date") or "") >= since),
            "urgent_alerts": len(urgent),
            "response_min": round(waits[len(waits) // 2]) if waits else None,
            "family_messages": sum(bool(m.heard_at) for m in messages),
            "questions_answered": sum(bool(m.answered_at) for m in messages),
            "memories": sum(m.at >= cutoff for m in self.memories()),
            "refills": sum(r.created_at >= cutoff for r in self.refills()),
        }

    # ---------- dashboard ----------

    def checkins(self, days: int = 14) -> list[CheckIn]:
        cutoff = (now() - timedelta(days=days)).isoformat()
        return sorted((c for c in self.store.load_all("checkin", CheckIn) if c.at >= cutoff), key=lambda c: c.at)

    def dashboard(self) -> dict:
        p = self.profile()
        by_day = self.adherence(7)
        scheduled = sum(d.scheduled for d in by_day)
        taken = sum(d.taken for d in by_day)
        today = self.schedule()
        return {
            "generated_at": now().isoformat(),
            "profile": p.model_dump(),
            "adherence": {
                "pct": round(100 * taken / scheduled, 1) if scheduled else None,
                "taken": taken, "scheduled": scheduled,
                "by_day": [d.model_dump() for d in by_day],
            },
            "today": today.model_dump(),
            "alerts": [a.model_dump() for a in self.alerts()[:30]],
            "open_alerts": sum(not a.acknowledged for a in self.alerts()),
            "checkins": [c.model_dump() for c in self.checkins(14)],
            "medications": [
                {**m.model_dump(), "low_stock": m.pills_remaining <= settings.low_stock_threshold}
                for m in self.medications()
            ],
            "refills": [r.model_dump() for r in self.refills()[:5]],
            "reminders": [r.model_dump() for r in self.reminders()[:10]],
            "latest_report": next((a.model_dump() for a in self.alerts() if a.kind == "report"), None),
            "alerts_reach": {"phone": bool(p.caregiver_phone), "email": bool(p.caregiver_email),
                             "backup": bool(p.backup_phone or p.backup_email or p.backup_telegram),
                             "telegram": bool(p.caregiver_telegram), "calls": notify.calls_enabled() and bool(p.caregiver_phone)},
            "family_messages": [{**m.model_dump(exclude={"file_id", "telegram_chat"}),
                                 "media_url": media.url(m.id) if m.media else ""} for m in self.family_messages()[:5]],
            "memories": [m.model_dump() for m in self.memories()[:6]],
            "ask_enabled": llm.enabled(),
            "trends": self.wellbeing_trends(),
            "impact": self.impact(),
        }

    # ---------- summaries ----------

    def adherence(self, days: int = 7) -> list[DayAdherence]:
        current = now()
        logs = self.store.load_all("dose", DoseLog)
        taken_keys = {(d.date, d.medication_id, d.slot) for d in logs}
        meds = self.medications()
        since = self._monitored_since()
        out = []
        for back in range(days - 1, -1, -1):
            day = current - timedelta(days=back)
            date = day.date().isoformat()
            slots = [(m.id, t) for m in meds for t in m.times if self._expected(m, _at(day, t), since)]
            if back == 0:  # today: only doses whose grace period has passed, or already taken
                slots = [
                    (mid, t) for mid, t in slots
                    if (date, mid, t) in taken_keys or current > _at(day, t) + timedelta(minutes=settings.missed_dose_grace_min)
                ]
            out.append(DayAdherence(date=date, scheduled=len(slots), taken=sum((date, mid, t) in taken_keys for mid, t in slots)))
        return out

    async def summary(self, days: int = 7, narrate: bool = True) -> CareSummary:
        by_day = self.adherence(days)
        scheduled = sum(d.scheduled for d in by_day)
        taken = sum(d.taken for d in by_day)
        pct = round(100 * taken / scheduled, 1) if scheduled else 100.0
        cutoff = (now() - timedelta(days=days)).isoformat()
        checkins = sorted((c for c in self.store.load_all("checkin", CheckIn) if c.at >= cutoff), key=lambda c: c.at)
        alerts = [a for a in self.alerts() if a.created_at >= cutoff]
        missed = self._missed_labels(days)
        p = self.profile()

        narrative = (
            f"Over the last {days} days {p.preferred_name} took {taken} of {scheduled} scheduled doses ({pct}%). "
            f"{len(missed)} doses were missed. {sum(not a.acknowledged for a in alerts)} alerts need attention."
        )
        if narrate:
            text = await llm.complete(
                system=(
                    "You write a short weekly care update for a family caregiver about their elderly parent. "
                    "Warm, factual, 4-6 sentences, plain text, no markdown. Call out patterns (e.g. evening doses "
                    "missed), wellbeing trends, and one concrete suggestion. Do not invent facts."
                ),
                prompt=(
                    f"Patient: {p.patient_name}. Caregiver: {p.caregiver_name}.\n"
                    f"Adherence by day: {[d.model_dump() for d in by_day]}\nMissed doses: {missed}\n"
                    f"Check-ins: {[c.model_dump(include={'at', 'mood', 'notes', 'concerns'}) for c in checkins]}\n"
                    f"Alerts: {[a.model_dump(include={'created_at', 'level', 'title', 'acknowledged'}) for a in alerts]}"
                ),
            )
            narrative = text or narrative
        return CareSummary(
            days=days, adherence_pct=pct, doses_scheduled=scheduled, doses_taken=taken, by_day=by_day,
            missed=missed, open_alerts=sum(not a.acknowledged for a in alerts),
            recent_moods=[c.mood for c in checkins[-5:]], narrative=narrative,
        )

    def _missed_labels(self, days: int) -> list[str]:
        current = now()
        taken = {(d.date, d.medication_id, d.slot) for d in self.store.load_all("dose", DoseLog)}
        since = self._monitored_since()
        out = []
        for back in range(days - 1, -1, -1):
            day = current - timedelta(days=back)
            date = day.date().isoformat()
            for m in self.medications():
                for t in m.times:
                    overdue = current > _at(day, t) + timedelta(minutes=settings.missed_dose_grace_min)
                    if overdue and (date, m.id, t) not in taken and self._expected(m, _at(day, t), since):
                        out.append(f"{day.strftime('%a')} {spoken_time(t)} {m.name}")
        return out


def _heard_one(m: FamilyMessage, first: bool = True) -> str:
    """One family message as a tool result says it (the voice model can't play a voice note: it reads what it says)."""
    if m.media == "voice":
        return f"{m.sender} sent a voice message{f'. It says: {m.text}' if m.text != 'A voice message' else ''}"
    if m.media == "photo":
        return f"{m.sender} sent a photo: {m.text}"
    if m.question:
        return f"{m.sender} is asking: {m.text}" if first else f"{m.sender} asks: {m.text}"
    return f"You have a message from {m.sender}: {m.text}" if first else f"from {m.sender}: {m.text}"


def heard_one(m: FamilyMessage, locale: str, name: str) -> str:
    """What the Echo says for one family message (a voice note's own audio is played after it)."""
    key = {"voice": "family_voice", "photo": "family_photo"}.get(m.media) or ("family_question" if m.question else "family_message")
    return i18n.t(locale, key, name=name, sender=m.sender, text=m.text).strip()


def heard_messages(messages: list[FamilyMessage], locale: str, name: str) -> tuple[str, str]:
    """Family messages as the Echo reads them out, and a note telling the voice model which of them
    wait for an answer (a question asked from Telegram goes back with answer_family_question)."""
    said = " ".join(heard_one(m, locale, name) for m in messages)
    asked = [m for m in messages if m.question]
    note = " ".join(f"{m.sender} asked them “{m.text}” (question_id {m.id}) and is waiting for the answer in Telegram. "
                    "Their next words are most likely the answer: call answer_family_question with it, in their own "
                    "words, and that question_id (not notify_caregiver, and without asking to confirm)." for m in asked)
    if len(asked) > 1:
        note += " Answer each question separately, with its own question_id and only the part of what they said that answers it."
    return said, note
