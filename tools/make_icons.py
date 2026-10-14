"""Draw the app icons (padlock on navy) into lotowriter/web/static/icons/."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "lotowriter" / "web" / "static" / "icons"
NAVY, WHITE, RED = (31, 58, 95), (255, 255, 255), (220, 30, 30)


def icon(size):
    im = Image.new("RGB", (size, size), NAVY)
    d = ImageDraw.Draw(im)
    u = size / 100  # design on a 100-unit grid, content inside the maskable safe zone (center 80%)
    d.rounded_rectangle([30 * u, 46 * u, 70 * u, 76 * u], radius=5 * u, fill=WHITE)          # body
    d.arc([36 * u, 24 * u, 64 * u, 58 * u], start=180, end=360, fill=WHITE, width=round(6 * u))  # shackle
    d.line([39 * u, 41 * u, 39 * u, 47 * u], fill=WHITE, width=round(6 * u))
    d.line([61 * u, 41 * u, 61 * u, 47 * u], fill=WHITE, width=round(6 * u))
    d.ellipse([46 * u, 54 * u, 54 * u, 62 * u], fill=RED)                                      # keyhole
    d.rectangle([48.5 * u, 60 * u, 51.5 * u, 69 * u], fill=RED)
    return im


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for s in (192, 512):
        icon(s).save(OUT / f"icon-{s}.png", optimize=True)
    print(f"Icons written to {OUT}")
