"""Families: each household's care data kept apart, and which household a request is about.

One server serves many families. The first household (PATIENT_ID; Margaret in the demo) is the one
the ?key= links, the voice simulator's key and an unlinked Echo reach. Anyone else signs in to the
dashboard with Google or Amazon and starts a household of their own (/welcome, while SIGNUP is on, up
to MAX_HOUSEHOLDS), then lists the family members who may sign in too (Settings, "Family who can sign
in"). An email belongs to one household at most.

Each household's documents live in a partition of their own (DynamoDB pk = household id); the
directory partition maps sign-in emails, Telegram chats and Telegram link codes to households.
`HouseholdRouter` puts the household of each request in a context variable (config.household):

- a dashboard or simulator key: the first household
- a bearer token on /mcp: the household of its login (Alexa+ account linking); with SERVICE_TOKEN (the
  built-in voice agent) the one named in its X-Kinvesper-Household header
- a signed-in session: the household of its email

/alexa (the linked account), Telegram (the chat) and signed links (their h=) set it in their routes.
`Current` stands in for a CareService and forwards to the current household's, so routes and MCP
tools work unchanged. Background tasks keep the context they were started in.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import re
import time
from collections.abc import Iterator

from pydantic import BaseModel, Field

from . import signin
from .auth import JwtTokenVerifier
from .config import HOUSEHOLD_HEADER, household, household_id, now, settings
from .models import Profile, new_id
from .service import CareService, SettingsError
from .store import Store

log = logging.getLogger(__name__)

DIRECTORY = "_directory"
TOKEN_CACHE_S = 300
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMERGENCY = {"amazon.com": "911", "amazon.ca": "911", "amazon.co.uk": "999", "amazon.com.au": "000"}  # else 112


class Member(BaseModel):
    email: str
    household: str
    added_by: str = ""
    at: str = Field(default_factory=lambda: now().isoformat())


class Household(BaseModel):
    id: str = Field(default_factory=lambda: new_id("home"))
    owner: str
    created_at: str = Field(default_factory=lambda: now().isoformat())


@contextlib.contextmanager
def within(home: str) -> Iterator[None]:
    """Run a block for one household."""
    token = household.set(home)
    try:
        yield
    finally:
        household.reset(token)


class Households:
    def __init__(self, first: Store | CareService):
        service = first if isinstance(first, CareService) else CareService(first)
        self.store = service.store
        self.first = settings.patient_id
        self.directory = self.store.scoped(DIRECTORY)
        service.household, service.directory = self.first, self.directory
        self._services: dict[str, CareService] = {self.first: service}
        self._tokens: dict[str, tuple[float, str | None]] = {}
        self._verifier: JwtTokenVerifier | None = None
        self.current = Current(self)

    @classmethod
    def of(cls, service: CareService | Current | Households) -> Households:
        """The households behind what a route was given: a lone CareService (tests) is the first one."""
        if isinstance(service, Households):
            return service
        if isinstance(service, Current):
            return service.homes
        return cls(service)

    # ---------- households ----------

    def exists(self, home: str) -> bool:
        return home == self.first or bool(home and self.directory.get("household", home))

    def ids(self) -> list[str]:
        return [self.first, *(h["id"] for h in self.directory.list("household"))]

    def service(self, home: str | None = None) -> CareService:
        home = home or household_id()
        if home not in self._services:
            if not self.exists(home):
                raise LookupError(f"No household {home}")
            self._services[home] = CareService(self.store.scoped(home), home, self.directory)
        return self._services[home]

    def create(self, owner: str, owner_name: str, patient_name: str, preferred_name: str, relation: str) -> str:
        """A new household for a signed-in caregiver who doesn't belong to one yet."""
        owner = owner.strip().lower()
        if self.resolve(owner, True):
            raise SettingsError("You already belong to a family on Kinvesper.")
        if not patient_name.strip() or not owner_name.strip():
            raise SettingsError("Add your name and the name of the person you care for.")
        if len(self.ids()) - 1 >= settings.max_households:
            raise SettingsError("Kinvesper isn't taking new families right now. Please try again later.")
        home = Household(owner=owner)
        site = self._services[self.first].amazon_site()
        CareService(self.store.scoped(home.id), home.id, self.directory).store.save("profile", Profile(
            patient_name=patient_name.strip()[:80], preferred_name=(preferred_name.strip() or patient_name.split()[0])[:40],
            caregiver_name=owner_name.strip()[:60], caregiver_relation=(relation.strip() or "family")[:30],
            caregiver_email=owner, pharmacy_name="", emergency_number=EMERGENCY.get(site, "112"), checkin_time="",
        ), id="main")
        self.directory.put("household", home.id, home.model_dump())
        self.directory.put("member", owner, Member(email=owner, household=home.id, added_by=owner).model_dump())
        log.info("New household %s", home.id)
        return home.id

    # ---------- who signs in to which ----------

    def resolve(self, email: str, outside: bool) -> str | None:
        """The household of a login's email. A Google or Amazon login with none gets None; a login made in
        the user pool (`make login`) belongs to the first household unless a family lists it."""
        email = (email or "").strip().lower()
        if email and (m := self.directory.get("member", email)) and self.exists(m.get("household", "")):
            return m["household"]
        if email and email in signin.family_emails(self._services[self.first].profile()):
            return self.first
        return None if outside else self.first

    def members(self, home: str) -> list[Member]:
        found = [Member.model_validate(d) for d in self.directory.list("member") if d.get("household") == home]
        return sorted(found, key=lambda m: m.at)

    def add_member(self, home: str, email: str, by: str) -> Member:
        email = email.strip().lower()
        if not EMAIL.match(email):
            raise SettingsError("That email address doesn't look right.")
        if (found := self.resolve(email, True)) == home:
            raise SettingsError(f"{email} can already sign in.")
        if found:
            raise SettingsError(f"{email} already belongs to another family on Kinvesper.")
        member = Member(email=email, household=home, added_by=by)
        self.directory.put("member", email, member.model_dump())
        return member

    def remove_member(self, home: str, email: str, by: str) -> None:
        email = email.strip().lower()
        m = self.directory.get("member", email)
        if not m or m.get("household") != home:
            raise SettingsError(f"{email} isn't on this family's sign-in list.")
        if email == (by or "").lower():
            raise SettingsError("You can't remove yourself. Ask another family member to do it.")
        self.directory.delete("member", email)

    async def of_token(self, token: str) -> str | None:
        """The household of an OAuth access token's login (Alexa account linking, Alexa+), or None."""
        tag = hashlib.sha256(token.encode()).hexdigest()
        if (hit := self._tokens.get(tag)) and hit[0] > time.monotonic():
            return hit[1]
        if not settings.oauth_issuer:
            return None
        if self._verifier is None:
            self._verifier = JwtTokenVerifier(settings)
        access = await self._verifier.verify_token(token)
        claims = (access.claims or {}) if access else {}
        login = await signin.token_login(token, claims) if claims else None
        if login:
            home = self.resolve(*login)
        else:  # no email from Cognito: a pool login is the first household's, an outside one nobody's
            home = self.first if claims and not signin.federated(claims) else None
        if len(self._tokens) > 512:
            self._tokens.clear()
        self._tokens[tag] = (time.monotonic() + TOKEN_CACHE_S, home)
        return home

    # ---------- Telegram ----------

    def telegram_household(self, update: dict) -> str:
        """Which household a bot update is for: its /start link code, else its chat."""
        query = update.get("callback_query") or {}
        msg = query.get("message") or update.get("message") or {}
        text = "" if query else str(msg.get("text") or "").strip()
        code = text.partition(" ")[2].strip() if text.startswith("/start") else ""
        if code and (found := self.directory.get("tgcode", code)) and self.exists(found.get("household", "")):
            return found["household"]
        chat = str((msg.get("chat") or {}).get("id", ""))
        if chat and (found := self.directory.get("tgchat", chat)) and self.exists(found.get("household", "")):
            return found["household"]
        return self.first


