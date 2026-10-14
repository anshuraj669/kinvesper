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
    caregiver_phone: str = ""  # E.164; alerts are texted here
    caregiver_email: str = ""  # alerts are emailed here once the caregiver confirms Amazon's link
    # Backup contact: gets urgent alerts nobody acknowledged within ESCALATE_AFTER_MIN minutes.
    backup_name: str = ""
    backup_phone: str = ""
    backup_email: str = ""
    pharmacy_name: str = "Main Street Pharmacy"
    emergency_number: str = "911"  # what Kinvesper tells them to call: 112 in India and the EU, 999 in the UK
    last_activity: str | None = None  # ISO timestamp of the last voice interaction


class TrustedContact(BaseModel):
    """A number the caregiver vouches for (bank, doctor, family), to catch callers pretending to be them."""

    id: str = Field(default_factory=lambda: new_id("contact"))
    name: str = Field(description="Who it is, e.g. 'State Bank of India', 'Dr. Patel', 'Ravi'")
    relation: str = Field(default="", description="What they are to the person, e.g. 'bank', 'doctor', 'grandson'")
    phone: str


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
    "missed_dose", "scam", "inactivity", "emergency", "refill", "caregiver_message", "checkin", "low_stock", "test", "report", "reminder"
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
    escalated_at: str | None = None  # when it went to the backup contact, unacknowledged


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
    number_check: Literal["impersonation", "matches", "unknown", "claims", "none"] = Field(
        default="none", description="The caller's number against the trusted contacts")
    trusted_contact: TrustedContact | None = Field(
        default=None, description="The saved contact they claimed to be, or whose number it is")


class ContactResult(BaseModel):
    found: bool
    contact: TrustedContact | None = None
    message: str


class Reminder(BaseModel):
    """A one-off reminder ("call Priya at 5"). Important ones are alarms: an extra heads-up, a timer
    that rings until stopped when it's close, and the caregiver hears about it at the time."""

    id: str = Field(default_factory=lambda: new_id("rem"))
    text: str = Field(description="What to remind them about, e.g. 'call Priya'")
    at: str = Field(description="Local date and time, ISO 8601")
    important: bool = False
    status: Literal["scheduled", "done", "cancelled"] = "scheduled"
    created_at: str = ""
    caregiver_notified: bool = False


class ReminderResult(BaseModel):
    reminder: Reminder | None = None
    reminders: list[Reminder] = Field(default_factory=list)
    message: str


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
