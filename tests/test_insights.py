"""The family safe word, wellbeing trends and the impact numbers."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kinvesper.dashboard import dashboard_routes
from kinvesper.seed import seed
from kinvesper.service import CareService
from kinvesper.store import JsonStore


@pytest.fixture
def household(tmp_path) -> CareService:
    """The demo household with its four earlier weeks, as `kinvesper --seed` makes it."""
    store = JsonStore(str(tmp_path / "state.json"))
    seed(store)
    return CareService(store)


def settings_body(service):
    view = asyncio.run(service.settings_view())
    return {"profile": {**view["profile"], "caregiver_phone": "+15551234567"}, "medications": view["medications"], "contacts": view["contacts"]}


# ---------- safe word ----------

def test_safe_word_is_saved_from_settings_and_kept_out_of_the_profile(service, tmp_path):
    client = TestClient(Starlette(routes=dashboard_routes(service)))
    r = client.put("/api/settings", json={**settings_body(service), "safe_word": "  Sunflower  "})
    assert r.status_code == 200 and r.json()["safe_word"] == "Sunflower"
    assert service.safe_word() == "Sunflower"
    assert "Sunflower" not in json.dumps(service.profile().model_dump()) and "Sunflower" not in json.dumps(service.dashboard())

    bad = client.put("/api/settings", json={**settings_body(service), "safe_word": "ab", "profile": {
        **settings_body(service)["profile"], "caregiver_name": "Changed"}})
    assert bad.status_code == 400 and "3 to 40" in bad.json()["error"]
    assert service.profile().caregiver_name == "Priya" and service.safe_word() == "Sunflower"  # nothing saved

    assert client.put("/api/settings", json={**settings_body(service), "safe_word": ""}).status_code == 200
    assert service.safe_word() == ""


def test_a_family_claim_gets_the_safe_word_advice(service):
    plain = asyncio.run(service.check_scam("A man says he's my grandson and needs money for bail"))
    assert "safe word" not in plain.advice
    service.set_safe_word("sunflower")
    told = asyncio.run(service.check_scam("A man says he's my grandson and needs money for bail"))
    assert "Ask them for your family's safe word" in told.advice and "sunflower" not in told.advice
    assert not asyncio.run(service.check_scam("The electricity board says my bill is overdue")).advice.count("safe word")


def test_checking_the_word_never_says_it(service):
    assert not asyncio.run(service.check_safe_word("sunflower")).set
    service.set_safe_word("Sunflower Seeds")
    ok = asyncio.run(service.check_safe_word("he said sunflower seeds"))
    assert ok.matches and "Sunflower" not in ok.message and not service.alerts()
    assert asyncio.run(service.check_safe_word("Sunflower seed")).matches  # close enough when repeated by ear
    wrong = asyncio.run(service.check_safe_word("rose"))
    assert wrong.matches is False and wrong.family_alerted and "hang up" in wrong.message
    assert "Sunflower" not in wrong.message
    alert = service.alerts()[0]
    assert alert.level == "urgent" and alert.kind == "scam" and "safe-word" in alert.title


# ---------- wellbeing trends ----------

def test_trends_flag_several_changes_together(household):
    t = household.wellbeing_trends()
    assert len(t["weeks"]) == 5 and t["level"] == "visit"
    keys = {s["key"] for s in t["signals"]}
    assert {"mood", "talks", "repeats"} <= keys and len(keys) >= 3
    this, before = t["weeks"][-1], t["weeks"][0]
    assert this["talks"] < before["talks"] and this["repeats"] > before["repeats"]
    assert "not a diagnosis" in t["note"]


def test_a_steady_week_is_steady(service):
    t = service.wellbeing_trends()  # no earlier weeks to compare with
    assert t["level"] == "steady" and not t["signals"]


def test_a_worrying_week_alerts_the_family_once(household):
    alert = asyncio.run(household.check_trends())
    assert alert and alert.kind == "pattern" and "worth a visit" in alert.title
    assert asyncio.run(household.check_trends()) is None


def test_talking_to_kinvesper_counts(service):
    service.touch_activity()
    service.touch_activity()
    day = service.profile().last_activity[:10]
    assert service.store.get("activity", day)["count"] == 2


# ---------- impact ----------

def test_impact_numbers(household):
    m = household.impact()
    assert m["scams_caught"] == 2 and m["urgent_alerts"] == 2 and m["response_min"] in (3, 4)
    assert m["family_messages"] == 2 and m["conversations"] > 100
    assert m["doses_scheduled"] > 100 and 70 <= m["adherence"] <= 100 and m["on_time"] > 0


def test_acknowledging_records_when(service):
    alert = asyncio.run(service.raise_alert("urgent", "emergency", "Fell", "in the kitchen", sms=False))
    first = service.acknowledge(alert.id).acknowledged_at
    assert first and service.acknowledge(alert.id).acknowledged_at == first
    assert service.impact()["response_min"] == 0


def test_the_dashboard_carries_both(household):
    d = household.dashboard()
    assert d["trends"]["level"] == "visit" and d["impact"]["scams_caught"] == 2


def test_no_safe_word_advice_once_the_caller_gave_one(service):
    service.set_safe_word("sunflower")
    r = asyncio.run(service.check_scam("He says he's my grandson and the safe word is tulip"))
    assert "Ask them for your family's safe word" not in r.advice
