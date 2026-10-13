"""Trusted contacts: numbers the caregiver saved, to catch callers pretending to be someone they're not.

"Someone from my bank called from 98 4123 7777" -> the bank's saved number is different -> impersonation.
Caller ID can be faked, so a matching number never lowers the risk: it only tells the person whose
number it is, and to call back on it before doing anything.
"""

from __future__ import annotations

import re

from .models import TrustedContact

WORD_DIGITS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9"}
# Words that say who a contact is, so "my bank" finds the contact whose relation is "bank".
GENERIC = {"my", "the", "of", "and", "dr", "mr", "mrs", "ms", "from", "office", "company", "ltd", "inc"}
MIN_DIGITS = 7
MATCH_DIGITS = 8  # compare the last 8 digits: "+91 98412 37777" is the same phone as "098412 37777"


def digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def numbers_in(text: str) -> list[str]:
    """Phone numbers in what the person said, written or spoken ("nine eight four one ..."), with
    "double five" spelled out."""
    words = re.findall(r"[a-z]+|\d+|[^\sa-z\d]", text.lower())
    out, run, repeat = [], "", 1
    for w in words:
        if w in ("double", "triple"):
            repeat = 2 if w == "double" else 3
            continue
        d = w if w.isdigit() else WORD_DIGITS.get(w) if (w != "o" or run) else None
        if d is not None:
            run += d * repeat if len(d) == 1 else d
        elif w not in ("-", "(", ")", "+", ".", ",") and not (w == "and" and run):
            if len(run) >= MIN_DIGITS:
                out.append(run)
            run = ""
        repeat = 1
    if len(run) >= MIN_DIGITS:
        out.append(run)
    return out


def same_number(a: str, b: str) -> bool:
    a, b = digits(a), digits(b)
    if not a or not b:
        return False
    n = min(MATCH_DIGITS, len(a), len(b))
    return a[-n:] == b[-n:]


def spoken(phone: str) -> str:
    """Digits in groups, for a voice to read: '+15550102030' -> '1 5 5 5, 0 1 0, 2 0 3 0'."""
    d = digits(phone)
    groups = [d[max(0, i - 4):i] for i in range(len(d), 0, -4)][::-1] if len(d) > 4 else [d]
    if len(groups) > 1 and len(groups[0]) < 3:  # merge a short leading group: "1 5 5 5" not "1, 5 5 5"
        groups[1] = groups[0] + groups[1]
        groups = groups[1:]
    return ", ".join(" ".join(g) for g in groups)


def _names(c: TrustedContact) -> set[str]:
    words = set(re.findall(r"[a-z]+", f"{c.name} {c.relation}".lower()))
    return {w for w in words if len(w) >= 3 and w not in GENERIC}


def mentioned(text: str, contacts: list[TrustedContact]) -> list[TrustedContact]:
    """Contacts the text claims to be, by name or by who they are ("my bank", "Dr. Patel", "Priya")."""
    said = set(re.findall(r"[a-z]+", text.lower()))
    return [c for c in contacts if _names(c) & said]


def find(query: str, contacts: list[TrustedContact]) -> TrustedContact | None:
    hits = mentioned(query, contacts)
    return hits[0] if hits else None


def check(text: str, contacts: list[TrustedContact], caller_number: str = "") -> tuple[str, TrustedContact | None, list[str]]:
    """Returns (verdict, contact, red flags). Verdicts:
    impersonation  claims to be a saved contact, from a different number
    matches        the number is a saved contact's (still call back before acting)
    unknown        a number that isn't saved
    claims         names a saved contact, no number given
    none           nothing to check
    """
    numbers = [*numbers_in(caller_number), *numbers_in(text)]
    claimed = mentioned(text, contacts)
    for n in numbers:
        if owner := next((c for c in contacts if same_number(n, c.phone)), None):
            if claimed and owner not in claimed:
                c = claimed[0]
                return "impersonation", c, [f"says they're {c.name}, but the call came from {owner.name}'s number"]
            return "matches", owner, []
    if numbers and claimed:
        c = claimed[0]
        return "impersonation", c, [f"says they're {c.name}, but that isn't {c.name}'s saved number"]
    if numbers:
        return "unknown", None, ["the number isn't in your trusted contacts"]
    if claimed:
        return "claims", claimed[0], []
    return "none", None, []
