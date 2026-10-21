# ruff: noqa: F811  (pytest fixtures imported from test_signin and test_telegram)
"""Many families on one server: signing up with Google or Amazon, each household's data kept apart,
the family sign-in list, and the routing of /mcp, Telegram, signed links and the scheduler."""

from __future__ import annotations

import asyncio
import dataclasses
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient
from test_signin import (  # noqa: F401  (fixtures)
    AMAZON,
    GOOGLE,
    KEY,
    access_token,
    app,
    client,
    cognito,
    homes,
    id_token,
    sign_in,
    verifier,
)
from test_telegram import bot, message, sent  # noqa: F401

from kinvesper import dashboard, households, media, notify, scheduler, signin
from kinvesper.config import HOUSEHOLD_HEADER, household_id
from kinvesper.households import HouseholdRouter, within
from kinvesper.models import Medication
from kinvesper.service import SettingsError
from kinvesper.store import JsonStore

ORIGIN = {"origin": "http://testserver"}


def sign_in_as(client, cognito, email, sub, **claims):
    client.cookies.clear()
    cognito["id_token"] = id_token(**{**AMAZON, **claims}, email=email, sub=sub)
    return sign_in(client, cognito)[1]


def sign_up(client, cognito, email="ravi@example.com", sub="amzn-ravi", **body):
    back = sign_in_as(client, cognito, email, sub)
    assert back.status_code == 302 and back.headers["location"] == "/welcome"
    form = {"patient_name": "Kamala Iyer", "preferred_name": "Amma", "your_name": "Ravi", "relation": "son", **body}
    r = client.post("/api/households", json=form, headers=ORIGIN)
    assert r.status_code == 201, r.text
    return r.json()["household"]


def test_a_new_family_signs_up_with_amazon_and_sees_only_their_own(client, cognito, homes, service):
    back = sign_in_as(client, cognito, "ravi@example.com", "amzn-ravi")
    assert back.status_code == 302 and back.headers["location"] == "/welcome"
    # signed in, but in no family yet: the dashboard sends them to set one up
    assert client.get("/dashboard").headers["location"] == "/welcome"
    assert client.get("/api/dashboard").json()["signup"] == "/welcome"
    assert client.get("/auth/me").status_code == 401
    page = client.get("/welcome")
    assert page.status_code == 200 and "Set up Kinvesper for your family" in page.text
    assert client.get("/api/households").json() == {"email": "ravi@example.com", "name": "ravi", "open": True}
    # a change from another site is refused
    assert client.post("/api/households", json={"patient_name": "X", "your_name": "Y"},
                       headers={"origin": "https://evil.example"}).status_code == 401
    assert client.post("/api/households", json={"patient_name": "", "your_name": "Ravi"}, headers=ORIGIN).status_code == 400

    r = client.post("/api/households", json={"patient_name": "Kamala Iyer", "preferred_name": "Amma", "your_name": "Ravi",
                                              "relation": "son"}, headers=ORIGIN)
    assert r.status_code == 201 and r.json()["next"] == "/dashboard/settings"
    home = r.json()["household"]
    assert home.startswith("home_") and homes.exists(home)

    mine = client.get("/api/dashboard").json()
    assert mine["profile"]["preferred_name"] == "Amma" and mine["profile"]["caregiver_email"] == "ravi@example.com"
    assert mine["medications"] == [] and mine["alerts"] == []
    assert client.get("/welcome").headers["location"] == "/dashboard"  # already set up
    assert client.post("/api/households", json={"patient_name": "Again", "your_name": "Ravi"}, headers=ORIGIN).status_code == 401

    # Margaret's household is untouched, and still what the key reaches
    margaret = client.get("/api/dashboard", headers={"x-dashboard-key": KEY}).json()
    assert margaret["profile"]["preferred_name"] == service.profile().preferred_name != "Amma"
    assert margaret["medications"]

    # settings saved by the new family land in their household only
    s = client.get("/api/settings").json()
    s["medications"] = [{"name": "Glycomet", "dose": "500 mg", "times": ["08:00"]}]
    s["profile"]["caregiver_phone"] = "+919876543210"
    assert client.put("/api/settings", json=s, headers=ORIGIN).status_code == 200
    assert [m.name for m in homes.service(home).medications()] == ["Glycomet"]
    assert "Glycomet" not in [m.name for m in service.medications()]


