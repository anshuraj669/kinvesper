"""Phase 4: proactive checks (missed doses, inactivity, low stock) and the caregiver dashboard."""

from datetime import timedelta

import httpx
import pytest

from kinvesper import service as service_module
from kinvesper.config import now
from kinvesper.models import Medication, Refill
from kinvesper.scheduler import check_once


@pytest.fixture
def clock(monkeypatch):
    """Freeze CareService's clock; returns a setter taking 'HH:MM' (today)."""
    today = now()
    state = {"t": today}
    monkeypatch.setattr(service_module, "now", lambda: state["t"])

    def set_time(hhmm: str):
        h, m = map(int, hhmm.split(":"))
        state["t"] = today.replace(hour=h, minute=m, second=0, microsecond=0)
        return state["t"]

    return set_time


@pytest.fixture
def texts(monkeypatch):
    sent = []

    async def fake_send(subject, body):
        sent.append(subject)
        return True

    monkeypatch.setattr(service_module.notify, "send", fake_send)
    return sent


def _alerts(service, kind, contains=""):
    return [a for a in service.alerts() if a.kind == kind and contains in (a.dedupe_key or "")]


async def test_missed_dose_alert_fires_exactly_once(service, clock, texts):
    service.add_medication(Medication(id="med_t", name="Testamine", dose="5 mg", times=["09:00"]))

    clock("09:45")  # inside the 60 min grace period
    await service.run_checks()
    assert not _alerts(service, "missed_dose", "med_t")

    clock("10:01")
    await service.run_checks()
    clock("10:30")
    await service.run_checks()
    await check_once(service)

    missed = _alerts(service, "missed_dose", "med_t")
    assert len(missed) == 1
    assert missed[0].notified
    assert sum("Testamine" in s for s in texts) == 1  # texted once, not once per check


async def test_logged_dose_is_not_missed(service, clock, texts):
    service.add_medication(Medication(id="med_t", name="Testamine", dose="5 mg", times=["09:00"]))
    service.log_dose("Testamine", taken_at=clock("09:10"))
    clock("11:00")
    await service.run_checks()
    assert not _alerts(service, "missed_dose", "med_t")


async def test_inactivity_once_per_quiet_spell(service, clock, texts):
    def last_active(hhmm):
        p = service.profile()
        p.last_activity = clock(hhmm).isoformat()
        service.store.save("profile", p, id="main")

    last_active("07:00")
    clock("10:00")  # 3 h: under the 4 h threshold
    await service.run_checks()
    assert not _alerts(service, "inactivity")

    clock("12:00")
    await service.run_checks()
    clock("13:00")
    await service.run_checks()
    assert len(_alerts(service, "inactivity")) == 1

    last_active("13:30")  # activity resets the spell
    clock("18:00")
    await service.run_checks()
    assert len(_alerts(service, "inactivity")) == 2


async def test_no_inactivity_alerts_at_night(service, clock, texts):
    p = service.profile()
    p.last_activity = clock("12:00").isoformat()
    service.store.save("profile", p, id="main")
    clock("23:30")
    await service.run_checks()
    assert not _alerts(service, "inactivity")


async def test_low_stock_reminder_is_quiet(service, clock, texts):
    clock("12:00")
    await service.run_checks()
    await service.run_checks()
    low = _alerts(service, "low_stock", "med_atorvastatin")
    assert len(low) == 1 and low[0].level == "info" and not low[0].notified  # dashboard only, no text


async def test_open_refill_suppresses_low_stock(service, clock, texts):
    stamp = clock("12:00").isoformat()
    service.store.save("refill", Refill(medication_id="med_atorvastatin", medication_name="Atorvastatin",
                                        status="verifying", created_at=stamp, updated_at=stamp))
    await service.run_checks()
    assert not _alerts(service, "low_stock", "med_atorvastatin")


def test_dashboard_data(service):
    d = service.dashboard()
    assert d["profile"]["preferred_name"] == "Margaret"
    assert len(d["adherence"]["by_day"]) == 7
    assert 0 <= d["adherence"]["pct"] <= 100
    assert len(d["today"]["items"]) == 5
    assert any(m["low_stock"] for m in d["medications"])
    assert d["checkins"]


# ---------- HTTP ----------


def test_dashboard_api(server_url):
    base = server_url.removesuffix("/mcp")
    assert httpx.get(f"{base}/dashboard").status_code == 200

    # a dose logged through the voice simulator shows up on the dashboard
    slot = (now() - timedelta(minutes=1)).strftime("%H:%M")
    sid = httpx.post(f"{base}/api/voice/sessions").json()["id"]
    turns = f"{base}/api/voice/sessions/{sid}/turns"
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def add_med():
        async with streamable_http_client(server_url) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                await s.call_tool("add_medication", {"name": "Dashboxin", "dose": "1 mg", "times": [slot]})

    asyncio.run(add_med())
    httpx.post(turns, json={"text": "I just took my Dashboxin"}, timeout=30)
    data = httpx.get(f"{base}/api/dashboard").json()
    item = next(i for i in data["today"]["items"] if i["medication"] == "Dashboxin")
    assert item["status"] == "taken"

    assert "raised" in httpx.post(f"{base}/api/checks").json()
    alert_id = httpx.get(f"{base}/api/dashboard").json()["alerts"][0]["id"]
    assert httpx.post(f"{base}/api/alerts/{alert_id}/ack").json()["acknowledged"] is True
    assert httpx.post(f"{base}/api/alerts/nope/ack").status_code == 404


def test_dashboard_off_with_oauth_and_no_key(auth_server):
    assert httpx.get(f"{auth_server}/dashboard").status_code == 404
    assert httpx.get(f"{auth_server}/api/dashboard").status_code == 404
