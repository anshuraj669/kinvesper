"""The wish list: found on Amazon by Claude's web search, kept for the family, and on an Echo put on the
Amazon Wish List through Alexa Shopping."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from kinvesper import alexa, llm, service as service_module
from kinvesper.alexa import AlexaBridge
from kinvesper.voice import VoiceSessions


def found(text: str, *urls: str):
    """A Claude reply after one web search."""
    return SimpleNamespace(stop_reason="end_turn", content=[
        SimpleNamespace(type="server_tool_use"),
        SimpleNamespace(type="web_search_tool_result", content=[SimpleNamespace(url=u, title="") for u in urls]),
        SimpleNamespace(type="text", text=text, citations=None)])


@pytest.fixture
def search(monkeypatch):
    replies = []
    monkeypatch.setattr(llm, "server_tools", lambda: [{"type": "web_search_20250305", "name": "web_search"}])

    async def create_message(**params):
        assert "amazon.in" in params["system"] and params["tools"]
        return replies.pop(0)
    monkeypatch.setattr(llm, "create_message", create_message)
    monkeypatch.setattr(service_module, "settings", dataclasses.replace(service_module.settings, amazon_site="amazon.in"))
    return replies


def test_a_wish_is_found_on_amazon_and_sent_to_the_family(service, search):
    search.append(found("PRODUCT: Philips LED reading lamp | URL: https://www.amazon.in/dp/B0ABCDEFGH",
                        "https://www.amazon.in/Philips-Reading-Lamp/dp/B0ABCDEFGH/ref=sr_1_1"))
    r = asyncio.run(service.add_to_wishlist("a reading lamp"))
    assert r.saved and r.item.asin == "B0ABCDEFGH" and r.item.url == "https://www.amazon.in/dp/B0ABCDEFGH"
    assert r.message.startswith("I found Philips LED reading lamp on Amazon and saved it to your wish list")
    alert = service.alerts()[0]
    assert alert.kind == "wish" and "reading lamp" in alert.title and "amazon.in/dp/B0ABCDEFGH" in alert.detail
    assert service.dashboard()["wishes"][0]["title"] == "Philips LED reading lamp"


def test_an_asin_the_search_never_returned_is_not_trusted(service, search):
    search.append(found("PRODUCT: Some lamp | URL: https://www.amazon.in/dp/B0MADEUP00",
                        "https://www.amazon.in/Other/dp/B0ABCDEFGH"))
    r = asyncio.run(service.add_to_wishlist("a reading lamp"))
    assert r.saved and not r.item.asin and r.item.url == "https://www.amazon.in/s?k=a+reading+lamp"
    assert "saved a reading lamp to your wish list" in r.message


def test_without_web_search_it_saves_a_search_link(service, monkeypatch):
    monkeypatch.setattr(llm, "server_tools", lambda: [])
    r = asyncio.run(service.add_to_wishlist("  Agatha Christie books. "))
    assert r.item.item == "Agatha Christie books" and "/s?k=Agatha+Christie+books" in r.item.url
    assert not asyncio.run(service.add_to_wishlist("")).saved


def test_the_amazon_site_follows_the_care_time_zone(monkeypatch, service):
    from zoneinfo import ZoneInfo
    for tz, site in (("Asia/Kolkata", "amazon.in"), ("America/New_York", "amazon.com"), ("Europe/London", "amazon.co.uk")):
        monkeypatch.setattr(service_module, "settings", dataclasses.replace(service_module.settings, tz=ZoneInfo(tz), amazon_site=""))
        assert service.amazon_site() == site


@pytest.fixture
async def bridge(connect, monkeypatch, service):
    monkeypatch.setattr(alexa.llm, "enabled", lambda: False)
    sessions = VoiceSessions(connect=connect)
    yield AlexaBridge(sessions, service)
    await sessions.close_all()


def resumed(token: str, code: str = "200", result: dict | None = None) -> dict:
    return {"session": {"sessionId": "s-wish"}, "request": {"type": "SessionResumedRequest", "locale": "en-US", "cause": {
        "type": "ConnectionCompleted", "token": token, "status": {"code": code, "message": "OK"}, "result": result}},
        "context": {"System": {"device": {"supportedInterfaces": {}}}}}


async def test_on_alexa_the_wish_goes_to_the_amazon_wish_list(bridge, service, search):
    search.append(found("PRODUCT: Warm wool shawl | URL: https://www.amazon.in/dp/B0SHAWL123",
                        "https://www.amazon.in/dp/B0SHAWL123"))
    wish = (await service.add_to_wishlist("a warm shawl")).item
    session = await bridge._session("s-wish")
    turn = asyncio.get_running_loop().create_future()
    turn.set_result(SimpleNamespace(
        reply="I found Warm wool shawl on Amazon.", awaiting_confirmation=False,
        tool_calls=[SimpleNamespace(name="add_to_wishlist", is_error=False)]))
    r = await bridge._reply("s-wish", session, turn, one_shot=False)
    directive = r["response"]["directives"][0]
    assert directive["type"] == "Connections.StartConnection" and directive["uri"] == "connection://AMAZON.AddToList/1"
    assert directive["input"] == {"products": [{"asin": "B0SHAWL123"}], "listType": "WISHLIST"}
    assert directive["token"] == f"wish:{wish.id}" and "shouldEndSession" not in r["response"]
    assert service.wish_for_alexa() is None  # handed over once

    ok = await bridge.handle(resumed(f"wish:{wish.id}"))
    assert "Amazon Wish List too" in ok["response"]["outputSpeech"]["text"]
    assert service.wish_list()[0].status == "on_amazon"

    # Amazon hasn't turned the preview on, or the item can't be listed: it's still saved
    failed = await bridge.handle(resumed(f"wish:{wish.id}", result={"code": "AlexaShopping.InvalidTaskForMarketplace"}))
    assert "couldn't put it on your Amazon Wish List" in failed["response"]["outputSpeech"]["text"]
    assert service.wish_list()[0].status == "not_on_amazon"
