"""Times for reminders: reading "at 5 PM", "in 2 hours", "tomorrow at 9" (for the offline router; Claude
passes an ISO time it works out from the [Now] line), and saying a time back ("at 5 PM today").
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .config import settings

NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
           "thirty": 30, "forty five": 45, "half an": 0.5, "half a": 0.5}
IN = re.compile(r"\bin (\d+(?:\.\d+)?|" + "|".join(sorted(NUMBERS, key=len, reverse=True)) + r") (minute|min|hour|hr)s?\b")
AT = re.compile(r"\bat (\d{1,2})(?:[:.](\d{2}))? ?(a\.?m\.?|p\.?m\.?)?(?![\w:])")
NOON = re.compile(r"\b(?:at )?(noon|midday|midnight)\b")
FILLER = re.compile(r"^(?:(?:please|can you|could you|kinvesper)\s+)*(?:remind me (?:to |about |that )?|set (?:an? )?(?:important )?"
                    r"(?:alarm|reminder) (?:for |to |about )?|wake me up (?:for )?)", re.I)


def local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=settings.tz) if dt.tzinfo is None else dt.astimezone(settings.tz)


def parse(text: str, now: datetime) -> datetime | None:
    """The time a spoken request means, in the care timezone, or None."""
    t = text.lower()
    if m := IN.search(t):
        amount = float(m[1]) if m[1][0].isdigit() else NUMBERS[m[1]]
        return now + (timedelta(hours=amount) if m[2].startswith("h") else timedelta(minutes=amount))
    day = now + timedelta(days=1) if "tomorrow" in t else now
    if m := NOON.search(t):
        hour, minute, half = (0 if m[1] == "midnight" else 12), 0, None
    elif m := AT.search(t):
        hour, minute, half = int(m[1]), int(m[2] or 0), (m[3] or "").replace(".", "")
        if hour > 23 or minute > 59:
            return None
    else:
        return None
    if half == "pm" and hour < 12:
        hour += 12
    elif half == "am" and hour == 12:
        hour = 0
    at = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if not half and hour <= 12 and ("tonight" in t or "evening" in t or (at <= now and "tomorrow" not in t)):
        at += timedelta(hours=12) if hour < 12 else timedelta(0)  # "at 5" said at 2 PM means 5 PM
    if at <= now and "tomorrow" not in t:
        at += timedelta(days=1)
    return at


def what(text: str) -> str:
    """'Remind me to call Priya at 5 PM' -> 'call Priya'."""
    rest = FILLER.sub("", text.strip())
    rest = re.sub(IN.pattern, "", rest, flags=re.I)
    rest = re.sub(AT.pattern, "", rest, flags=re.I)
    rest = re.sub(r"\b(?:at )?(?:noon|midday|midnight)\b|\b(?:today|tonight|tomorrow|this evening)\b", "", rest, flags=re.I)
    rest = re.sub(r"\s+", " ", rest).strip(" ,.?!")
    return re.sub(r"^(?:to|about|that|for)\s+", "", rest, flags=re.I) or text.strip()


def spoken(at: datetime, now: datetime) -> str:
    """'at 5 PM today', 'at 9:30 AM tomorrow', 'at 10 AM on Friday, October 16'."""
    clock = {(12, 0): "noon", (0, 0): "midnight"}.get((at.hour, at.minute)) or (
        at.strftime("%-I %p") if at.minute == 0 else at.strftime("%-I:%M %p"))
    days = (at.date() - now.date()).days
    if days == 0:
        return f"at {clock} today" if at.hour < 18 else f"at {clock} tonight"
    if days == 1:
        return f"at {clock} tomorrow"
    if days < 7:
        return f"at {clock} on {at.strftime('%A')}"
    return f"at {clock} on {at.strftime('%A, %B %-d')}"
