"""HTTP entry point: MCP at /mcp, voice simulator at /voice, caregiver dashboard at /dashboard,
and the public pages the Alexa+ store listing links to (/privacy, /terms). Every request is about one
household (households.py), set by HouseholdRouter before the routes run."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import secrets

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import scheduler, signin
from .alexa import alexa_routes
from .auth import metadata_routes
from .config import settings
from .dashboard import dashboard_routes
from .households import HouseholdRouter, Households
from .listing import listing_routes
from .mcp_server import build_server
from .seed import seed
from .service import CareService
from .store import make_store
from .voice import VoiceSessions
from .web import voice_routes

log = logging.getLogger("kinvesper")


def create_app(service: CareService | None = None, voice: VoiceSessions | None = None) -> Starlette:
    if service is None:
        store = make_store()
        if not store.get("profile", "main"):
            log.info("Empty store: seeding demo household")
            seed(store)
        service = CareService(store)
    homes = Households(service)

    mcp = build_server(homes.current)
    mcp_app = mcp.streamable_http_app()
    voice = voice or VoiceSessions()

    async def health(_):
        return JSONResponse({"ok": True, "protocol": "2025-11-25", "store": settings.store, "llm": settings.llm_provider})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        checks = asyncio.create_task(scheduler.run_forever(homes))
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                checks.cancel()
                await voice.close_all()

    routes = [
        Route("/health", health), *metadata_routes(settings),
        *signin.signin_routes(homes.resolve), *voice_routes(voice, service, homes), *dashboard_routes(service, homes),
        *listing_routes(), *alexa_routes(voice, homes.current),
        Mount("/", app=mcp_app),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(HouseholdRouter, homes=homes)])
    app.state.service = service
    app.state.homes = homes
    app.state.mcp = mcp
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Kinvesper MCP server")
    parser.add_argument("--seed", action="store_true", help="reset the store to the demo household and exit")
    parser.add_argument("--check", action="store_true", help="run the proactive checks once and exit (for a cron/EventBridge trigger)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines include the Telegram bot token

    if args.seed:
        seed(make_store())
        print("Seeded demo household.")
        return
    if args.check:
        raised = asyncio.run(scheduler.check_all(Households(make_store())))
        print(f"Checks done: {raised} new alert(s).")
        return
    log.info("Voice simulator: http://127.0.0.1:%s/voice", settings.port)
    if signin.enabled():
        log.info("Caregiver dashboard (sign in): %s/dashboard", settings.base_url.rstrip("/"))
    if not settings.dashboard_key:  # never an open dashboard: a private link for this run
        object.__setattr__(settings, "dashboard_key", secrets.token_urlsafe(18))
        log.info("Caregiver dashboard (private link): %s/dashboard?key=%s", settings.base_url.rstrip("/"), settings.dashboard_key)
    elif not signin.enabled():
        log.info("Caregiver dashboard: %s/dashboard?key=<DASHBOARD_KEY>", settings.base_url.rstrip("/"))
    uvicorn.run(create_app(), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
