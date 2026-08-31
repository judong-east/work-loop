"""Generate the Workloop app icon (loop mark on a mint-to-cyan squircle).

Draws with Pillow at high resolution and downscales for crisp small sizes,
emitting assets/icon.png (1024px), assets/icon.ico (multi-size), and the
matching vector source assets/icon.svg.

Usage: py -3.10 tools/make_icon.py
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

# Diagonal gradient stops matching --accent-grad in workbench.css.
GRADIENT_FROM = (61, 219, 164)    # mint
GRADIENT_TO = (14, 165, 233)      # cyan
MARK_COLOR = (255, 255, 255, 255)

SIZE = 1024          # working canvas (downscaled for output)
CORNER = 232         # squircle corner radius (~22%)
RING_RADIUS = 300    # loop ring radius
STROKE = 108         # loop ring thickness
CENTER = SIZE // 2


def ring_points(angle_deg: float) -> tuple[float, float]:
    rad = math.radians(angle_deg)
    return (CENTER + RING_RADIUS * math.cos(rad), CENTER + RING_RADIUS * math.sin(rad))


def draw_loop(draw: ImageDraw.ImageDraw) -> None:
    """One 290-degree clockwise arc with an arrowhead; gap on the right side."""
    box = (CENTER - RING_RADIUS, CENTER - RING_RADIUS, CENTER + RING_RADIUS, CENTER + RING_RADIUS)
    # Screen angles: 35deg is lower-right; sweep clockwise through bottom,
    # left, top, ending at -35deg (upper-right).
    draw.arc(box, start=35, end=325, fill=MARK_COLOR, width=STROKE)

    # Arrowhead at the end point (-35deg), pointing along the clockwise tangent.
    tip_angle = -35.0
    tip_point = ring_points(tip_angle)
    rad = math.radians(tip_angle)
    tangent = (-math.sin(rad), math.cos(rad))
    normal = (math.cos(rad), math.sin(rad))
    length = 190
    half = 118
    tip = (tip_point[0] + tangent[0] * length, tip_point[1] + tangent[1] * length)
    base_a = (tip_point[0] + normal[0] * half, tip_point[1] + normal[1] * half)
    base_b = (tip_point[0] - normal[0] * half, tip_point[1] - normal[1] * half)
    draw.polygon([tip, base_a, base_b], fill=MARK_COLOR)


def gradient_tile(size: int) -> Image.Image:
    """Diagonal mint-to-cyan gradient built from a 2x2 bilinear ramp."""
    ramp = Image.new("RGB", (2, 2))
    ramp.putpixel((0, 0), GRADIENT_FROM)
    ramp.putpixel((1, 1), GRADIENT_TO)
    # Blend the off-diagonal corners so the ramp stays smooth, not banded.
    mid = tuple((a + b) // 2 for a, b in zip(GRADIENT_FROM, GRADIENT_TO))
    ramp.putpixel((1, 0), mid)
    ramp.putpixel((0, 1), mid)
    gradient = ramp.resize((size, size), Image.BILINEAR).convert("RGBA")

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=CORNER * (size // SIZE), fill=255,
    )
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(gradient, (0, 0), mask)
    return tile


def render(scale: int) -> Image.Image:
    size = SIZE * scale
    image = gradient_tile(size)
    draw = ImageDraw.Draw(image)
    s = scale
    # Draw the loop geometry in working units by scaling every coordinate.
    globals_box = (CENTER - RING_RADIUS) * s
    draw.arc(
        (globals_box, globals_box, (CENTER + RING_RADIUS) * s, (CENTER + RING_RADIUS) * s),
        start=35, end=325, fill=MARK_COLOR, width=STROKE * s,
    )
    tip_point = ring_points(-35.0)
    rad = math.radians(-35.0)
    tangent = (-math.sin(rad), math.cos(rad))
    normal = (math.cos(rad), math.sin(rad))
    tip = ((tip_point[0] + tangent[0] * 190) * s, (tip_point[1] + tangent[1] * 190) * s)
    base_a = ((tip_point[0] + normal[0] * 118) * s, (tip_point[1] + normal[1] * 118) * s)
    base_b = ((tip_point[0] - normal[0] * 118) * s, (tip_point[1] - normal[1] * 118) * s)
    draw.polygon([tip, base_a, base_b], fill=MARK_COLOR)
    return image.resize((SIZE, SIZE), Image.LANCZOS)


ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#3DDBA4"/>
      <stop offset="1" stop-color="#0EA5E9"/>
    </linearGradient>
  </defs>
  <rect width="1024" height="1024" rx="232" fill="url(#g)"/>
  <g fill="none" stroke="#FFFFFF" stroke-width="108" stroke-linecap="round">
    <path d="M {start_x} {start_y} A {r} {r} 0 1 1 {end_x} {end_y}"/>
  </g>
  <polygon fill="#FFFFFF" points="{ax},{ay} {bx},{by} {cx},{cy}"/>
</svg>
"""


def write_svg(path: Path) -> None:
    start = ring_points(35.0)
    end = ring_points(-35.0)
    tip_point = ring_points(-35.0)
    rad = math.radians(-35.0)
    tangent = (-math.sin(rad), math.cos(rad))
    normal = (math.cos(rad), math.sin(rad))
    tip = (tip_point[0] + tangent[0] * 190, tip_point[1] + tangent[1] * 190)
    base_a = (tip_point[0] + normal[0] * 118, tip_point[1] + normal[1] * 118)
    base_b = (tip_point[0] - normal[0] * 118, tip_point[1] - normal[1] * 118)
    path.write_text(ICON_SVG.format(
        r=RING_RADIUS,
        start_x=f"{start[0]:.1f}", start_y=f"{start[1]:.1f}",
        end_x=f"{end[0]:.1f}", end_y=f"{end[1]:.1f}",
        ax=f"{tip[0]:.1f}", ay=f"{tip[1]:.1f}",
        bx=f"{base_a[0]:.1f}", by=f"{base_a[1]:.1f}",
        cx=f"{base_b[0]:.1f}", cy=f"{base_b[1]:.1f}",
    ), encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    icon = render(scale=2)
    icon.save(assets / "icon.png")
    icon.save(
        assets / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    write_svg(assets / "icon.svg")
    print(f"written: {assets}\\icon.png, icon.ico, icon.svg")


if __name__ == "__main__":
    main()
