"""Demo household: Margaret (78, lives alone) and her daughter Priya."""

from __future__ import annotations

import random
from datetime import timedelta

from .config import now, settings
from .models import CheckIn, DoseLog, Medication, Profile
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


def seed(store: Store, days_of_history: int = 6) -> None:
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
