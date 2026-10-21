"""Caregiver sign-in: the dashboard's login page and the check behind Alexa account linking.

    GET /auth/login?next=/dashboard   to Amazon Cognito's hosted login (authorization code + PKCE)
    GET /auth/callback                Cognito comes back here: the ID token is checked, a session cookie set
    GET /auth/logout                  clears the session, and Cognito's
    GET /auth/signed-out              "You're signed out"
    GET /auth/me                      who is signed in ({"email", "name"}), 401 when nobody is

Cognito is the identity provider for everything: its own logins (made with `make login`, no self
sign-up) and, once their client IDs and secrets are set up, "Continue with Google" and "Login with
Amazon" on the same page. Which household a login belongs to is households.Households.resolve: an
email on a family's sign-in list, or for the first household its caregiver's or backup's email in
Settings or CAREGIVER_LOGINS. A Google or Amazon login with none signs in only to start a household
of its own (/welcome, when SIGNUP is on); a login made in the user pool belongs to the first household
unless a family lists it. Only verified emails count. (Amazon sends no email_verified claim; it
confirms an account's email before the account can be used, so its email counts as verified.)

The same rule decides which accounts may link the Alexa skill and Alexa+ (see auth.JwtTokenVerifier):
their access tokens carry no email, so it comes from Cognito's userInfo endpoint.

The session is a signed cookie (HMAC, 7 days), HttpOnly and SameSite=Lax. Requests it authorizes that
change something must also come from this site (the Origin header), as a second guard against CSRF.
On when OAUTH_ISSUER and LOGIN_CLIENT_ID are set; the ?key= links keep working beside it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlencode, urlsplit

import anyio
import httpx
import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .config import settings

log = logging.getLogger(__name__)

COOKIE = "kv_session"
FLOW_COOKIE = "kv_login"  # state, PKCE verifier and where to go back to, during one sign-in
SESSION_S = 7 * 24 * 3600
FLOW_S = 10 * 60
USERINFO_CACHE_S = 600
# Cognito names a federated user "<provider>_<id>" and puts them in the group "<pool id>_<provider>".
PROVIDERS = ("google", "loginwithamazon", "signinwithapple", "facebook")
VERIFIED_EMAIL = {"loginwithamazon"}  # sends no email_verified, but only verified emails sign in

_discovery: dict = {}
_userinfo: dict[str, tuple[float, dict | None]] = {}
_jwks: jwt.PyJWKClient | None = None
_transport: httpx.AsyncBaseTransport | None = None  # tests swap in a fake Cognito


def _http(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


def enabled() -> bool:
    return bool(settings.oauth_issuer and settings.login_client_id)


def _key() -> bytes:
    secret = settings.dashboard_key or settings.service_token or settings.login_client_id or "kinvesper-local"
    return hmac.new(secret.encode(), b"kinvesper-session", hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal(data: dict) -> str:
    body = _b64(json.dumps(data, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())}"


def unseal(value: str) -> dict | None:
    """The data in a cookie this server signed, or None if it's forged, mangled or expired."""
    body, _, sig = (value or "").partition(".")
    expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    if not body or not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("exp", 0) > time.time() else None


# ---------- who may sign in ----------

# The household of a login's email (and whether it's a Google/Amazon login), or None: households.py.
Resolve = Callable[[str, bool], str | None]

def family_emails(profile) -> set[str]:
    listed = settings.caregiver_logins.replace(",", " ").split()
    return {e.strip().lower() for e in [*listed, profile.caregiver_email, profile.backup_email] if e and "@" in e}


def federated(claims: dict) -> bool:
    """A Google (or other outside) login, rather than one made in the user pool."""
    if claims.get("identities"):
        return True
    name = str(claims.get("cognito:username") or claims.get("username") or "").lower()
    groups = [str(g).lower() for g in claims.get("cognito:groups") or []]
    return name.startswith(tuple(f"{p}_" for p in PROVIDERS)) or any(g.endswith(tuple(f"_{p}" for p in PROVIDERS)) for g in groups)


def provider(claims: dict) -> str:
    """The outside identity provider of a federated login, lowercased ("google", "loginwithamazon")."""
    for identity in claims.get("identities") or []:
        if isinstance(identity, dict) and identity.get("providerName"):
            return str(identity["providerName"]).lower()
    name = str(claims.get("cognito:username") or claims.get("username") or "").lower()
    return next((p for p in PROVIDERS if name.startswith(f"{p}_")), "")


def verified(claims: dict) -> bool:
    """A login's email can be trusted: made with `make login`, or verified by Google or Amazon."""
    if not federated(claims):
        return True
    return claims.get("email_verified") in (True, "true", "True") or provider(claims) in VERIFIED_EMAIL


def allowed(claims: dict, emails: set[str]) -> bool:
    """A login made in the pool, or a verified outside login whose email is one of `emails`."""
    return not federated(claims) or (verified(claims) and str(claims.get("email") or "").lower() in emails)


# ---------- Cognito ----------

