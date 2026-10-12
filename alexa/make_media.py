"""Draw the Alexa store icons and carousel images into src/kinvesper/static/addon/.

The add-on and skill manifests need PNGs at fixed sizes, hosted at public URLs; Kinvesper serves them itself.
Run once after changing the artwork:  uv run --with pillow python alexa/make_media.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "src/kinvesper/static/addon"
TEAL, TEAL_DARK, WHITE = (15, 118, 110), (17, 78, 74), (255, 255, 255)
ICON_SIZES = (64, 72, 88, 126, 180, 241)  # Alexa+ add-on
SKILL_ICON_SIZES = (108, 512)  # classic skill (alexa/skill)
FONTS = "/usr/share/fonts/truetype/noto"
SS = 4  # draw large, then downsample for smooth edges


def heart(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    """A heart of two overlapping circles and a triangle, centred on (cx, cy), about 2r wide."""
    lobe, dx, top = r * 0.55, r * 0.48, cy - r * 0.45
    for x in (cx - dx, cx + dx):
        draw.ellipse((x - lobe, top - lobe, x + lobe, top + lobe), fill=fill)
    edge = dx + lobe * 0.96
    draw.polygon([(cx - edge, top + lobe * 0.28), (cx + edge, top + lobe * 0.28), (cx, cy + r * 0.95)], fill=fill)
    draw.rectangle((cx - dx, top, cx + dx, top + lobe), fill=fill)


def logo(size: int, tile: bool = True) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if tile:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s * 0.22, fill=TEAL)
    ring = s * 0.36
    d.ellipse((s / 2 - ring, s / 2 - ring, s / 2 + ring, s / 2 + ring), outline=WHITE, width=round(s * 0.06))
    heart(d, s / 2, s / 2 + s * 0.02, s * 0.2, WHITE)
    return img.resize((size, size), Image.LANCZOS)


def carousel(title: str, lines: list[str], name: str) -> None:
    w, h = 600, 900
    img = Image.new("RGB", (w, h), TEAL)
    d = ImageDraw.Draw(img)
    d.rectangle((0, h * 0.62, w, h), fill=TEAL_DARK)
    mark = logo(220, tile=False)
    img.paste(mark, ((w - 220) // 2, 120), mark)
    bold = ImageFont.truetype(f"{FONTS}/NotoSans-Bold.ttf", 52)
    body = ImageFont.truetype(f"{FONTS}/NotoSans-Regular.ttf", 34)
    d.text((w / 2, 430), title, font=bold, fill=WHITE, anchor="mm")
    y = 600
    for line in lines:
        d.text((w / 2, y), line, font=body, fill=WHITE, anchor="mm")
        y += 56
    img.save(OUT / name, optimize=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (*ICON_SIZES, *SKILL_ICON_SIZES):
        logo(size).save(OUT / f"icon-{size}.png", optimize=True)
    carousel("Kinvesper", ['"Did I take my pills?"', '"Tell Priya I\'m fine."', '"Is this call a scam?"'], "carousel-1.png")
    carousel("Family in the loop", ["Missed dose? Priya gets a text.", "Dizzy spell? She hears", "about it right away."], "carousel-2.png")
    print(f"Wrote {len(ICON_SIZES) + len(SKILL_ICON_SIZES)} icons and 2 carousel images to {OUT}")


if __name__ == "__main__":
    main()
