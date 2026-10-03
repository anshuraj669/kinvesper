import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from kinvesper.seed import seed
from kinvesper.service import CareService
from kinvesper.store import JsonStore


@pytest.fixture
def service(tmp_path) -> CareService:
    store = JsonStore(str(tmp_path / "state.json"))
    seed(store)
    return CareService(store)


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
