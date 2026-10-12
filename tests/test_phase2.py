"""Phase 2: Tasks, elicitation, icons and OAuth, end to end over Streamable HTTP."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, ElicitResult

from kinvesper.auth import SERVICE_CLIENT_ID, JwtTokenVerifier, authorization_server_metadata
from kinvesper.config import Settings

from .conftest import SERVICE_TOKEN, _run_server

# ---------- tasks ----------


async def test_refill_runs_as_task(server_url):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert tools["request_pharmacy_refill"].execution.taskSupport == "optional"
            assert tools["get_medication_schedule"].execution.taskSupport == "forbidden"

            created = await session.experimental.call_tool_as_task("request_pharmacy_refill", {"medication": "cholesterol"})
            assert created.task.status == "working"

            seen = []
            async for status in session.experimental.poll_task(created.task.taskId):
                seen.append(status.statusMessage)
            assert status.status == "completed"
            assert len(set(seen)) >= 2  # progress messages changed while the task was working

            result = await session.experimental.get_task_result(created.task.taskId, CallToolResult)
            assert result.structuredContent["refill"]["status"] == "ready"
            assert "Atorvastatin" in result.structuredContent["message"]

            refills = json.loads((await session.read_resource("care://refills")).contents[0].text)
            assert refills[0]["status"] == "ready"


async def test_refill_without_task_and_unknown_med(server_url):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            plain = await session.call_tool("request_pharmacy_refill", {"medication": "thyroid", "fulfillment": "delivery"})
            assert plain.structuredContent["refill"]["status"] == "ready"
            assert "deliver" in plain.structuredContent["message"]

            missing = await session.call_tool("request_pharmacy_refill", {"medication": "aspirin"})
            assert missing.structuredContent["refill"] is None


async def test_task_call_rejected_for_plain_tool(server_url):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            with pytest.raises(Exception):
                await session.experimental.call_tool_as_task("get_medication_schedule", {})


async def test_duplicate_open_refill_is_reused(service):
    async def interrupt(refill):
        if refill.status == "verifying":
            raise RuntimeError("stop mid-way")

    with pytest.raises(RuntimeError):
        await service.request_refill("Metformin", on_status=interrupt)
    assert service.refills()[0].status == "failed"  # an interrupted refill doesn't block the next one

    first = await service.request_refill("Metformin")
    assert first.refill.status == "ready"
    assert any(a.kind == "refill" for a in service.alerts())


# ---------- elicitation ----------


def _elicit_answer(send: bool | None):
    async def callback(_ctx, params):
        assert "Priya" in params.message
        if send is None:
            return ElicitResult(action="decline")
        return ElicitResult(action="accept", content={"send": send})
    return callback


@pytest.mark.parametrize(("answer", "sent"), [(True, True), (False, False), (None, False)])
async def test_notify_caregiver_asks_first(server_url, answer, sent):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write, elicitation_callback=_elicit_answer(answer)) as session:
            await session.initialize()
            result = await session.call_tool("notify_caregiver", {"message": "I'm going to the doctor"})
            assert result.structuredContent["sent"] is sent
            assert result.structuredContent["confirmed_by_user"] is sent


async def test_notify_caregiver_without_elicitation_support(server_url):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:  # no callback, so no elicitation capability
            await session.initialize()
            result = await session.call_tool("notify_caregiver", {"message": "Running late"})
            assert result.structuredContent["sent"] is True
            assert result.structuredContent["confirmed_by_user"] is None


# ---------- icons ----------


async def test_everything_has_icons(server_url):
    async with streamable_http_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.icons[0].src.startswith("data:image/svg+xml;base64,")
            assert all(t.icons for t in (await session.list_tools()).tools)
            assert all(r.icons for r in (await session.list_resources()).resources)
            assert all(p.icons for p in (await session.list_prompts()).prompts)


# ---------- OAuth ----------


def test_unauthenticated_request_is_challenged(auth_server):
    r = httpx.post(f"{auth_server}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]

    bad = httpx.post(f"{auth_server}/mcp", headers={"Authorization": "Bearer not-a-jwt"}, json={})
    assert bad.status_code == 401

    meta = httpx.get(f"{auth_server}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{auth_server}/mcp"
    # This server serves the authorization server metadata (Cognito's lacks PKCE S256)
    assert meta["authorization_servers"] == [f"{auth_server}/"]
    assert "kinvesper/mcp" in meta["scopes_supported"]

    # Alexa+ reads the root path; it must say the same thing
    root = httpx.get(f"{auth_server}/.well-known/oauth-protected-resource").json()
    assert root["resource"] == meta["resource"]
    assert root["authorization_servers"] == meta["authorization_servers"]
    assert root["scopes_supported"] == meta["scopes_supported"]


async def test_service_token_is_accepted(auth_server):
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    async with httpx.AsyncClient(headers=headers) as http:
        async with streamable_http_client(f"{auth_server}/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert (await session.list_tools()).tools


class _StaticJwks:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, _token):
        return self


ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"


@pytest.fixture
def signer():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(oauth_issuer=ISSUER, oauth_audience="app-client-1", oauth_scopes="kinvesper/mcp", service_token="")
    verifier = JwtTokenVerifier(settings, jwks_client=_StaticJwks(private.public_key()))

    def sign(**claims):
        base = {"iss": ISSUER, "exp": int(time.time()) + 300, "sub": "user-1", "client_id": "app-client-1",
                "scope": "kinvesper/mcp", "token_use": "access"}
        return jwt.encode({**base, **claims}, private, algorithm="RS256")

    return verifier, sign


async def test_jwt_verifier(signer):
    verifier, sign = signer
    token = await verifier.verify_token(sign())
    assert token.client_id == "app-client-1"
    assert token.scopes == ["kinvesper/mcp"]

    assert await verifier.verify_token(sign(exp=int(time.time()) - 10)) is None  # expired
    assert await verifier.verify_token(sign(iss="https://evil.example")) is None  # wrong issuer
    assert await verifier.verify_token(sign(client_id="other-client")) is None  # wrong audience


async def test_jwt_verifier_accepts_each_listed_client():
    """MCP clients and the Alexa+ add-on have their own Cognito app clients."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(oauth_issuer=ISSUER, oauth_audience="mcp-client,alexa-client", oauth_scopes="kinvesper/mcp",
                        service_token="")
    verifier = JwtTokenVerifier(settings, jwks_client=_StaticJwks(private.public_key()))

    def token(client_id):
        claims = {"iss": ISSUER, "exp": int(time.time()) + 300, "client_id": client_id, "scope": "kinvesper/mcp"}
        return jwt.encode(claims, private, algorithm="RS256")

    assert (await verifier.verify_token(token("mcp-client"))).client_id == "mcp-client"
    assert (await verifier.verify_token(token("alexa-client"))).client_id == "alexa-client"
    assert await verifier.verify_token(token("someone-else")) is None


