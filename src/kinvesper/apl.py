"""Echo Show screen: today's medicines as a large-print card (Alexa Presentation Language).

Shown with every reply on a device with a screen. Each dose that's due or missed has a big "Taken"
button; touching it sends an Alexa.Presentation.APL.UserEvent with ["taken", "<medicine>"], which logs
the dose like saying "I took my Metformin".
"""

from __future__ import annotations

from .models import ScheduleResult
from .service import spoken_time

TOKEN = "kinvesper-today"
STATUS = {  # label, colour: words and colour together, never colour alone
    "taken": ("Taken ✓", "#34c759"),
    "due": ("Due now", "#ffcc33"),
    "missed": ("Missed", "#ff6b63"),
    "upcoming": ("Later", "#9fb3b0"),
}


def supports_screen(body: dict) -> bool:
    device = ((body.get("context") or {}).get("System") or {}).get("device") or {}
    return "Alexa.Presentation.APL" in (device.get("supportedInterfaces") or {})


def _row() -> dict:
    return {
        "type": "Container", "direction": "row", "alignItems": "center", "paddingTop": "@spacingSmall",
        "paddingBottom": "@spacingSmall", "width": "100%",
        "items": [
            {"type": "Text", "text": "${data.time}", "width": "18vw", "fontSize": "@fontSizeLarge", "color": "#c4d0cd"},
            {"type": "Container", "grow": 1, "shrink": 1, "items": [
                {"type": "Text", "text": "${data.name}", "fontSize": "@fontSizeLarge", "fontWeight": "bold", "color": "#ffffff"},
                {"type": "Text", "text": "${data.note}", "fontSize": "@fontSizeSmall", "color": "#97a6a3", "maxLines": 1},
            ]},
            {"type": "Text", "text": "${data.label}", "color": "${data.color}", "fontSize": "@fontSizeMedium",
             "fontWeight": "bold", "paddingRight": "@spacingMedium"},
            {"type": "TouchWrapper", "when": "${data.canTake}", "accessibilityLabel": "Mark ${data.name} as taken",
             "onPress": [{"type": "SendEvent", "arguments": ["taken", "${data.medication}"]}],
             "item": {"type": "Frame", "backgroundColor": "#0f766e", "borderRadius": "40dp",
                      "paddingLeft": "@spacingMedium", "paddingRight": "@spacingMedium",
                      "paddingTop": "@spacingXSmall", "paddingBottom": "@spacingXSmall",
                      "item": {"type": "Text", "text": "Taken", "fontSize": "@fontSizeMedium", "fontWeight": "bold", "color": "#ffffff"}}},
        ],
    }


DOCUMENT = {
    "type": "APL", "version": "2023.3", "theme": "dark",
    "mainTemplate": {"parameters": ["payload"], "items": [{
        "type": "Container", "width": "100vw", "height": "100vh",
        "items": [
            {"type": "Frame", "position": "absolute", "width": "100vw", "height": "100vh",
             "background": {"type": "LinearGradient", "colorRange": ["#0e3b39", "#14254a", "#2a1a44"], "angle": 135}},
            {"type": "Container", "width": "100vw", "height": "100vh", "paddingLeft": "@marginHorizontal",
             "paddingRight": "@marginHorizontal", "paddingTop": "@spacingMedium", "items": [
                {"type": "Text", "text": "${payload.screen.title}", "fontSize": "@fontSizeXLarge", "fontWeight": "bold", "color": "#ffffff"},
                {"type": "Text", "text": "${payload.screen.subtitle}", "fontSize": "@fontSizeMedium", "color": "#c4d0cd",
                 "maxLines": 2, "paddingBottom": "@spacingSmall"},
                {"type": "Sequence", "grow": 1, "data": "${payload.screen.items}", "items": [_row()]},
            ]},
        ],
    }]},
}


def screen(schedule: ScheduleResult, name: str, said: str = "") -> dict:
    """The RenderDocument directive for today's doses."""
    taken = sum(i.status == "taken" for i in schedule.items)
    items = [{
        "time": spoken_time(i.time), "name": f"{i.medication} {i.dose}", "medication": i.medication,
        "note": i.instructions or i.purpose, "label": STATUS[i.status][0], "color": STATUS[i.status][1],
        "canTake": i.status in ("due", "missed"),
    } for i in schedule.items]
    return {
        "type": "Alexa.Presentation.APL.RenderDocument", "token": TOKEN, "document": DOCUMENT,
        "datasources": {"screen": {
            "title": f"{name}'s medicines today",
            "subtitle": said or f"{taken} of {len(schedule.items)} taken",
            "items": items,
        }},
    }


def photo(url: str, caption: str) -> dict:
    """A family photo, full screen, with who sent it."""
    return {
        "type": "Alexa.Presentation.APL.RenderDocument", "token": "kinvesper-photo",
        "document": {"type": "APL", "version": "2023.3", "theme": "dark", "mainTemplate": {"parameters": ["payload"], "items": [{
            "type": "Container", "width": "100vw", "height": "100vh", "items": [
                {"type": "Image", "source": "${payload.photo.url}", "width": "100vw", "height": "100vh",
                 "scale": "best-fit", "position": "absolute", "accessibilityLabel": "${payload.photo.caption}"},
                {"type": "Frame", "position": "absolute", "bottom": 0, "width": "100vw",
                 "background": {"type": "LinearGradient", "colorRange": ["#00000000", "#000000cc"], "angle": 180},
                 "item": {"type": "Text", "text": "${payload.photo.caption}", "fontSize": "@fontSizeLarge", "fontWeight": "bold",
                          "color": "#ffffff", "paddingLeft": "@marginHorizontal", "paddingTop": "@spacingLarge",
                          "paddingBottom": "@spacingMedium"}},
            ]}]}},
        "datasources": {"photo": {"url": url, "caption": caption}},
    }
