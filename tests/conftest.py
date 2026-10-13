import contextlib
import dataclasses
import os
import socket
import subprocess
import sys
import time

import json

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from kinvesper import mcp_server
from kinvesper.mcp_server import INSTRUCTIONS, build_server
from kinvesper.seed import seed
from kinvesper.service import CareService
from kinvesper.store import JsonStore


@pytest.fixture
def service(tmp_path) -> CareService:
    store = JsonStore(str(tmp_path / "state.json"))
    seed(store)
    return CareService(store)


@pytest.fixture
def connect(service, monkeypatch):
    """Voice sessions talk to the real MCP server, in memory."""
    monkeypatch.setattr(mcp_server, "settings", dataclasses.replace(mcp_server.settings, refill_step_seconds=0.02))
    server = build_server(service)

    @contextlib.asynccontextmanager
    async def _connect(elicit):
        async with create_connected_server_and_client_session(server, elicitation_callback=elicit) as session:
            yield session, INSTRUCTIONS

    return lambda elicit: _connect(elicit)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(data_dir, port=None, **extra_env):
    port = port or _free_port()
    env = {
        **os.environ,
        "PORT": str(port),
        "DATA_FILE": str(data_dir / "state.json"),
        "LLM_PROVIDER": "none",
        "STT_PROVIDER": "off",  # no Whisper download or AWS calls in tests
        "TTS_PROVIDER": "browser",  # no AWS calls in tests
        "REFILL_STEP_SECONDS": "0.3",  # long enough for a client polling every 0.5 s to see progress
        **extra_env,
    }
    proc = subprocess.Popen([sys.executable, "-m", "kinvesper.app"], env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/health").status_code == 200:
                return proc, base
        except httpx.TransportError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("server did not start")


@pytest.fixture
def fresh_server(tmp_path):
    """A server of its own, for tests that change the household."""
    proc, base = _run_server(tmp_path)
    try:
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def server_url(tmp_path_factory):
    proc, base = _run_server(tmp_path_factory.mktemp("data"))
    try:
        yield f"{base}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


SERVICE_TOKEN = "test-service-token"


@pytest.fixture(scope="session")
def auth_server(tmp_path_factory):
    """A server with OAuth on. Only the static service token is usable offline."""
    port = _free_port()
    proc, base = _run_server(
        tmp_path_factory.mktemp("auth"),
        port,
        PUBLIC_URL=f"http://127.0.0.1:{port}",
        OAUTH_ISSUER="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST",
        OAUTH_SCOPES="kinvesper/mcp",
        SERVICE_TOKEN=SERVICE_TOKEN,
    )
    try:
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------- Alexa Reminders API ----------

SYSTEM = {"apiEndpoint": "https://api.amazonalexa.com", "apiAccessToken": "token-123",
          "user": {"userId": "amzn1.ask.account.TEST"}}


class FakeRemindersApi:
    """The Alexa Reminders API, in memory."""

    def __init__(self, allowed=True):
        self.allowed = allowed
        self.reminders: dict[str, dict] = {}
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        assert request.headers["authorization"] == "Bearer token-123"
        if not self.allowed:
            return httpx.Response(403, json={"code": "UNAUTHORIZED"})
        if request.method == "POST":
            token = f"alert-{len(self.reminders) + len(self.calls)}"
            self.reminders[token] = json.loads(request.content)
            return httpx.Response(201, json={"alertToken": token})
        if request.method == "DELETE":
            self.reminders.pop(request.url.path.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        return httpx.Response(405)



@pytest.fixture
def reminders_api():
    return FakeRemindersApi()


@pytest.fixture
def alexa_system():
    return SYSTEM