async def discovery() -> dict:
    if not _discovery:
        url = f"{settings.oauth_issuer.rstrip('/')}/.well-known/openid-configuration"
        async with _http(5) as http:
            r = await http.get(url)
            r.raise_for_status()
            _discovery.update(r.json())
    return _discovery


async def userinfo(access_token: str) -> dict | None:
    """The email behind an access token (Cognito's userInfo endpoint), cached for a few minutes."""
    tag = hashlib.sha256(access_token.encode()).hexdigest()
    if (hit := _userinfo.get(tag)) and hit[0] > time.monotonic():
        return hit[1]
    try:
        endpoint = (await discovery())["userinfo_endpoint"]
        async with _http(5) as http:
            r = await http.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
        info = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, KeyError, ValueError) as e:
        log.warning("Couldn't reach Cognito's userInfo endpoint: %s", e)
        return None  # not cached: try again next time
    if isinstance(info, dict) and isinstance(info.get("identities"), str):
        info["identities"] = json.loads(info["identities"] or "[]")
    if len(_userinfo) > 256:
        _userinfo.clear()
    _userinfo[tag] = (time.monotonic() + USERINFO_CACHE_S, info)
    return info


async def token_login(token: str, claims: dict) -> tuple[str, bool] | None:
    """(email, outside login?) behind a verified access token, or None when its email can't be trusted."""
    info = await userinfo(token)
    if not info:
        return None
    outside = federated(claims)
    if outside and not verified({**info, "identities": info.get("identities") or [{"providerName": provider(claims) or "?"}]}):
        return None
    return str(info.get("email") or "").lower(), outside


def token_check(resolve: Resolve) -> Callable[[str, dict], Awaitable[bool]]:
    """For auth.JwtTokenVerifier: may this (already verified) access token be used? A pool login always;
    a Google or Amazon one when its email belongs to a household."""
    async def check(token: str, claims: dict) -> bool:
        if not federated(claims):
            return True
        login = await token_login(token, claims)
        ok = bool(login) and resolve(*login) is not None
        if not ok:
            log.info("Refused a %s login that isn't in any family", claims.get("username", "federated"))
        return ok
    return check


async def _id_claims(id_token: str) -> dict:
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(settings.oauth_jwks_url or f"{settings.oauth_issuer.rstrip('/')}/.well-known/jwks.json")
    key = await anyio.to_thread.run_sync(_jwks.get_signing_key_from_jwt, id_token)
    return jwt.decode(id_token, key.key, algorithms=["RS256"], audience=settings.login_client_id,
                      issuer=settings.oauth_issuer.rstrip("/"), options={"require": ["exp", "iss", "aud"]})


# ---------- the session ----------

def session(request: Request) -> dict | None:
    return unseal(request.cookies.get(COOKIE, "")) if enabled() else None


def same_site(request: Request) -> bool:
    """A browser sends Origin on every POST/PUT: it must be this server."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True
    ours = {f"{request.url.scheme}://{request.url.netloc}"}
    if settings.public_url:
        p = urlsplit(settings.public_url)
        ours.add(f"{p.scheme}://{p.netloc}")
    return origin in ours


def signed_in(request: Request) -> dict | None:
    """A valid session from this site, whether or not it belongs to a household yet."""
    s = session(request)
    return s if s and same_site(request) else None


def caregiver(request: Request, resolve: Resolve) -> dict | None:
    """The signed-in caregiver, with their "household", if the session is valid, the request is from this
    site and their email still belongs to a family (it may have been taken off the list since)."""
    s = signed_in(request)
    if not s:
        return None
    home = resolve(str(s.get("email") or ""), bool(s.get("fed")))
    return {**s, "household": home} if home else None


def login_url(path: str) -> str:
    return f"/auth/login?next={quote(path, safe='/')}"


def _next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") and "\\" not in value else "/dashboard"


def _secure() -> bool:
    return settings.base_url.startswith("https://")


def _redirect_uri() -> str:
    return f"{settings.base_url.rstrip('/')}/auth/callback"


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kinvesper sign-in</title><style>
:root {{ color-scheme: light dark; --bg: #f6f7f5; --ink: #1d2422; --muted: #5d6763; --brand: #0f766e; --card: #fff; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #111614; --ink: #e8ece9; --muted: #9aa5a0; --brand: #2dd4bf; --card: #1a201e; }} }}
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--ink);
  font: 16px/1.5 system-ui, sans-serif; padding: 16px; box-sizing: border-box; }}
main {{ background: var(--card); border-radius: 16px; padding: 28px; max-width: 420px; box-shadow: 0 10px 30px -12px rgba(0,0,0,.25); }}
h1 {{ font-size: 20px; margin: 0 0 8px; }} p {{ color: var(--muted); margin: 0 0 18px; }}
a.btn {{ display: inline-block; background: var(--brand); color: #fff; padding: 10px 18px; border-radius: 999px; text-decoration: none; font-weight: 600; }}
@media (prefers-color-scheme: dark) {{ a.btn {{ color: #062b27; }} }}
</style></head><body><main><h1>{title}</h1><p>{text}</p><a class="btn" href="{href}">{action}</a></main></body></html>"""


