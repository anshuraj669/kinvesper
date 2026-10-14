"""Caregiver dashboard: the page and its JSON API.

    GET  /dashboard                  the dashboard page
    GET  /dashboard/settings         the settings page: the person, the caregiver's contact, the medicines
    GET  /api/dashboard              everything the page shows, in one call
    GET  /api/settings               the household's settings
    PUT  /api/settings               save them ({"profile": {...}, "medications": [...], "contacts": [...]})
    POST /api/settings/test-alert    send a test alert to the caregiver's phone and email
    POST /api/reports/weekly         email the caregiver this week's report now
    POST /api/alerts/{id}/ack        acknowledge an alert
    POST /api/reminders/{id}/cancel  cancel a reminder or alarm (the Echo drops it the next time it's used)
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
from .service import CareService, SettingsError

OFF = "The caregiver dashboard is off: with OAuth on it needs DASHBOARD_KEY."


def dashboard_routes(service: CareService) -> list[Route]:
    page = files("kinvesper").joinpath("static/dashboard.html").read_text()
    settings_page = files("kinvesper").joinpath("static/settings.html").read_text()

    def guard(request: Request) -> Response | None:
        if not settings.oauth_issuer:
            return None
        if not settings.dashboard_key:
            return JSONResponse({"error": OFF}, status_code=404)
        if not hmac.compare_digest(request.headers.get("x-dashboard-key", ""), settings.dashboard_key):
            return JSONResponse({"error": "Missing or wrong dashboard key"}, status_code=403)
        return None

    def html(body: str) -> Response:
        if settings.oauth_issuer and not settings.dashboard_key:
            return HTMLResponse(f"<p>{OFF}</p>", status_code=404)
        return HTMLResponse(body)

    async def dashboard_page(_: Request) -> Response:
        return html(page)

    async def settings_html(_: Request) -> Response:
        return html(settings_page)

    async def household(request: Request) -> Response:
        if denied := guard(request):
            return denied
        if request.method == "GET":
            return JSONResponse(await service.settings_view())
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("medications"), list):
                raise SettingsError("Send a profile and a list of medicines.")
            trusted = body.get("contacts")
            if trusted is not None and not isinstance(trusted, list):
                raise SettingsError("Send the trusted contacts as a list.")
            return JSONResponse(await service.update_settings(body.get("profile") or {}, body["medications"], trusted))
        except (SettingsError, ValueError) as e:
            return JSONResponse({"error": str(e) if isinstance(e, SettingsError) else "Invalid request"}, status_code=400)

    async def weekly_report(request: Request) -> Response:
        if denied := guard(request):
            return denied
        alert = await service.send_weekly_report()
        return JSONResponse({"sent": alert.notified, "summary": alert.detail})

    async def test_alert(request: Request) -> Response:
        if denied := guard(request):
            return denied
        alert = await service.send_test_alert()
        return JSONResponse({"sent": alert.notified, **await service.settings_view()})

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

    async def cancel_reminder(request: Request) -> Response:
        if denied := guard(request):
            return denied
        result = service.cancel_reminder(reminder_id=request.path_params["reminder_id"])
        if result.reminder is None:
            return JSONResponse({"error": "Unknown reminder"}, status_code=404)
        return JSONResponse(result.reminder.model_dump())

    async def checks(request: Request) -> Response:
        if denied := guard(request):
            return denied
        return JSONResponse({"raised": await check_once(service)})

    return [
        Route("/dashboard", dashboard_page),
        Route("/dashboard/settings", settings_html),
        Route("/api/dashboard", data),
        Route("/api/settings", household, methods=["GET", "PUT"]),
        Route("/api/settings/test-alert", test_alert, methods=["POST"]),
        Route("/api/reports/weekly", weekly_report, methods=["POST"]),
        Route("/api/alerts/{alert_id}/ack", ack, methods=["POST"]),
        Route("/api/checks", checks, methods=["POST"]),
        Route("/api/reminders/{reminder_id}/cancel", cancel_reminder, methods=["POST"]),
    ]