def test_the_family_list_lets_others_in(client, cognito, homes):
    home = sign_up(client, cognito)
    view = client.get("/api/family").json()
    assert [m["email"] for m in view["members"]] == ["ravi@example.com"] and view["you"] == "ravi@example.com"

    assert client.post("/api/family", json={"email": "not-an-email"}, headers=ORIGIN).status_code == 400
    r = client.post("/api/family", json={"email": "Meera@Example.com"}, headers=ORIGIN)
    assert r.status_code == 200 and [m["email"] for m in r.json()["members"]] == ["ravi@example.com", "meera@example.com"]
    assert r.json()["members"][1]["added_by"] == "ravi@example.com"
    assert "already" in client.post("/api/family", json={"email": "meera@example.com"}, headers=ORIGIN).json()["error"]
    # Margaret's caregiver can't be taken into another family
    homes.service(homes.first).store.save("profile", homes.service(homes.first).profile().model_copy(
        update={"caregiver_email": "priya@example.com"}), id="main")
    assert "another family" in client.post("/api/family", json={"email": "priya@example.com"}, headers=ORIGIN).json()["error"]
    assert "yourself" in client.post("/api/family/remove", json={"email": "ravi@example.com"}, headers=ORIGIN).json()["error"]

    # Meera signs in with Google and lands straight in the same family
    back = sign_in_as(client, cognito, "meera@example.com", "g-meera", **GOOGLE)
    assert back.headers["location"] == "/dashboard"
    assert client.get("/api/dashboard").json()["profile"]["preferred_name"] == "Amma"

    # taken off the list: her session stops working, and she'd start a family of her own
    client.cookies.clear()
    sign_in_as(client, cognito, "ravi@example.com", "amzn-ravi")
    assert client.post("/api/family/remove", json={"email": "meera@example.com"}, headers=ORIGIN).status_code == 200
    assert homes.resolve("meera@example.com", True) is None
    assert homes.members(home)[0].email == "ravi@example.com" and len(homes.members(home)) == 1


def test_margarets_family_list_shows_the_settings_emails(client, homes, service):
    service.store.save("profile", service.profile().model_copy(update={"caregiver_email": "priya@example.com"}), id="main")
    view = client.get("/api/family", headers={"x-dashboard-key": KEY}).json()
    assert view["also"] == ["priya@example.com"] and view["members"] == [] and view["you"] == ""
    r = client.post("/api/family", json={"email": "nurse@example.com"}, headers={"x-dashboard-key": KEY})
    assert r.json()["members"][0] == {**r.json()["members"][0], "email": "nurse@example.com", "added_by": "dashboard key"}
    assert homes.resolve("nurse@example.com", True) == homes.first


def test_sign_up_can_be_closed_and_is_capped(client, cognito, homes, monkeypatch):
    closed = dataclasses.replace(dashboard.settings, signup=False)
    monkeypatch.setattr(dashboard, "settings", closed)
    sign_in_as(client, cognito, "ravi@example.com", "amzn-ravi")
    assert client.get("/api/households").json()["open"] is False
    assert client.post("/api/households", json={"patient_name": "K", "your_name": "R"}, headers=ORIGIN).status_code == 403

    monkeypatch.setattr(households, "settings", dataclasses.replace(households.settings, max_households=1))
    homes.create("one@example.com", "One", "First Person", "", "")
    with pytest.raises(SettingsError, match="isn't taking new families"):
        homes.create("two@example.com", "Two", "Second Person", "", "")
    with pytest.raises(SettingsError, match="already belong"):
        homes.create("one@example.com", "One", "Another", "", "")


def test_a_pool_login_belongs_to_the_first_household_unless_listed(homes):
    assert homes.resolve("anyone@example.com", False) == homes.first
    assert homes.resolve("anyone@example.com", True) is None
    home = homes.create("ravi@example.com", "Ravi", "Kamala Iyer", "Amma", "son")
    homes.add_member(home, "carer@example.com", "ravi@example.com")
    assert homes.resolve("Carer@example.com", False) == home


def test_each_household_has_its_own_partition(tmp_path):
    store = JsonStore(str(tmp_path / "s.json"))
    store.put("med", "a", {"x": 1})
    other = store.scoped("home_1")
    other.put("med", "b", {"x": 2})
    assert store.list("med") == [{"x": 1}] and other.list("med") == [{"x": 2}]
    store.clear()  # reseeding the first household keeps everyone else's
    assert store.list("med") == [] and other.list("med") == [{"x": 2}]
    assert JsonStore(str(tmp_path / "s.json")).scoped("home_1").get("med", "b") == {"x": 2}


# ---------- which household a request is about ----------

async def test_the_router_picks_the_household(homes, cognito, monkeypatch):
    on = dataclasses.replace(households.settings, service_token="svc", simulator_key="sim", oauth_issuer=signin.settings.oauth_issuer)
    monkeypatch.setattr(households, "settings", on)
    home = homes.create("ravi@example.com", "Ravi", "Kamala Iyer", "Amma", "son")
    homes._verifier = verifier(homes, allow=False)
    seen = []

    async def app(scope, receive, send):
        seen.append(household_id())

    router = HouseholdRouter(app, homes)

    async def call(headers: dict) -> str:
        await router({"type": "http", "method": "GET", "path": "/mcp", "headers": [(k.encode(), v.encode()) for k, v in headers.items()]},
                     None, None)
        return seen[-1]

    assert await call({}) == homes.first
    assert await call({"authorization": "Bearer svc", HOUSEHOLD_HEADER: home}) == home
    assert await call({"authorization": "Bearer svc", HOUSEHOLD_HEADER: "home_nope"}) == homes.first
    assert await call({"authorization": "Bearer wrong", HOUSEHOLD_HEADER: home}) == homes.first  # the header needs the service token
    assert await call({"x-simulator-key": "sim"}) == homes.first

    ravi = access_token(username="loginwithamazon_1", sub="r1")
    cognito["userinfo"][ravi] = {"sub": "r1", "email": "ravi@example.com", "identities": '[{"providerName":"LoginWithAmazon"}]'}
    assert await call({"authorization": f"Bearer {ravi}"}) == home
    assert await homes.of_token(ravi) == home
    stranger = access_token(username="loginwithamazon_2", sub="r2")
    cognito["userinfo"][stranger] = {"sub": "r2", "email": "who@example.com", "identities": '[{"providerName":"LoginWithAmazon"}]'}
    assert await homes.of_token(stranger) is None
    assert await homes.of_token(access_token()) == homes.first  # a pool login, no email from Cognito
    assert household_id() == homes.first  # nothing leaks out of a request


