"""Icons for the server, tools, resources and prompts, as self-contained SVG data URIs.

Each icon is a white line glyph on a rounded colored tile. Data URIs keep them working offline
and behind OAuth, with no extra route to host.
"""

from __future__ import annotations

from base64 import b64encode

from mcp.types import Icon

TEAL, BLUE, GREEN, AMBER, RED, PURPLE, SLATE = "#0f766e", "#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed", "#475569"

GLYPHS = {
    "logo": '<circle cx="12" cy="12" r="8.5"/><path d="M12 16.5s-4-2.6-4-5.6a2.2 2.2 0 0 1 4-1.3 2.2 2.2 0 0 1 4 1.3c0 3-4 5.6-4 5.6z"/>',
    "clock": '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "heart": '<path d="M12 20s-7-4.5-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.5-7 10-7 10z"/>',
    "message": '<path d="M4 5h16v11H9l-5 4z"/>',
    "warning": '<path d="M12 3.5l9 16H3z"/><path d="M12 10v4.5M12 17.5h.01"/>',
    "chart": '<path d="M4 4v16h16"/><path d="M8.5 16v-4M12.5 16V8M16.5 16v-6"/>',
    "bag": '<rect x="5" y="7" width="14" height="13" rx="2"/><path d="M9 7V5h6v2M12 10.5v6M9 13.5h6"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 20c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    "bell": '<path d="M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 21h4"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    "report": '<path d="M6 3h9l3 3v15H6z"/><path d="M9 11h6M9 15h6"/>',
}


def icon(glyph: str, color: str) -> list[Icon]:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        f'<rect width="24" height="24" rx="5" fill="{color}"/>'
        '<g fill="none" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
        f'transform="translate(2.4 2.4) scale(0.8)">{GLYPHS[glyph]}</g></svg>'
    )
    src = "data:image/svg+xml;base64," + b64encode(svg.encode()).decode()
    return [Icon(src=src, mimeType="image/svg+xml", sizes=["any"])]
