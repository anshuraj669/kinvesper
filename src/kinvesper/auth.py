"""OAuth 2.1 for the MCP endpoint: Kinvesper is a resource server, Amazon Cognito is the issuer.

Bearer tokens are JWTs checked against the issuer's JWKS: signature, expiry, issuer, audience
(`aud`, or `client_id` on Cognito access tokens) and required scopes. The built-in voice
simulator can also use a static SERVICE_TOKEN. Auth is off when OAUTH_ISSUER is empty.
"""

from __future__ import annotations

import hmac
import logging

import anyio
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from .config import Settings

log = logging.getLogger(__name__)

SERVICE_CLIENT_ID = "kinvesper-simulator"


class JwtTokenVerifier:
    def __init__(self, settings: Settings, jwks_client: jwt.PyJWKClient | None = None):
        self.issuer = settings.oauth_issuer.rstrip("/")
        self.audience = settings.oauth_audience
        self.scopes = settings.oauth_scope_list
        self.service_token = settings.service_token
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
        if self.audience and self.audience not in audiences and self.audience != client_id:
            log.info("Rejected bearer token: wrong audience")
            return None
        scopes = claims.get("scope", "").split()
        return AccessToken(
            token=token, client_id=client_id or claims.get("sub", ""), scopes=scopes,
            expires_at=claims["exp"], subject=claims.get("sub"), claims=claims,
        )


def auth_settings(settings: Settings) -> AuthSettings | None:
    if not settings.oauth_issuer:
        return None
    return AuthSettings(
        issuer_url=settings.oauth_issuer,
        resource_server_url=f"{settings.base_url}/mcp",
        required_scopes=settings.oauth_scope_list or None,
        validate_token_resource=False,  # JwtTokenVerifier checks the audience itself
    )
