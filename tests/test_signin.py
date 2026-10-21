"""Caregiver sign-in on the dashboard and Alexa account linking, against a fake Cognito."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from kinvesper import alexa, dashboard, households, signin
from kinvesper.alexa import AlexaBridge
from kinvesper.auth import JwtTokenVerifier
from kinvesper.households import HouseholdRouter, Households
from kinvesper.voice import VoiceSessions

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
LOGIN = "https://kinvesper-1.auth.us-east-1.amazoncognito.com"
CLIENT = "dashboard-client"
KEY = "k" * 40
PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StaticJwks:
    def get_signing_key_from_jwt(self, _token):
        return type("Key", (), {"key": PRIVATE.public_key()})()


def id_token(**claims) -> str:
    base = {"iss": ISSUER, "aud": CLIENT, "sub": "user-1", "exp": int(time.time()) + 600, "token_use": "id",
            "email": "priya@example.com", "email_verified": True, "cognito:username": "priya"}
    return jwt.encode({**base, **claims}, PRIVATE, algorithm="RS256")


@pytest.fixture
def cognito(monkeypatch):
    """Sign-in turned on, with a fake Cognito. `cognito["id_token"]` is what its token endpoint returns."""
    state = {"id_token": id_token(), "token_requests": [], "userinfo": {}}

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            form = parse_qs(request.content.decode())
            state["token_requests"].append(form)
            return httpx.Response(200, json={"id_token": state["id_token"], "access_token": "at", "token_type": "Bearer"})
        if request.url.path == "/oauth2/userInfo":
            info = state["userinfo"].get(request.headers["authorization"].removeprefix("Bearer "))
            return httpx.Response(200, json=info) if info else httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(404)

    on = dataclasses.replace(signin.settings, oauth_issuer=ISSUER, login_client_id=CLIENT, dashboard_key=KEY,
                             public_url="http://testserver")
    for module in (signin, dashboard, households):
        monkeypatch.setattr(module, "settings", on)
    monkeypatch.setattr(signin, "_transport", httpx.MockTransport(api))
    monkeypatch.setattr(signin, "_jwks", StaticJwks())
    monkeypatch.setattr(signin, "_discovery", {
        "issuer": ISSUER, "authorization_endpoint": f"{LOGIN}/oauth2/authorize", "token_endpoint": f"{LOGIN}/oauth2/token",
        "userinfo_endpoint": f"{LOGIN}/oauth2/userInfo"})
    monkeypatch.setattr(signin, "_userinfo", {})
    return state


@pytest.fixture
def homes(service):
    return Households(service)


def app(homes):
    return Starlette(routes=[*signin.signin_routes(homes.resolve), *dashboard.dashboard_routes(homes.current, homes)],
                     middleware=[Middleware(HouseholdRouter, homes=homes)])


@pytest.fixture
def client(homes, cognito):
    return TestClient(app(homes), follow_redirects=False)


def sign_in(client, cognito, next_path="/dashboard"):
    r = client.get(f"/auth/login?next={next_path}")
    assert r.status_code == 302
    sent = parse_qs(urlsplit(r.headers["location"]).query)
    back = client.get(f"/auth/callback?code=the-code&state={sent['state'][0]}")
    return sent, back


def test_the_dashboard_sends_you_to_sign_in_and_back(client, cognito, service):
    r = client.get("/dashboard/settings")
    assert r.status_code == 302 and r.headers["location"] == "/auth/login?next=/dashboard/settings"
    assert client.get("/api/dashboard").status_code == 401

    sent, back = sign_in(client, cognito, "/dashboard/settings")
    assert sent["client_id"] == [CLIENT] and sent["redirect_uri"] == ["http://testserver/auth/callback"]
    assert sent["code_challenge_method"] == ["S256"] and set(sent["scope"][0].split()) == {"openid", "email", "profile"}
    # the token request proves the PKCE verifier behind the challenge
    verifier = cognito["token_requests"][0]["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert sent["code_challenge"] == [challenge]

    assert back.status_code == 302 and back.headers["location"] == "/dashboard/settings"
    cookie = back.cookies.get(signin.COOKIE)
    assert cookie and "httponly" in back.headers["set-cookie"].lower() and "samesite=lax" in back.headers["set-cookie"].lower()
    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/auth/me").json() == {"email": "priya@example.com", "name": "priya"}
    # changes need this site's Origin as well as the cookie
    assert client.post("/api/checks", headers={"origin": "https://evil.example"}).status_code == 401
    assert client.post("/api/checks", headers={"origin": "http://testserver"}).status_code == 200

    out = client.get("/auth/logout")
    assert out.headers["location"].startswith(f"{LOGIN}/logout?client_id={CLIENT}&logout_uri=")
    client.cookies.clear()
    assert client.get("/api/dashboard").status_code == 401


def test_the_key_link_still_works(client):
    assert client.get("/dashboard?key=anything").status_code == 200  # the page; its calls carry the key
    assert client.get("/api/dashboard", headers={"x-dashboard-key": KEY}).status_code == 200
    assert client.get("/api/dashboard", headers={"x-dashboard-key": "wrong"}).status_code == 403


def test_a_stale_or_forged_sign_in_is_refused(client, cognito):
    client.get("/auth/login")
    assert client.get("/auth/callback?code=c&state=not-the-state").status_code == 400
    assert not cognito["token_requests"]
    client.cookies.set(signin.COOKIE, signin.seal({"sub": "x", "exp": int(time.time()) + 60})[:-2] + "AA")
    assert client.get("/api/dashboard").status_code == 401
    expired = signin.seal({"sub": "x", "exp": int(time.time()) - 1})
    assert signin.unseal(expired) is None


def test_off_site_next_links_are_ignored(client, cognito):
    _, back = sign_in(client, cognito, "//evil.example/x")
    assert back.headers["location"] == "/dashboard"


GOOGLE = {"cognito:username": "google_1093", "identities": [{"providerName": "Google", "userId": "1093"}]}


def test_google_logins_must_be_the_familys(client, cognito, service, monkeypatch):
    monkeypatch.setattr(signin, "settings", dataclasses.replace(signin.settings, signup=False))
    cognito["id_token"] = id_token(**GOOGLE, email="stranger@gmail.com")
    _, back = sign_in(client, cognito)
    assert back.status_code == 403 and "isn't one of this family's logins" in back.text
    assert not back.cookies.get(signin.COOKIE)

    p = service.profile()
    service.store.save("profile", p.model_copy(update={"backup_email": "Raj@Gmail.com"}), id="main")
    cognito["id_token"] = id_token(**GOOGLE, email="raj@gmail.com", sub="user-2")
    _, back = sign_in(client, cognito)
    assert back.status_code == 302 and client.get("/api/dashboard").status_code == 200

    # an unverified Google email is never enough
    assert not signin.allowed({**GOOGLE, "email": "raj@gmail.com", "email_verified": False}, {"raj@gmail.com"})

    # taken out of Settings: the session stops working
    service.store.save("profile", service.profile().model_copy(update={"backup_email": ""}), id="main")
    assert client.get("/api/dashboard").status_code == 401


def test_logins_made_in_the_pool_are_always_let_in():
    assert signin.allowed({"cognito:username": "margaret", "email": "anyone@example.com"}, set())
    assert signin.federated({"username": "google_123"}) and signin.federated({"cognito:groups": ["us-east-1_X_Google"]})
    assert not signin.federated({"username": "priya@example.com"})


def test_caregiver_logins_adds_emails(monkeypatch, service):
    monkeypatch.setattr(signin, "settings", dataclasses.replace(signin.settings, caregiver_logins="a@x.com, B@y.com"))
    assert {"a@x.com", "b@y.com"} <= signin.family_emails(service.profile())


# ---------- access tokens: Alexa account linking and /mcp ----------

def access_token(**claims) -> str:
    base = {"iss": ISSUER, "client_id": "alexa-client", "sub": "user-1", "exp": int(time.time()) + 600,
            "token_use": "access", "scope": "openid email kinvesper/mcp", "username": "priya"}
    return jwt.encode({**base, **claims}, PRIVATE, algorithm="RS256")


def verifier(homes, allow=True):
    settings = dataclasses.replace(signin.settings, oauth_issuer=ISSUER, oauth_audience="alexa-client")
    return JwtTokenVerifier(settings, jwks_client=StaticJwks(), allow=signin.token_check(homes.resolve) if allow else None)


def test_a_google_token_is_checked_against_the_family(cognito, homes, service):
    v = verifier(homes)
    assert asyncio.run(v.verify_token(access_token())) is not None  # a pool login

    stranger = access_token(username="google_1", sub="g1")
    cognito["userinfo"][stranger] = {"sub": "g1", "email": "stranger@gmail.com", "email_verified": "true",
                                     "identities": '[{"providerName":"Google"}]'}
    assert asyncio.run(v.verify_token(stranger)) is None

    service.store.save("profile", service.profile().model_copy(update={"caregiver_email": "priya@example.com"}), id="main")
    family = access_token(username="google_2", sub="g2")
    cognito["userinfo"][family] = {"sub": "g2", "email": "Priya@example.com", "email_verified": "true",
                                   "identities": '[{"providerName":"Google"}]'}
    assert asyncio.run(v.verify_token(family)) is not None


@pytest.fixture
async def bridge(connect, monkeypatch, homes, cognito):
    monkeypatch.setattr(alexa.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    homes._verifier = verifier(homes, allow=False)
    yield AlexaBridge(sessions, homes.current, homes.of_token, required=True)
    await sessions.close_all()


def launch(token: str | None) -> dict:
    user = {"userId": "amzn1.ask.account.X", **({"accessToken": token} if token else {})}
    return {"session": {"sessionId": "s-link", "new": True, "user": user}, "request": {"type": "LaunchRequest", "locale": "en-US"},
            "context": {"System": {"user": user, "device": {"supportedInterfaces": {}}}}}


async def test_alexa_asks_for_linking_until_a_family_login_is_linked(bridge):
    for token in (None, "not-a-jwt", access_token(exp=int(time.time()) - 5)):
        r = await bridge.handle(launch(token))
        assert r["response"]["card"] == {"type": "LinkAccount"} and r["response"]["shouldEndSession"] is True
        assert "Link account" in r["response"]["outputSpeech"]["text"]
    r = await bridge.handle(launch(access_token()))
    assert "card" not in r["response"] and r["response"]["shouldEndSession"] is False
    # Alexa's "session ended" needs no linking check
    assert await bridge.handle({"session": {"sessionId": "s-link"}, "request": {"type": "SessionEndedRequest"}}) == \
        {"version": "1.0", "response": {}}


AMAZON = {"cognito:username": "loginwithamazon_amzn1.account.X", "identities": [{"providerName": "LoginWithAmazon"}]}


def test_login_with_amazon_needs_no_email_verified_claim(client, cognito, service):
    service.store.save("profile", service.profile().model_copy(update={"caregiver_email": "priya@example.com"}), id="main")
    token = id_token(**AMAZON, sub="amzn-1")
    claims = jwt.decode(token, options={"verify_signature": False})
    del claims["email_verified"]
    cognito["id_token"] = jwt.encode(claims, PRIVATE, algorithm="RS256")
    _, back = sign_in(client, cognito)
    assert back.status_code == 302 and client.get("/api/dashboard").status_code == 200
    assert not signin.allowed({**AMAZON, "email": "stranger@example.com"}, {"priya@example.com"})
    assert signin.provider({"username": "loginwithamazon_amzn1"}) == "loginwithamazon"
    r = client.get("/auth/login?provider=LoginWithAmazon")
    assert parse_qs(urlsplit(r.headers["location"]).query)["identity_provider"] == ["LoginWithAmazon"]
