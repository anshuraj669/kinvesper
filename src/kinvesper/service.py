"""CareService: all care logic. MCP tools, the dashboard API and the scheduler call into this."""

from __future__ import annotations

import difflib
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import anyio

from . import contacts, llm, notify, scam
from .config import now, settings
from .models import (
    Alert,
    AlertKind,
    AlertLevel,
    CareSummary,
    CheckIn,
    CheckInResult,
    ContactResult,
    DayAdherence,
    DoseLog,
    LogDoseResult,
    Medication,
    NotifyResult,
    Profile,
    Refill,
    RefillResult,
    ScamAssessment,
    ScheduleItem,
    TrustedContact,
    ScheduleResult,
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
                  "emergency_number")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


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
            "sms": bool(p.caregiver_phone),
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
        if not re.fullmatch(r"\d{2,6}", fields["emergency_number"]):
            raise SettingsError("The emergency number should be the short local number, like 112, 911 or 999.")
        if (fields["backup_phone"] or fields["backup_email"]) and not fields["backup_name"]:
            raise SettingsError("Add the backup contact's name.")
        if not (fields["caregiver_phone"] or fields["caregiver_email"]):
            raise SettingsError("Add a phone number or an email, so alerts reach the caregiver.")

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
        return await self.settings_view()

    async def send_test_alert(self) -> Alert:
        p = self.profile()
        return await self.raise_alert(
            "info", "test", "Test alert",
            f"This is how Kinvesper will reach you, {p.caregiver_name}, when something needs your attention.", sms=True)

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
        return existing.model_copy(update=values) if existing else Medication(**values)

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
            alert.notified = await notify.send(f"Kinvesper: {title}", f"{detail}\n— about {p.patient_name}",
                                               phone=p.caregiver_phone)
        self.store.save("alert", alert)
        return alert

    def acknowledge(self, alert_id: str) -> Alert | None:
        alert = self.store.load("alert", alert_id, Alert)
        if alert:
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
                if (m.id, t) not in logs and current > _at(current, t) + timedelta(minutes=settings.missed_dose_grace_min):
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

        await self._escalate(current)
        week = current.isocalendar()
        if (DAYS[current.weekday()] == settings.report_day and current.strftime("%H:%M") >= settings.report_time
                and f"report:{week.year}-W{week.week}" not in seen):
            raised.append(await self.send_weekly_report())
        return raised

    async def _escalate(self, current: datetime) -> None:
        """Urgent alerts nobody acknowledged in time go to the backup contact, once."""
        p = self.profile()
        if not (p.backup_phone or p.backup_email):
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
            a.escalated_at = current.isoformat()
            self.store.save("alert", a)

    async def send_weekly_report(self) -> Alert:
        """Email the caregiver this week's report (adherence, missed doses, moods, suspicious calls)."""
        current, p = now(), self.profile()
        s = await self.summary(7)
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
            f"- Alerts still open: {s.open_alerts}\n\n"
            f"Open the Kinvesper dashboard for details.\n— Kinvesper"
        )
        alert = Alert(created_at=current.isoformat(), level="info", kind="report",
                      title=f"Weekly report for {p.caregiver_name}", detail=s.narrative,
                      dedupe_key=f"report:{week.year}-W{week.week}")
        alert.notified = await notify.send(f"Kinvesper weekly report: {p.preferred_name}", body)
        alert.acknowledged = True  # informational: nothing to act on
        self.store.save("alert", alert)
        return alert

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
        }

    # ---------- summaries ----------

    def adherence(self, days: int = 7) -> list[DayAdherence]:
        current = now()
        logs = self.store.load_all("dose", DoseLog)
        taken_keys = {(d.date, d.medication_id, d.slot) for d in logs}
        meds = self.medications()
        out = []
        for back in range(days - 1, -1, -1):
            day = current - timedelta(days=back)
            date = day.date().isoformat()
            slots = [(m.id, t) for m in meds for t in m.times]
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
        out = []
        for back in range(days - 1, -1, -1):
            day = current - timedelta(days=back)
            date = day.date().isoformat()
            for m in self.medications():
                for t in m.times:
                    overdue = current > _at(day, t) + timedelta(minutes=settings.missed_dose_grace_min)
                    if overdue and (date, m.id, t) not in taken:
                        out.append(f"{day.strftime('%a')} {spoken_time(t)} {m.name}")
        return out
