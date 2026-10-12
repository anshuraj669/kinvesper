"""Public pages the Alexa+ store listing links to: privacy policy, terms of use and artwork.

    GET /privacy              privacy policy
    GET /terms                terms of use
    GET /addon/{name}.png     store icons and carousel images (drawn by alexa/make_media.py)

These are open even with OAuth on: Amazon and customers read them before linking an account.
"""

from __future__ import annotations

import re
from importlib.resources import files

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

STATIC = files("kinvesper").joinpath("static")
CACHE = {"Cache-Control": "public, max-age=86400"}


def listing_routes() -> list[Route]:
    privacy = STATIC.joinpath("privacy.html").read_text()
    terms = STATIC.joinpath("terms.html").read_text()

    async def privacy_page(_: Request) -> Response:
        return HTMLResponse(privacy, headers=CACHE)

    async def terms_page(_: Request) -> Response:
        return HTMLResponse(terms, headers=CACHE)

    async def artwork(request: Request) -> Response:
        name = request.path_params["name"]
        image = STATIC.joinpath("addon", f"{name}.png")
        if not re.fullmatch(r"[a-z0-9-]+", name) or not image.is_file():
            return Response(status_code=404)
        return Response(image.read_bytes(), media_type="image/png", headers=CACHE)

    return [
        Route("/privacy", privacy_page),
        Route("/terms", terms_page),
        Route("/addon/{name}.png", artwork),
    ]
