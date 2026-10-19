"""OAuth 2.1 for the MCP endpoint: Kinvesper is a resource server, Amazon Cognito is the issuer.

Bearer tokens are JWTs checked against the issuer's JWKS: signature, expiry, issuer, audience
(`aud`, or `client_id` on Cognito access tokens; OAUTH_AUDIENCE may list several app clients,
e.g. MCP clients and Alexa+) and required scopes. The built-in voice simulator can also use a
static SERVICE_TOKEN. Auth is off when OAUTH_ISSUER is empty. `allow` can refuse a valid token: a Google
login that isn't one of the family's emails (signin.token_check).

Discovery: Cognito's own metadata doesn't advertise PKCE S256, which Alexa+ checks before it
deploys an add-on. So this server names itself as the authorization server and serves RFC 8414
metadata that points at Cognito's endpoints (login and tokens still come from Cognito).
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable

import anyio
import httpx
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Settings

log = logging.getLogger(__name__)

SERVICE_CLIENT_ID = "kinvesper-simulator"


class JwtTokenVerifier:
    def __init__(self, settings: Settings, jwks_client: jwt.PyJWKClient | None = None,
                 allow: Callable[[str, dict], Awaitable[bool]] | None = None):
        self.issuer = settings.oauth_issuer.rstrip("/")
        self.audiences = set(settings.oauth_audience.replace(",", " ").split())
        self.scopes = settings.oauth_scope_list
        self.service_token = settings.service_token
        self.allow = allow
        self._jwks = jwks_client or jwt.PyJWKClient(settings.oauth_jwks_url or f"{self.issuer}/.well-known/jwks.json")

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.service_token and hmac.compare_digest(token, self.service_token):
            return AccessToken(token=token, client_id=SERVICE_CLIENT_ID, scopes=self.scopes)
        try:
            # PyJWKClient fetches (and caches) keys with blocking urllib
            key = await anyio.to_thread.run_sync(self._jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], issuer=self.issuer,
                options={"require": ["exp", "iss"], "verify_aud": False},
            )
        except jwt.PyJWTError as e:
            log.info("Rejected bearer token: %s", e)
            return None

        audiences = claims.get("aud") or []
        if isinstance(audiences, str):
            audiences = [audiences]
        client_id = claims.get("client_id") or claims.get("azp") or ""
        if self.audiences and not self.audiences & {*audiences, client_id}:
            log.info("Rejected bearer token: wrong audience")
            return None
        if self.allow and not await self.allow(token, claims):
            return None
        scopes = claims.get("scope", "").split()
        return AccessToken(
            token=token, client_id=client_id or claims.get("sub", ""), scopes=scopes,
            expires_at=claims["exp"], subject=claims.get("sub"), claims=claims,
        )


def server_id(settings: Settings) -> str:
    """This server as an authorization server identifier, spelled the way the MCP SDK publishes it."""
    return str(AnyHttpUrl(settings.base_url))


def authorization_server_metadata(oidc: dict, scopes: list[str], issuer: str | None = None) -> dict:
    """RFC 8414 metadata built from the issuer's OpenID configuration.

    Cognito only publishes /.well-known/openid-configuration, without PKCE methods, so Kinvesper
    serves this copy under its own identifier (`issuer`) with Cognito's endpoints.
    """
    keys = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri", "revocation_endpoint", "userinfo_endpoint")
    meta = {k: oidc[k] for k in keys if oidc.get(k)}
    if issuer:
        meta["issuer"] = issuer
    meta["scopes_supported"] = sorted({*scopes, *oidc.get("scopes_supported", [])})
    meta["response_types_supported"] = ["code"]
    meta["grant_types_supported"] = ["authorization_code", "refresh_token"]
    meta["code_challenge_methods_supported"] = ["S256"]  # Cognito enforces PKCE but doesn't advertise it
    meta["token_endpoint_auth_methods_supported"] = oidc.get(
        "token_endpoint_auth_methods_supported", ["none", "client_secret_basic", "client_secret_post"])
    return meta


def metadata_routes(settings: Settings) -> list[Route]:
    """OAuth discovery documents at the root of the server.

    /.well-known/oauth-authorization-server is fetched once from Cognito and cached.
    /.well-known/oauth-protected-resource repeats the SDK's /.well-known/oauth-protected-resource/mcp,
    because Alexa+ reads the root path.
    """
    if not settings.oauth_issuer:
        return []
    cached: dict = {}
    resource = {
        "resource": f"{settings.base_url}/mcp",
        "authorization_servers": [server_id(settings)],
        "scopes_supported": settings.oauth_scope_list,
        "bearer_methods_supported": ["header"],
    }

    async def protected_resource(_request):
        return JSONResponse(resource, headers={"Cache-Control": "public, max-age=3600"})

    async def metadata(_request):
        if not cached:
            url = f"{settings.oauth_issuer.rstrip('/')}/.well-known/openid-configuration"
            try:
                async with httpx.AsyncClient(timeout=5) as http:
                    r = await http.get(url)
                    r.raise_for_status()
                    cached.update(authorization_server_metadata(r.json(), settings.oauth_scope_list, server_id(settings)))
            except (httpx.HTTPError, ValueError) as e:
                log.warning("Could not load OAuth metadata from %s: %s", url, e)
                return JSONResponse({"error": "temporarily_unavailable"}, status_code=503)
        return JSONResponse(cached, headers={"Cache-Control": "public, max-age=3600"})

    return [
        Route("/.well-known/oauth-authorization-server", metadata),
        Route("/.well-known/oauth-protected-resource", protected_resource),
    ]


def auth_settings(settings: Settings) -> AuthSettings | None:
    if not settings.oauth_issuer:
        return None
    return AuthSettings(
        issuer_url=server_id(settings),  # see the module docstring: metadata for Cognito, served here
        resource_server_url=f"{settings.base_url}/mcp",
        required_scopes=settings.oauth_scope_list or None,
        validate_token_resource=False,  # JwtTokenVerifier checks the audience itself
    )