class Current:
    """The current household's CareService, for code written for one household."""

    def __init__(self, homes: Households):
        self.homes = homes

    @property
    def store(self) -> Store:
        return CurrentStore(self.homes)  # resolved on every call, so it can be kept

    def __getattr__(self, name: str):
        return getattr(self.homes.service(), name)


class CurrentStore(Store):
    def __init__(self, homes: Households):
        self.homes = homes

    def _store(self) -> Store:
        return self.homes.service().store

    def put(self, kind, id, doc):
        self._store().put(kind, id, doc)

    def get(self, kind, id):
        return self._store().get(kind, id)

    def list(self, kind):
        return self._store().list(kind)

    def delete(self, kind, id):
        self._store().delete(kind, id)

    def clear(self):
        self._store().clear()

    def scoped(self, name):
        return self._store().scoped(name)


def _same(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


class HouseholdRouter:
    """ASGI middleware: sets the household each request is about (see the module docstring)."""

    def __init__(self, app, homes: Households):
        self.app, self.homes = app, homes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.requests import Request

        try:
            home = await self.pick(Request(scope))
        except Exception:
            log.exception("Couldn't tell which household a request is for")
            home = self.homes.first
        with within(home):
            await self.app(scope, receive, send)

    async def pick(self, request) -> str:
        h = request.headers
        if _same(h.get("x-dashboard-key", ""), settings.dashboard_key) or _same(h.get("x-simulator-key", ""), settings.simulator_key):
            return self.homes.first
        auth = h.get("authorization", "")
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip()
            if _same(token, settings.service_token):
                asked = h.get(HOUSEHOLD_HEADER, "")
                return asked if self.homes.exists(asked) else self.homes.first
            return await self.homes.of_token(token) or self.homes.first  # an unknown login is refused at /mcp anyway
        if s := signin.signed_in(request):
            return self.homes.resolve(str(s.get("email") or ""), bool(s.get("fed"))) or self.homes.first
        return self.homes.first