def test_telegram_reaches_the_household_that_made_the_link(homes, service, bot):
    from test_telegram import webhook

    home = homes.create("ravi@example.com", "Ravi", "Kamala Iyer", "Amma", "son")
    theirs = homes.service(home)
    _, post = webhook(homes.current)
    with within(home):
        url = asyncio.run(theirs.telegram_link("caregiver"))["url"]
    assert post(message(f"/start {url.split('start=')[1]}", chat=777)).status_code == 200
    assert theirs.profile().caregiver_telegram == "777" and service.profile().caregiver_telegram != "777"
    assert "Amma" in sent(bot)[-1]["text"] or "Kamala" in sent(bot)[-1]["text"]

    post(message("Dinner is at 8", chat=777))
    assert [m.text for m in theirs.family_messages()][:1] == ["Dinner is at 8"]
    assert "Dinner is at 8" not in [m.text for m in service.family_messages()]
    asyncio.run(theirs.telegram_unlink("caregiver"))
    assert homes.directory.get("tgchat", "777") is None


def test_signed_links_and_alerts_carry_the_household(homes, monkeypatch):
    home = homes.create("ravi@example.com", "Ravi", "Kamala Iyer", "Amma", "son")
    with within(home):
        link = media.url("msg_1")
        assert notify._audience("caregiver") == f"{home}:caregiver"
    q = parse_qs(urlsplit(link).query)
    assert q["h"] == [home] and media.signature_ok("msg_1", q["e"][0], q["s"][0], home)
    assert not media.signature_ok("msg_1", q["e"][0], q["s"][0], homes.first)  # can't be pointed at another family
    assert notify._audience("caregiver") == "caregiver" and "h=" not in media.url("msg_1")
    client = TestClient(app(homes))
    assert client.get(f"/media/msg_1?e={q['e'][0]}&s={q['s'][0]}").status_code == 403


async def test_the_scheduler_checks_every_household(homes, monkeypatch):
    home = homes.create("ravi@example.com", "Ravi", "Kamala Iyer", "Amma", "son")
    theirs = homes.service(home)
    theirs.add_medication(Medication(name="Metformin", dose="500 mg", times=["00:00"], pills_remaining=1))
    audiences = []

    async def send(subject, body, phone="", audience="caregiver"):
        audiences.append((household_id(), notify._audience(audience), subject))
        return True
    monkeypatch.setattr(notify, "send", send)
    await scheduler.check_all(homes)
    mine = [a for a in theirs.alerts() if a.kind == "low_stock"]
    assert mine and "Metformin" in mine[0].title
    assert all(a.title != mine[0].title for a in homes.service(homes.first).alerts())
    assert (home, f"{home}:caregiver", "Kinvesper: Amma missed the 12 AM Metformin") in audiences
    assert all(tag == "caregiver" for h, tag, _ in audiences if h == homes.first)


def test_the_voice_simulator_needs_a_key_or_your_own_family(homes, cognito, monkeypatch):
    from kinvesper import web
    from kinvesper.voice import VoiceSessions

    monkeypatch.setattr(web, "settings", dataclasses.replace(web.settings, oauth_issuer=signin.settings.oauth_issuer,
                                                             service_token="svc", simulator_key="sim"))
    from starlette.applications import Starlette
    from starlette.middleware import Middleware

    client = TestClient(Starlette(routes=[*signin.signin_routes(homes.resolve), *dashboard.dashboard_routes(homes.current, homes),
                                          *web.voice_routes(VoiceSessions(), homes.current, homes)],
                                  middleware=[Middleware(HouseholdRouter, homes=homes)]), follow_redirects=False)
    assert client.get("/voice").headers["location"] == "/auth/login?next=/voice"
    assert client.get("/voice?key=sim").status_code == 200
    assert client.post("/api/voice/sessions").status_code == 403
    sign_up(client, cognito)
    assert client.get("/voice").status_code == 200
    assert client.delete("/api/voice/sessions/nope", headers=ORIGIN).status_code == 404  # past the guard: no such session
    assert client.delete("/api/voice/sessions/nope", headers={"origin": "https://evil.example"}).status_code == 403
