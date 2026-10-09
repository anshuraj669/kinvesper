"""Elder-fraud detection: deterministic red-flag rules, optionally refined by Claude.

Rules alone must be good enough to protect the user when no LLM is configured.
"""

from __future__ import annotations

import json
import re

from . import llm
from .models import ScamAssessment

# (pattern, red flag shown to the family, weight)
RULES: list[tuple[str, str, int]] = [
    (r"gift ?cards?|itunes|google play card|steam card", "Asked for payment in gift cards", 3),
    (r"wire|western union|moneygram|zelle|venmo|cash ?app|bitcoin|crypto|atm", "Asked for a wire, app or crypto payment", 3),
    (r"\bbail\b|jail|arrested|police station|lawyer|attorney", "Claims a relative is in legal trouble", 2),
    (r"grand(son|daughter|child|kid)|nephew|niece", "Caller claims to be or speak for a grandchild or relative", 1),
    (r"\birs\b|tax(es)? (owed|debt)|social security|\bssa\b|medicare|warrant", "Impersonates a government agency", 3),
    (r"don'?t tell|keep (it|this) (a )?secret|between us|don'?t call", "Asked to keep it secret from family", 3),
    (r"right now|immediately|urgent|within the hour|today only|act fast", "Pressure to act immediately", 2),
    (r"won|winner|lottery|sweepstakes|prize|inheritance", "Unexpected prize that requires a fee", 2),
    (r"remote|teamviewer|anydesk|install|access (to )?your computer|virus|microsoft|apple support", "Tech-support or remote access request", 3),
    (r"bank account|account number|routing|pin\b|password|verify your|card number|ssn|social security number", "Asked for bank or identity details", 3),
    (r"suspended|frozen|locked|compromised", "Claims an account is frozen or compromised", 2),
]

SAFE_ADVICE = {
    "high": "This sounds like a scam. Please don't send money, gift cards, or personal details. Hang up, and "
    "if you're worried, call your family on the number you already have. You did the right thing asking me.",
    "medium": "Some parts of this sound suspicious. Before doing anything, hang up and check with family or "
    "call the organization using a number you already trust. Real agencies never demand instant payment.",
    "low": "I don't see obvious warning signs, but never share bank details or send money to someone who "
    "contacted you first. When in doubt, check with family.",
}


def rule_assess(description: str) -> tuple[str, list[str], int]:
    text = description.lower()
    flags, score = [], 0
    for pattern, flag, weight in RULES:
        if re.search(pattern, text):
            flags.append(flag)
            score += weight
    risk = "high" if score >= 5 else "medium" if score >= 2 else "low"
    return risk, flags, score


async def assess(description: str) -> ScamAssessment:
    risk, flags, _ = rule_assess(description)
    advice = SAFE_ADVICE[risk]

    # A small local model adds latency (and its own mistakes) on the voice path; the rules stand alone.
    refined = None if llm.local() else await llm.complete(
        system=(
            "You protect an elderly person from phone, email and text scams. Given their description of a "
            "contact, return JSON only: {\"risk\": \"low|medium|high\", \"red_flags\": [short strings], "
            "\"advice\": \"2-3 calm, simple spoken sentences for an older adult\"}. Never tell them to pay, "
            "share details, or call back a number the contact supplied."
        ),
        prompt=f"Rule-based pre-screen: risk={risk}, flags={flags}\n\nTheir description:\n{description}",
        max_tokens=1024,
    )
    if refined:
        try:
            data = json.loads(refined[refined.find("{") : refined.rfind("}") + 1])
            order = ["low", "medium", "high"]
            # The LLM may raise the risk level, never lower it below the rules.
            llm_risk = data.get("risk", risk)
            if llm_risk in order and order.index(llm_risk) > order.index(risk):
                risk = llm_risk
            flags = list(dict.fromkeys(flags + [str(f) for f in data.get("red_flags", [])]))[:6]
            advice = str(data.get("advice") or advice)
        except (ValueError, TypeError):
            pass

    return ScamAssessment(risk=risk, red_flags=flags, advice=advice)