def page(title: str, text: str, href: str, action: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=title, text=text, href=href, action=action), status_code=status)


def signin_routes(resolve: Resolve) -> list[Route]:
    if not enabled():
        return []

    async def login(request: Request) -> Response:
        try:
            authorize = (await discovery())["authorization_endpoint"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("Couldn't load Cognito's sign-in settings: %s", e)
            return page("Sign-in is unavailable", "Kinvesper couldn't reach the sign-in service. Please try again in a minute.",
                        request.url.path + "?" + request.url.query, "Try again", status=503)
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        query = {"response_type": "code", "client_id": settings.login_client_id, "redirect_uri": _redirect_uri(),
                 "scope": "openid email profile", "state": state, "code_challenge": challenge,
                 "code_challenge_method": "S256"}
        if idp := request.query_params.get("provider"):  # straight there: ?provider=Google or ?provider=LoginWithAmazon
            query["identity_provider"] = idp
        r = RedirectResponse(f"{authorize}?{urlencode(query)}", status_code=302)
        flow = seal({"state": state, "verifier": verifier, "next": _next(request.query_params.get("next")),
                     "exp": int(time.time()) + FLOW_S})
        r.set_cookie(FLOW_COOKIE, flow, max_age=FLOW_S, path="/auth", httponly=True, secure=_secure(), samesite="lax")
        return r

    async def callback(request: Request) -> Response:
        q = request.query_params
        flow = unseal(request.cookies.get(FLOW_COOKIE, ""))
        again = login_url((flow or {}).get("next", "/dashboard"))
        if q.get("error"):
            return page("Sign-in didn't finish", q.get("error_description") or "The sign-in was cancelled.", again, "Sign in again", 400)
        if not flow or not hmac.compare_digest(str(flow.get("state", "")), q.get("state", "")) or not q.get("code"):
            return page("Sign-in expired", "That sign-in took too long or was opened in another browser.", again, "Sign in again", 400)
        try:
            async with _http(10) as http:
                r = await http.post((await discovery())["token_endpoint"], data={
                    "grant_type": "authorization_code", "client_id": settings.login_client_id, "code": q["code"],
                    "redirect_uri": _redirect_uri(), "code_verifier": flow["verifier"]})
            tokens = r.json()
            claims = await _id_claims(tokens["id_token"])
        except (httpx.HTTPError, KeyError, ValueError, jwt.PyJWTError) as e:
            log.warning("Sign-in failed at the token step: %s", e)
            return page("Sign-in failed", "Kinvesper couldn't confirm the sign-in. Please try again.", again, "Sign in again", 502)
        email, outside = str(claims.get("email") or "").lower(), federated(claims)
        home = resolve(email, outside) if verified(claims) else None
        if not home and not (outside and email and verified(claims) and settings.signup):
            log.info("Refused dashboard sign-in for an email that isn't in any family")
            return page("This account isn't on the list",
                        f"{claims.get('email') or 'This account'} isn't one of this family's logins. Ask the caregiver to "
                        "add your email in Settings, then sign in again.", "/auth/logout", "Use another account", 403)
        name = claims.get("name") or claims.get("given_name") or email.split("@")[0]
        cookie = seal({"sub": claims["sub"], "email": email, "name": name, "fed": outside,
                       "exp": int(time.time()) + SESSION_S})
        out = RedirectResponse(_next(flow.get("next")) if home else "/welcome", status_code=302)  # new: set up a family
        out.set_cookie(COOKIE, cookie, max_age=SESSION_S, path="/", httponly=True, secure=_secure(), samesite="lax")
        out.delete_cookie(FLOW_COOKIE, path="/auth")
        log.info("Caregiver signed in to the dashboard")
        return out

    async def logout(_: Request) -> Response:
        target = f"{settings.base_url.rstrip('/')}/auth/signed-out"
        try:
            host = urlsplit((await discovery())["authorization_endpoint"])
            url = f"{host.scheme}://{host.netloc}/logout?" + urlencode({"client_id": settings.login_client_id, "logout_uri": target})
        except (httpx.HTTPError, KeyError, ValueError):
            url = target  # at least this server forgets them
        out = RedirectResponse(url, status_code=302)
        out.delete_cookie(COOKIE, path="/")
        return out

    async def signed_out(_: Request) -> Response:
        return page("You're signed out", "You've signed out of the Kinvesper dashboard.", "/auth/login?next=/dashboard", "Sign in")

    async def me(request: Request) -> Response:
        s = caregiver(request, resolve)
        if not s:
            if signed_in(request):
                return JSONResponse({"error": "Set up your family first", "signup": "/welcome"}, status_code=401)
            return JSONResponse({"error": "Not signed in", "login": login_url("/dashboard")}, status_code=401)
        return JSONResponse({"email": s.get("email", ""), "name": s.get("name", "")})

    return [
        Route("/auth/login", login),
        Route("/auth/callback", callback),
        Route("/auth/logout", logout),
        Route("/auth/signed-out", signed_out),
        Route("/auth/me", me),
    ]
