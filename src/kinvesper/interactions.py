"""Medicine interaction check: the FDA's drug labels (openFDA), read by Claude.

Each medicine's FDA label has a "Drug interactions" section (prescription medicines) or "Ask a doctor
or pharmacist before use if you are taking..." (over-the-counter ones). Claude reads those sections for
the medicines in question and names the pairs that interact, in plain words, citing only what the
labels say. Without Claude, a pair is flagged when one label names the other medicine or its class
(the lisinopril label names "non-steroidal anti-inflammatory agents"; ibuprofen is one).

It never says a combination is safe: no finding means the labels don't mention it, so the advice is
always to check with a pharmacist or doctor.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from . import llm
from .models import Interaction, InteractionResult

log = logging.getLogger(__name__)

OPENFDA = "https://api.fda.gov/drug/label.json"
TIMEOUT_S = 6
MAX_LABEL_CHARS = 4000  # per medicine, for Claude: the interactions section is usually shorter
SECTIONS = ("drug_interactions", "ask_doctor_or_pharmacist", "ask_doctor", "do_not_use", "contraindications")
# Class words the labels use, and the medicines' classes as the FDA files them ("[EPC]" dropped).
CLASS_ALIASES = {"nonsteroidal anti-inflammatory": ("nsaid", "non-steroidal anti-inflammatory", "nonsteroidal anti-inflammatory"),
                 "vitamin k antagonist": ("anticoagula", "blood thinner", "warfarin"),
                 "thiazide diuretic": ("diuretic",), "loop diuretic": ("diuretic",)}
SEVERITY = {"serious": 0, "moderate": 1, "minor": 2}


@dataclass
class Label:
    query: str
    names: set[str] = field(default_factory=set)  # generic, brand and substance names, lower case
    classes: list[str] = field(default_factory=list)
    text: str = ""


_cache: dict[str, Label | None] = {}


def base_name(medicine: str) -> str:
    """'Lisinopril 10 mg' -> 'lisinopril'; 'Tylenol Extra Strength' -> 'tylenol extra strength'."""
    words = re.split(r"\s+(?=\d)", medicine.strip().lower(), maxsplit=1)[0]
    return re.sub(r"[^a-z0-9\- ]", "", words).strip()


async def _search(http: httpx.AsyncClient, q: str) -> list[dict]:
    term = quote(f'"{q}"')
    url = f"{OPENFDA}?search=openfda.substance_name:{term}+openfda.brand_name:{term}+openfda.generic_name:{term}&limit=10"
    r = await http.get(url)
    if r.status_code == 404:  # openFDA's "no matches"
        return []
    r.raise_for_status()
    return r.json().get("results", [])


def _pick(results: list[dict], q: str) -> dict | None:
    """The best label: one ingredient, named like the query, with an interactions section."""
    def score(r: dict) -> tuple:
        fda = r.get("openfda") or {}
        substances = fda.get("substance_name") or []
        names = " ".join(substances + (fda.get("brand_name") or []) + (fda.get("generic_name") or [])).lower()
        return (len(substances) == 1, q in names, bool(r.get("drug_interactions")),
                any(r.get(s) for s in SECTIONS))
    ranked = sorted(results, key=score, reverse=True)
    return ranked[0] if ranked and any(ranked[0].get(s) for s in SECTIONS) else None


async def label(http: httpx.AsyncClient, medicine: str) -> Label | None:
    q = base_name(medicine)
    if not q:
        return None
    if q in _cache:
        return _cache[q]
    try:
        best = _pick(await _search(http, q), q)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("openFDA lookup for %s failed: %s", q, e)
        return None  # not cached: try again next time
    found = None
    if best:
        fda = best.get("openfda") or {}
        names = {n.lower() for k in ("generic_name", "brand_name", "substance_name") for n in fda.get(k) or []}
        names |= {n.split()[0] for n in names if n.split()}  # "warfarin sodium" -> "warfarin"
        names.add(q)
        classes = [re.sub(r"\s*\[EPC\]$", "", c).lower() for c in fda.get("pharm_class_epc") or []]
        text = "\n".join(" ".join(best[s]) for s in SECTIONS if best.get(s))
        found = Label(query=q, names=names, classes=classes, text=re.sub(r"\s+", " ", text))
    _cache[q] = found
    return found


def _terms(lbl: Label) -> set[str]:
    """Words another label would use for this medicine: its names and its class."""
    terms = {n for n in lbl.names if len(n) > 3}
    for c in lbl.classes:
        stem = re.sub(r"\s+(drug|agent|inhibitor)s?$", "", c)
        terms.add(stem)
        terms.update(CLASS_ALIASES.get(stem, ()))
    return terms


def _mentions(a: Label, b: Label) -> str | None:
    """The sentence of a's label that names b (or b's class), if any."""
    text = a.text.lower()
    for term in sorted(_terms(b), key=len, reverse=True):
        if (i := text.find(term)) >= 0:
            start = max(text.rfind(". ", 0, i) + 2, 0)
            end = text.find(". ", i)
            return a.text[start:end + 1 if end > 0 else start + 300].strip()
    return None


def _rule_pairs(pairs: list[tuple[str, str]], labels: dict[str, Label]) -> list[Interaction]:
    out = []
    for a, b in pairs:
        if _mentions(labels[a], labels[b]):
            out.append(Interaction(medicines=[a, b], severity="moderate",
                                   explanation=f"{b}'s FDA label warns about taking it with medicines like {a}."))
        elif _mentions(labels[b], labels[a]):
            out.append(Interaction(medicines=[a, b], severity="moderate",
                                   explanation=f"{a}'s FDA label warns about taking it with medicines like {b}."))
    return out


SCHEMA = {
    "type": "object",
    "properties": {"interactions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "medicines": {"type": "array", "items": {"type": "string"}},
            "severity": {"type": "string", "enum": ["serious", "moderate", "minor"]},
            "explanation": {"type": "string"},
        },
        "required": ["medicines", "severity", "explanation"], "additionalProperties": False}}},
    "required": ["interactions"], "additionalProperties": False,
}
SYSTEM = (
    "You check medicines for interactions for an older adult and their family, using only the FDA label "
    "excerpts given. List each pair from the pairs to check that a label says can interact (by name or by "
    "drug class). For each: the two medicine names as the user wrote them, a severity (serious: the label "
    "warns of bleeding, organ damage, dangerous blood pressure or blood sugar, or says not to combine; "
    "moderate: needs monitoring or a dose change; minor), and one or two plain sentences on what could happen. "
    "No jargon, no dosing advice. If the labels say nothing about a pair, leave it out. Never invent interactions."
)


async def check(new: list[str], current: list[str], transport: httpx.AsyncBaseTransport | None = None) -> InteractionResult:
    """Interactions between the `new` medicines and the `current` ones (and among the new ones)."""
    names = list(dict.fromkeys([m.strip() for m in new + current if m.strip()]))
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport, headers={"User-Agent": "Kinvesper"}) as http:
        labels = dict(zip(names, await asyncio.gather(*(label(http, n) for n in names)), strict=True))
    not_found = [n for n, lbl in labels.items() if lbl is None]
    new_set = [n for n in dict.fromkeys(new) if labels.get(n)]
    pairs = [(n, c) for i, n in enumerate(new_set) for c in [*new_set[i + 1:], *current]
             if labels.get(c) and base_name(c) != base_name(n)]
    pairs = list(dict.fromkeys(pairs))
    found: list[Interaction] = []
    if pairs:
        excerpts = "\n\n".join(f"## {n}\n{labels[n].text[:MAX_LABEL_CHARS]}" for n in dict.fromkeys(x for p in pairs for x in p))
        asked = "\n".join(f"- {a} + {b}" for a, b in pairs)
        data = await llm.structured(SYSTEM, f"Pairs to check:\n{asked}\n\nFDA label excerpts:\n{excerpts}", SCHEMA, max_tokens=1500)
        if data is not None:
            wanted = {frozenset(map(base_name, p)) for p in pairs}
            found = [i for i in (Interaction.model_validate(x) for x in data.get("interactions", []))
                     if len(i.medicines) == 2 and frozenset(map(base_name, i.medicines)) in wanted]
        else:
            found = _rule_pairs(pairs, labels)
    found.sort(key=lambda i: SEVERITY[i.severity])
    return InteractionResult(checked=names, interactions=found, not_found=not_found, message=spoken(found, new, not_found))


def spoken(found: list[Interaction], new: list[str], not_found: list[str]) -> str:
    asked = " and ".join(new) or "these medicines"
    if found:
        first = found[0]
        other = next((m for m in first.medicines if base_name(m) not in map(base_name, new)), first.medicines[-1])
        more = f" There {'is' if len(found) == 2 else 'are'} {len(found) - 1} more to ask about." if len(found) > 1 else ""
        if first.severity == "serious":
            return (f"Please don't take {asked} with your {other} until you've checked with your doctor or pharmacist. "
                    f"{first.explanation}{more}")
        return f"Please check with your pharmacist before taking {asked} with your {other}. {first.explanation}{more}"
    if not_found and set(map(base_name, not_found)) >= set(map(base_name, new)):
        return f"I couldn't find {asked} in the FDA's medicine labels. Please ask your pharmacist before taking it."
    return (f"The FDA labels don't mention a problem between {asked} and your other medicines. "
            "Still, it's best to check with your pharmacist.")
