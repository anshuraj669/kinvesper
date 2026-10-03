"""Caregiver dashboard: the page and its JSON API.

    GET  /dashboard                  the dashboard page
    GET  /api/dashboard              everything the page shows, in one call
    POST /api/alerts/{id}/ack        acknowledge an alert
    POST /api/checks                 run the proactive checks now (demo button)

Health data, so with OAuth on the dashboard needs DASHBOARD_KEY (open /dashboard?key=...) and is
off without it. With OAuth off (local dev) it is open.
"""

from __future__ import annotations

import hmac
from importlib.resources import files

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .config import settings
from .scheduler import check_once
from .service import CareService

OFF = "The caregiver dashboard is off: with OAuth on it needs DASHBOARD_KEY."


def dashboard_routes(service: CareService) -> list[Route]:
    page = files("kinvesper").joinpath("static/dashboard.html").read_text()

    def guard(request: Request) -> Response | None:
        if not settings.oauth_issuer:
            return None
        if not settings.dashboard_key:
            return JSONResponse({"error": OFF}, status_code=404)
        if not hmac.compare_digest(request.headers.get("x-dashboard-key", ""), settings.dashboard_key):
            return JSONResponse({"error": "Missing or wrong dashboard key"}, status_code=403)
        return None

    async def dashboard_page(_: Request) -> Response:
        if settings.oauth_issuer and not settings.dashboard_key:
            return HTMLResponse(f"<p>{OFF}</p>", status_code=404)
        return HTMLResponse(page)

    async def data(request: Request) -> Response:
        if denied := guard(request):
            return denied
        return JSONResponse(service.dashboard())

    async def ack(request: Request) -> Response:
        if denied := guard(request):
            return denied
        alert = service.acknowledge(request.path_params["alert_id"])
        if alert is None:
            return JSONResponse({"error": "Unknown alert"}, status_code=404)
        return JSONResponse(alert.model_dump())

    async def checks(request: Request) -> Response:
        if denied := guard(request):
            return denied
        return JSONResponse({"raised": await check_once(service)})

    return [
        Route("/dashboard", dashboard_page),
        Route("/api/dashboard", data),
        Route("/api/alerts/{alert_id}/ack", ack, methods=["POST"]),
        Route("/api/checks", checks, methods=["POST"]),
    ]