async def test_service_token_verifier():
    settings = Settings(oauth_issuer=ISSUER, oauth_scopes="kinvesper/mcp", service_token="s3cret")
    verifier = JwtTokenVerifier(settings, jwks_client=_StaticJwks(None))
    token = await verifier.verify_token("s3cret")
    assert token.client_id == SERVICE_CLIENT_ID
    assert token.scopes == ["kinvesper/mcp"]


def test_authorization_server_metadata_from_cognito():
    cognito = {
        "issuer": ISSUER,
        "authorization_endpoint": "https://kinvesper-1.auth.us-east-1.amazoncognito.com/oauth2/authorize",
        "token_endpoint": "https://kinvesper-1.auth.us-east-1.amazoncognito.com/oauth2/token",
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        "scopes_supported": ["openid", "email"],
        "response_types_supported": ["code", "token"],
    }
    meta = authorization_server_metadata(cognito, ["kinvesper/mcp"])
    assert meta["issuer"] == ISSUER
    assert authorization_server_metadata(cognito, [], "https://kinvesper.example/")["issuer"] == "https://kinvesper.example/"
    assert meta["authorization_endpoint"].endswith("/oauth2/authorize")
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["response_types_supported"] == ["code"]
    assert {"kinvesper/mcp", "openid"} <= set(meta["scopes_supported"])


def test_authorization_server_metadata_route(auth_server, server_url):
    # The test issuer doesn't exist, so the route answers but can't load Cognito's document.
    assert httpx.get(f"{auth_server}/.well-known/oauth-authorization-server").status_code == 503
    base = server_url.removesuffix("/mcp")
    assert httpx.get(f"{base}/.well-known/oauth-authorization-server").status_code == 404  # auth off


# ---------- tunnels ----------


def test_public_url_host_is_allowed(tmp_path):
    proc, base = _run_server(tmp_path, PUBLIC_URL="https://kinvesper-demo.trycloudflare.com")
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"Accept": "application/json, text/event-stream"}
    try:
        tunnel = httpx.post(f"{base}/mcp", json=init, headers={**headers, "Host": "kinvesper-demo.trycloudflare.com"})
        other = httpx.post(f"{base}/mcp", json=init, headers={**headers, "Host": "evil.example"})
        assert tunnel.status_code == 200
        assert other.status_code == 421  # DNS-rebinding protection still on for anything else
    finally:
        proc.terminate()
        proc.wait(timeout=10)
