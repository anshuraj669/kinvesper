"""Caregiver dashboard: the page and its JSON API.

    GET  /dashboard                  the dashboard page
    GET  /dashboard/settings         the settings page: the person, the caregiver's contact, the medicines
    GET  /dashboard/doctor           a printable summary for the next doctor's visit
    GET  /api/dashboard              everything the page shows, in one call
    GET  /api/settings               the household's settings
    PUT  /api/settings               save them ({"profile": {...}, "medications": [...], "contacts": [...]})
    POST /api/settings/test-alert    send a test alert to the caregiver's phone and email
    POST /api/reports/weekly         email the caregiver this week's report now
    POST /api/alerts/{id}/ack        acknowledge an alert
    POST /api/reminders/{id}/cancel  cancel a reminder or alarm (the Echo drops it the next time it's used)
    POST /api/checks                 run the proactive checks now (demo button)
    POST /api/ask                    ask Kinvesper about the person ({"question", "history": [{"q", "a"}]})
    POST /api/messages               send the person a message, read out on the Echo ({"text"})
    GET  /api/reports/doctor         the doctor visit summary (?days=30)
    POST /api/medications/scan       a photo of a prescription (the image as the body) -> medicines to review
    POST /api/medications/interactions  check medicines just added ({"medicines": [names]}) for interactions
    POST /api/calls/{id}/ack         Twilio, when the caregiver presses 1 on an alert call (signed by Twilio)

Health data, so with OAuth on the dashboard needs DASHBOARD_KEY (open /dashboard?key=...) and is
off without it. With OAuth off (local dev) it is open.
"""

from __future__ import annotations

import hmac
import logging
from importlib.resources import files

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import media, notify, telegram
from .config import settings
from .models import ScanResult
from .scheduler import check_once
from .service import CareService, SettingsError

log = logging.getLogger(__name__)

OFF = "The caregiver dashboard is off: with OAuth on it needs DASHBOARD_KEY."
MAX_PHOTO_BYTES = 5 * 1024 * 1024  # Claude's limit for one image


