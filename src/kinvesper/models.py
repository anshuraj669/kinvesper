"""Domain records and tool result shapes.

Tool results are Pydantic models so FastMCP publishes an `outputSchema` for each tool
and returns `structuredContent` alongside the spoken text.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------- stored records ----------


class Profile(BaseModel):
    patient_name: str
    preferred_name: str
    caregiver_name: str
    caregiver_relation: str = "daughter"
    caregiver_phone: str = ""
    pharmacy_name: str = "Main Street Pharmacy"
    last_activity: str | None = None  # ISO timestamp of the last voice interaction


class Medication(BaseModel):
    id: str = Field(default_factory=lambda: new_id("med"))
    name: str
    dose: str
    purpose: str = ""
    times: list[str] = Field(description="Daily times in 24h HH:MM")
    instructions: str = ""
    pills_remaining: int = 30
    pills_per_dose: int = 1
    active: bool = True


class DoseLog(BaseModel):
    id: str = Field(default_factory=lambda: new_id("dose"))
    medication_id: str
    date: str  # YYYY-MM-DD (patient local)
    slot: str  # HH:MM scheduled time this dose satisfies
    taken_at: str  # ISO timestamp
    source: str = "voice"


AlertLevel = Literal["info", "warning", "urgent"]
AlertKind = Literal[
    "missed_dose", "scam", "inactivity", "emergency", "refill", "caregiver_message", "checkin", "low_stock"
]


class Alert(BaseModel):
    id: str = Field(default_factory=lambda: new_id("alert"))
    created_at: str
    level: AlertLevel
    kind: AlertKind
    title: str
    detail: str = ""
    dedupe_key: str | None = None
    notified: bool = False
    acknowledged: bool = False


RefillStatus = Literal["requested", "contacting_pharmacy", "verifying", "approved", "ready", "failed"]


class Refill(BaseModel):
    id: str = Field(default_factory=lambda: new_id("refill"))
    medication_id: str
    medication_name: str
    fulfillment: Literal["pickup", "delivery"] = "pickup"
    status: RefillStatus = "requested"
    status_message: str = "Refill requested"
    ready_by: str | None = None
    created_at: str
    updated_at: str


class CheckIn(BaseModel):
    id: str = Field(default_factory=lambda: new_id("checkin"))
    at: str
    mood: Literal["great", "good", "okay", "low", "unwell"]
    notes: str = ""
    concerns: list[str] = []


# ---------- tool results ----------

DoseStatus = Literal["taken", "due", "missed", "upcoming"]


class ScheduleItem(BaseModel):
    medication: str
    dose: str
    purpose: str
    time: str
    status: DoseStatus
    taken_at: str | None = None
    instructions: str = ""


class ScheduleResult(BaseModel):
    date: str
    items: list[ScheduleItem]
    low_stock: list[str] = Field(default_factory=list, description="Medications with few pills left")
    spoken_summary: str


class LogDoseResult(BaseModel):
    status: Literal["logged", "already_taken", "not_found", "not_due"]
    medication: str | None = None
    slot: str | None = None
    taken_at: str | None = None
    pills_remaining: int | None = None
    low_stock: bool = False
    message: str


class MedicationResult(BaseModel):
    medication: Medication
    message: str


class RefillResult(BaseModel):
    refill: Refill | None = Field(default=None, description="Empty when the medication wasn't found")
    message: str


RiskLevel = Literal["low", "medium", "high"]


class ScamAssessment(BaseModel):
    risk: RiskLevel
    red_flags: list[str]
    advice: str
    family_alerted: bool = False


class NotifyResult(BaseModel):
    sent: bool
    confirmed_by_user: bool | None = Field(default=None, description="None when the client can't ask for confirmation")
    alert_id: str | None = None
    message: str


class CheckInResult(BaseModel):
    recorded: bool
    concerns: list[str]
    caregiver_alerted: bool
    message: str


class DayAdherence(BaseModel):
    date: str
    scheduled: int
    taken: int


class CareSummary(BaseModel):
    days: int
    adherence_pct: float
    doses_scheduled: int
    doses_taken: int
    by_day: list[DayAdherence]
    missed: list[str]
    open_alerts: int
    recent_moods: list[str]
    narrative: str
