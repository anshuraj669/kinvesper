"""Demo household: Margaret (78, lives alone) and her daughter Priya."""

from __future__ import annotations

import random
from datetime import timedelta

from .config import now, settings
from .models import Alert, CheckIn, DoseLog, FamilyMessage, Medication, Profile
from .service import _at
from .store import Store

MEDS = [
    Medication(id="med_lisinopril", name="Lisinopril", dose="10 mg", purpose="blood pressure heart",
               times=["08:00"], instructions="take with a full glass of water", pills_remaining=18),
    Medication(id="med_metformin", name="Metformin", dose="500 mg", purpose="diabetes blood sugar",
               times=["08:00", "20:00"], instructions="take with food", pills_remaining=41),
    Medication(id="med_atorvastatin", name="Atorvastatin", dose="20 mg", purpose="cholesterol",
               times=["20:00"], pills_remaining=4),
    Medication(id="med_levothyroxine", name="Levothyroxine", dose="50 mcg", purpose="thyroid",
               times=["06:30"], instructions="take on an empty stomach, 30 minutes before breakfast", pills_remaining=25),
]

MOODS = ["good", "good", "okay", "great", "low", "good"]
EARLIER_MOODS = ["good", "great", "great", "good", "great", "good", "great"]
HISTORY_WEEKS = 4  # before the last week: a steadier baseline for the wellbeing trends


def seed(store: Store, days_of_history: int = 6, history: bool = True) -> None:
    store.clear()
    current = now()
    store.save("profile", Profile(
        patient_name="Margaret Hill", preferred_name="Margaret", caregiver_name="Priya",
        caregiver_relation="daughter", caregiver_phone=settings.caregiver_sms, caregiver_email=settings.caregiver_email,
        pharmacy_name="Main Street Pharmacy", last_activity=current.isoformat(),
    ), id="main")
    for m in MEDS:
        store.save("med", m.model_copy())

    rng = random.Random(42)
    grace = timedelta(minutes=settings.missed_dose_grace_min)
    for back in range(days_of_history, -1, -1):
        day = current - timedelta(days=back)
        for m in MEDS:
            for t in m.times:
                slot = _at(day, t)
                if back == 0 and current <= slot + grace:
                    continue  # leave today's current/upcoming doses open for the live demo
                # evening doses are missed more often: a pattern the weekly summary should spot
                p_taken = 0.7 if t >= "20:00" else 0.95
                if rng.random() < p_taken:
                    taken = slot + timedelta(minutes=rng.randint(-10, 35))
                    store.save("dose", DoseLog(medication_id=m.id, date=day.date().isoformat(), slot=t,
                                               taken_at=taken.isoformat(), source="seed"))
        if back > 0:
            store.save("checkin", CheckIn(at=_at(day, "10:15").isoformat(), mood=MOODS[back % len(MOODS)],
                                          notes="" if back % 3 else "a bit lonely this week"))

    if history:
        _history(store, current, days_of_history)


def _history(store: Store, current, recent_days: int) -> None:
    """Four steadier weeks before the recent ones, so the wellbeing trends have a baseline to compare with: doses
    taken more reliably, better moods, fewer repeated questions and more conversations than this week. Plus a
    few past alerts and family messages for the impact numbers."""
    rng = random.Random(7)
    for back in range(recent_days + 1 + HISTORY_WEEKS * 7, recent_days, -1):
        day = current - timedelta(days=back)
        date = day.date().isoformat()
        for m in MEDS:
            for t in m.times:
                if rng.random() < 0.99:
                    taken = _at(day, t) + timedelta(minutes=rng.randint(-10, 30))
                    store.save("dose", DoseLog(medication_id=m.id, date=date, slot=t, taken_at=taken.isoformat(), source="seed"))
        store.save("checkin", CheckIn(at=_at(day, "10:10").isoformat(), mood=EARLIER_MOODS[back % len(EARLIER_MOODS)]))
        store.put("activity", date, {"date": date, "count": rng.randint(5, 9)})
        if back % 6 == 0:
            store.put("asks", date, {"schedule": 1})
    for back in range(recent_days, -1, -1):  # this week: quieter, and the same questions more often
        date = (current - timedelta(days=back)).date().isoformat()
        store.put("activity", date, {"date": date, "count": rng.randint(2, 4)})
        if back in (1, 2, 4):
            store.put("asks", date, {"schedule": 3 if back == 2 else 2})

    def past(days: float, minutes: int, **kw) -> Alert:
        at = current - timedelta(days=days)
        return Alert(created_at=at.isoformat(), notified=True, acknowledged=True,
                     acknowledged_at=(at + timedelta(minutes=minutes)).isoformat(), **kw)

    for a in (
        past(19, 4, level="urgent", kind="scam", title="Likely scam targeting Margaret",
             detail="A caller said her grandson was in jail and asked for gift cards.\nRed flags: gift cards; a relative in trouble"),
        past(11, 7, level="warning", kind="scam", title="Suspicious contact targeting Margaret",
             detail="Someone claiming to be from the bank asked for her OTP.\nRed flags: asked for a one-time code"),
        past(16, 3, level="urgent", kind="checkin", title="Margaret isn't feeling well",
             detail="Mood: unwell. Felt dizzy getting up from the couch."),
    ):
        store.save("alert", a)
    for days, text in ((9, "Happy Diwali, Mum! We'll call you in the evening."), (5, "The kids say hi! See you on Sunday.")):
        at = current - timedelta(days=days)
        store.save("message", FamilyMessage(sender="Priya", text=text, created_at=at.isoformat(),
                                            heard_at=(at + timedelta(hours=3)).isoformat()))