def dashboard_routes(service: CareService) -> list[Route]:
    page = files("kinvesper").joinpath("static/dashboard.html").read_text()
    settings_page = files("kinvesper").joinpath("static/settings.html").read_text()
    doctor_page = files("kinvesper").joinpath("static/doctor.html").read_text()

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

    async def doctor_html(_: Request) -> Response:
        return html(doctor_page)

    async def json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    async def ask(request: Request) -> Response:
        if denied := guard(request):
            return denied
        body = await json_body(request)
        question = str(body.get("question") or "").strip()
        if not question:
            return JSONResponse({"error": "Ask a question first"}, status_code=400)
        history = body.get("history") if isinstance(body.get("history"), list) else []
        return JSONResponse({"answer": await service.ask(question, [h for h in history if isinstance(h, dict)])})

    async def message(request: Request) -> Response:
        if denied := guard(request):
            return denied
        try:
            sent = service.send_family_message(str((await json_body(request)).get("text") or ""))
        except SettingsError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(sent.model_dump())

    async def doctor_report(request: Request) -> Response:
        if denied := guard(request):
            return denied
        try:
            days = max(7, min(90, int(request.query_params.get("days", "30"))))
        except ValueError:
            days = 30
        return JSONResponse(await service.doctor_report(days))

    async def scan(request: Request) -> Response:
        if denied := guard(request):
            return denied
        image = await request.body()
        if not image:
            return JSONResponse({"error": "No photo received"}, status_code=400)
        if len(image) > MAX_PHOTO_BYTES:
            return JSONResponse({"error": "That photo is too large. Please use one under 5 MB."}, status_code=413)
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        try:
            result: ScanResult = await service.scan_prescription(image, media_type)
        except SettingsError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        return JSONResponse(result.model_dump())

    async def call_ack(request: Request) -> Response:
        """The caregiver pressed a key on an alert call. Twilio's signature stands in for the dashboard key."""
        form = {k: str(v) for k, v in (await request.form()).items()}
        url = f"{settings.public_url.rstrip('/')}{request.url.path}"
        if not notify.twilio_signature_ok(url, form, request.headers.get("x-twilio-signature", "")):
            return Response(status_code=403)
        alert = service.acknowledge(request.path_params["alert_id"]) if form.get("Digits") == "1" else None
        p = service.profile()
        said = ("Thank you. Alert calls are working, and pressing 1 marks an alert as handled. Goodbye." if alert and alert.kind == "test"
                else f"Thank you. The alert is marked as handled. Please check on {p.preferred_name}." if alert
                else "Okay. The alert is still open in the Kinvesper dashboard. Goodbye.")
        return Response(f'<Response><Say voice="{notify.VOICE}">{notify.escape(said)}</Say></Response>',
                        media_type="application/xml")

    async def telegram_webhook(request: Request) -> Response:
        """Updates from the Kinvesper bot. Telegram's secret header stands in for the dashboard key."""
        if not telegram.secret_ok(request.headers.get("x-telegram-bot-api-secret-token", "")):
            return Response(status_code=403)
        try:
            await service.telegram_update(await json_body(request))
        except Exception:  # answer 200 anyway, or Telegram resends the same update again and again
            log.exception("Telegram update failed")
        return Response(status_code=200)

    async def telegram_link(request: Request) -> Response:
        if denied := guard(request):
            return denied
        body = await json_body(request)
        try:
            if request.url.path.endswith("/unlink"):
                return JSONResponse(await service.telegram_unlink(str(body.get("audience") or "caregiver")))
            return JSONResponse(await service.telegram_link(str(body.get("audience") or "caregiver")))
        except SettingsError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    async def family_media(request: Request) -> Response:
        """A voice note or photo from the family's Telegram chat. The signed link stands in for the dashboard
        key, because the Echo fetches it with no headers of ours."""
        mid, q = request.path_params["message_id"], request.query_params
        if not media.signature_ok(mid, q.get("e", ""), q.get("s", "")):
            return Response(status_code=403)
        found = await service.family_media(mid)
        if found is None:
            return Response(status_code=404)
        data, media_type = found
        return Response(data, media_type=media_type, headers={"Cache-Control": "private, max-age=3600"})

    async def check_interactions(request: Request) -> Response:
        if denied := guard(request):
            return denied
        names = (await json_body(request)).get("medicines")
        if not isinstance(names, list) or not names:
            return JSONResponse({"error": "Send the medicines to check"}, status_code=400)
        return JSONResponse((await service.check_new_medicines([str(n) for n in names][:10])).model_dump())

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
            word = str(body.get("safe_word") or "") if "safe_word" in body else None
            if word is not None:
                service.check_safe_word_setting(word)  # before anything is saved
            saved = await service.update_settings(body.get("profile") or {}, body["medications"], trusted)
            if word is not None:
                service.set_safe_word(word)
                saved["safe_word"] = service.safe_word()
            return JSONResponse(saved)
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
        alert, called = await service.send_test_alert()
        return JSONResponse({"sent": alert.notified or called, "called": called, **await service.settings_view()})

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
        Route("/dashboard/doctor", doctor_html),
        Route("/api/dashboard", data),
        Route("/api/settings", household, methods=["GET", "PUT"]),
        Route("/api/settings/test-alert", test_alert, methods=["POST"]),
        Route("/api/reports/weekly", weekly_report, methods=["POST"]),
        Route("/api/alerts/{alert_id}/ack", ack, methods=["POST"]),
        Route("/api/checks", checks, methods=["POST"]),
        Route("/api/reminders/{reminder_id}/cancel", cancel_reminder, methods=["POST"]),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/messages", message, methods=["POST"]),
        Route("/api/reports/doctor", doctor_report),
        Route("/api/medications/scan", scan, methods=["POST"]),
        Route("/api/medications/interactions", check_interactions, methods=["POST"]),
        Route("/api/calls/{alert_id}/ack", call_ack, methods=["POST"]),
        Route("/api/telegram/webhook", telegram_webhook, methods=["POST"]),
        Route("/media/{message_id}", family_media),
        Route("/api/telegram/link", telegram_link, methods=["POST"]),
        Route("/api/telegram/unlink", telegram_link, methods=["POST"]),
    ]
