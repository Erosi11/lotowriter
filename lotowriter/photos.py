"""Photo preparation for printing: fix orientation, strip metadata, draw markup, downsize."""
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

PRINT_DPI = 300

# Markup proportions (fractions of the photo's longer side). The web app's SVG overlay uses the same values.
STROKE = 0.012
ARROW_HEAD = 0.05
TEXT_SIZE = 0.06
COLORS = {"red": (230, 0, 0), "yellow": (255, 214, 0), "white": (255, 255, 255), "black": (0, 0, 0)}
OUTLINE = {"red": (255, 255, 255), "yellow": (0, 0, 0), "white": (0, 0, 0), "black": (255, 255, 255)}


def _font(size):
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def draw_annotations(im, annotations):
    """Draw arrows / ellipses / boxes / text. Coordinates are fractions of width and height."""
    if not annotations:
        return im
    w, h = im.size
    longest = max(w, h)
    width = max(2, round(STROKE * longest))
    halo = max(1, width // 3)
    d = ImageDraw.Draw(im)
    for a in annotations:
        color = COLORS.get(a.get("color"), COLORS["red"])
        outline = OUTLINE.get(a.get("color"), (255, 255, 255))
        kind = a.get("type")
        if kind == "arrow":
            x1, y1, x2, y2 = a["x1"] * w, a["y1"] * h, a["x2"] * w, a["y2"] * h
            ang = math.atan2(y2 - y1, x2 - x1)
            head = ARROW_HEAD * longest
            pts = [(x2, y2),
                   (x2 - head * math.cos(ang - 0.45), y2 - head * math.sin(ang - 0.45)),
                   (x2 - head * math.cos(ang + 0.45), y2 - head * math.sin(ang + 0.45))]
            # Shaft stops at the base of the head so the tip stays sharp.
            bx, by = x2 - 0.8 * head * math.cos(ang), y2 - 0.8 * head * math.sin(ang)
            d.line([(x1, y1), (bx, by)], fill=outline, width=width + 2 * halo)
            d.polygon(pts, fill=outline, outline=outline, width=2 * halo)
            d.line([(x1, y1), (bx, by)], fill=color, width=width)
            d.polygon(pts, fill=color)
        elif kind in ("ellipse", "rect"):
            x0, y0 = a["x"] * w, a["y"] * h
            x1, y1 = x0 + a["w"] * w, y0 + a["h"] * h
            box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
            shape = d.ellipse if kind == "ellipse" else d.rectangle
            shape(box, outline=outline, width=width + 2 * halo)
            shape(box, outline=color, width=width)
        elif kind == "text":
            size = max(10, round(TEXT_SIZE * longest))
            d.text((a["x"] * w, a["y"] * h), str(a.get("text", "")), fill=color, font=_font(size), anchor="lm",
                   stroke_width=max(1, size // 12), stroke_fill=outline)
    return im


def prepare_photo(src, dst_dir, name, max_w_in, max_h_in, annotations=None):
    """Write a print-ready JPEG and return (path, width_in, height_in).

    - Applies the EXIF orientation so photos never print sideways.
    - Draws the photo markup (arrows, circles, labels) on the print copy only.
    - Saves without EXIF, which drops GPS/location data.
    - Scales to fit max_w_in x max_h_in at 300 dpi (never upscales).
    """
    src = Path(src)
    dst_dir = Path(dst_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != "RGB":
            im = im.convert("RGB")
        im = draw_annotations(im, annotations)
        w, h = im.size
        scale = min(max_w_in / w, max_h_in / h)  # inches per pixel to fit the box
        out_w_in, out_h_in = w * scale, h * scale
        target_px = (max(1, round(out_w_in * PRINT_DPI)), max(1, round(out_h_in * PRINT_DPI)))
        if target_px[0] < w:
            im = im.resize(target_px, Image.LANCZOS)
        dst = dst_dir / f"{name}.jpg"
        im.save(dst, "JPEG", quality=85, optimize=True)
    return dst, out_w_in, out_h_in
