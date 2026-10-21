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
    GET  /api/family                 who may sign in to this household
    POST /api/family                 let someone sign in ({"email"}); POST /api/family/remove takes them off
    GET  /welcome                    a new family's first page: who they care for (then Settings)
    GET  /api/households             the signed-in newcomer ({"email", "name", "open"})
    POST /api/households             start their household ({"patient_name", "preferred_name", "your_name", "relation"})

Health data, so with OAuth on the dashboard needs a signed-in caregiver (LOGIN_CLIENT_ID, see
signin.py) or DASHBOARD_KEY (open /dashboard?key=...; EventBridge's checks use it too), and is off
with neither. With OAuth off (local dev) it is open. Every call is about the household the request
belongs to (households.py): the key's is the first household, a signed-in caregiver's their own.
"""

from __future__ import annotations

import hmac
import logging
from importlib.resources import files

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import media, notify, signin, telegram
from .config import household_id, settings
from .households import Households, within
from .models import ScanResult
from .scheduler import check_once
from .service import CareService, SettingsError

log = logging.getLogger(__name__)

OFF = "The caregiver dashboard is off: with OAuth on it needs LOGIN_CLIENT_ID or DASHBOARD_KEY."
MAX_PHOTO_BYTES = 5 * 1024 * 1024  # Claude's limit for one image


def dashboard_routes(service: CareService, homes: Households | None = None) -> list[Route]:
    homes = homes or Households.of(service)
    service = homes.current  # the household of each request
    page = files("kinvesper").joinpath("static/dashboard.html").read_text()
    settings_page = files("kinvesper").joinpath("static/settings.html").read_text()
    doctor_page = files("kinvesper").joinpath("static/doctor.html").read_text()
    welcome_page = files("kinvesper").joinpath("static/welcome.html").read_text()

    def keyed(request: Request) -> bool:
        key = request.headers.get("x-dashboard-key", "")
        return bool(settings.dashboard_key and key and hmac.compare_digest(key, settings.dashboard_key))

    def member(request: Request) -> dict | None:
        """The signed-in caregiver, if this request is about their own household."""
        s = signin.caregiver(request, homes.resolve)
        return s if s and s["household"] == household_id() else None

    def guard(request: Request) -> Response | None:
        if not settings.oauth_issuer:
            return None
        key = request.headers.get("x-dashboard-key", "")
        if keyed(request) or member(request):
            return None
        if signin.enabled() and not key:
            if signin.signed_in(request) and not signin.caregiver(request, homes.resolve):
                return JSONResponse({"error": "Set up your family first", "signup": "/welcome"}, status_code=401)
            return JSONResponse({"error": "Please sign in again", "login": signin.login_url("/dashboard")}, status_code=401)
        if not settings.dashboard_key:
            return JSONResponse({"error": OFF}, status_code=404)
        return JSONResponse({"error": "Missing or wrong dashboard key"}, status_code=403)

    def html(request: Request, body: str) -> Response:
        if not settings.oauth_issuer:
            return HTMLResponse(body)
        if signin.enabled() and not request.query_params.get("key") and not member(request):
            if signin.signed_in(request) and not signin.caregiver(request, homes.resolve):
                return RedirectResponse("/welcome", status_code=302)  # signed in, no family yet
            return RedirectResponse(signin.login_url(request.url.path), status_code=302)
        if not signin.enabled() and not settings.dashboard_key:
            return HTMLResponse(f"<p>{OFF}</p>", status_code=404)
        return HTMLResponse(body)

    async def dashboard_page(request: Request) -> Response:
        return html(request, page)

    async def settings_html(request: Request) -> Response:
        return html(request, settings_page)

    async def doctor_html(request: Request) -> Response:
        return html(request, doctor_page)

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
        url = f"{settings.public_url.rstrip('/')}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
        if not notify.twilio_signature_ok(url, form, request.headers.get("x-twilio-signature", "")):
            return Response(status_code=403)
        home = request.query_params.get("h") or homes.first  # signed with the URL
        if not homes.exists(home):
            return Response(status_code=404)
        with within(home):
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
            update = await json_body(request)
            with within(homes.telegram_household(update)):
                await service.telegram_update(update)
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
        home = q.get("h") or homes.first
        if not media.signature_ok(mid, q.get("e", ""), q.get("s", ""), home) or not homes.exists(home):
            return Response(status_code=403)
        with within(home):
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

    def family_view(request: Request) -> dict:
        home, s = household_id(), member(request)
        listed = signin.family_emails(service.profile()) if home == homes.first else set()
        return {
            "members": [{"email": m.email, "added_by": m.added_by, "at": m.at} for m in homes.members(home)],
            "also": sorted(listed - {m.email for m in homes.members(home)}),  # the first household's Settings emails
            "you": (s or {}).get("email", ""),
            "providers": signin.enabled(),
        }

    async def family(request: Request) -> Response:
        if denied := guard(request):
            return denied
        if request.method == "GET":
            return JSONResponse(family_view(request))
        email = str((await json_body(request)).get("email") or "")
        by = (member(request) or {}).get("email", "") or "dashboard key"
        try:
            if request.url.path.endswith("/remove"):
                homes.remove_member(household_id(), email, by)
            else:
                homes.add_member(household_id(), email, by)
        except SettingsError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(family_view(request))

    def newcomer(request: Request) -> dict | None:
        """Signed in with Google or Amazon, and in no family yet."""
        s = signin.signed_in(request)
        return s if s and s.get("fed") and not homes.resolve(str(s.get("email") or ""), True) else None

    async def welcome(request: Request) -> Response:
        if not signin.enabled():
            return RedirectResponse("/dashboard", status_code=302)
        if not signin.signed_in(request):
            return RedirectResponse(signin.login_url("/welcome"), status_code=302)
        if not newcomer(request):
            return RedirectResponse("/dashboard", status_code=302)
        return HTMLResponse(welcome_page)

    async def households(request: Request) -> Response:
        s = newcomer(request) if signin.enabled() else None
        if not s:
            return JSONResponse({"error": "Sign in with Google or Amazon first", "login": signin.login_url("/welcome")},
                                status_code=401)
        if request.method == "GET":
            return JSONResponse({"email": s.get("email", ""), "name": s.get("name", ""), "open": settings.signup})
        if not settings.signup:
            return JSONResponse({"error": "Kinvesper isn't taking new families right now."}, status_code=403)
        body = await json_body(request)
        try:
            home = homes.create(str(s.get("email") or ""), str(body.get("your_name") or ""), str(body.get("patient_name") or ""),
                                str(body.get("preferred_name") or ""), str(body.get("relation") or ""))
        except SettingsError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        log.info("A new family signed up")
        return JSONResponse({"household": home, "next": "/dashboard/settings"}, status_code=201)

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
        Route("/api/family", family, methods=["GET", "POST"]),
        Route("/api/family/remove", family, methods=["POST"]),
        Route("/welcome", welcome),
        Route("/api/households", households, methods=["GET", "POST"]),
    ]
