"""Public pages the Alexa+ store listing links to, and the add-on manifest that points at them."""

import json
from pathlib import Path

import httpx

ADDON = Path(__file__).resolve().parent.parent / "alexa/addon-package/addon.json"


def test_listing_pages_are_public(auth_server):
    for path in ("/privacy", "/terms"):
        r = httpx.get(f"{auth_server}{path}")
        assert r.status_code == 200
        assert "Kinvesper" in r.text
    icon = httpx.get(f"{auth_server}/addon/icon-72.png")
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"
    assert icon.content.startswith(b"\x89PNG")
    assert httpx.get(f"{auth_server}/addon/missing.png").status_code == 404
    assert httpx.get(f"{auth_server}/addon/..%2Fprivacy.png").status_code == 404


def test_addon_manifest_points_at_served_files(auth_server):
    addon = json.loads(ADDON.read_text())
    listing = addon["storeListing"]["locales"]["en-US"]
    assert len(listing["shortDescription"]) <= 123
    assert len(listing["fullDescription"]) <= 4000
    assert len(listing["examplePhrases"]) >= 3
    assert addon["integrations"][0]["config"]["endpoints"]["default"]["uri"].endswith("/mcp")

    sizes = {i["size"] for i in listing["mediaAssets"]["icons"]["light"]}
    assert sizes == {"64x64", "72x72", "88x88", "126x126", "180x180", "241x241"}
    for asset in [*listing["mediaAssets"]["icons"]["light"], *listing["mediaAssets"]["carouselImages"]]:
        path = asset["uri"].split("/addon/", 1)[1]
        assert httpx.get(f"{auth_server}/addon/{path}").status_code == 200, path
