"""Build the Workloop desktop logo from the supplied transparent artwork.

The source artwork is kept in ``assets/logo-source.png``.  This script trims
its transparent bounds, centers the mark on a square canvas, and emits:

* ``assets/icon.png``: 1024px transparent square app icon
* ``assets/icon.ico``: Windows sizes from 16px through 256px
* ``assets/icon.svg``: an SVG wrapper with the artwork embedded as base64
* ``assets/logo-wide.png`` and matching webview assets: the horizontal lockup

Usage: ``python tools/make_icon.py``
"""
from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image


ICON_SIZE = 1024
MARK_MARGIN = 42


def normalize_logo(source_path: Path) -> Image.Image:
    """Return the supplied mark centered on a square transparent canvas."""
    source = Image.open(source_path).convert("RGBA")
    alpha = source.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError(f"logo source is fully transparent: {source_path}")

    cropped = source.crop(bbox)
    side = max(cropped.width, cropped.height)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(
        cropped,
        ((side - cropped.width) // 2, (side - cropped.height) // 2),
        cropped,
    )

    target = ICON_SIZE - (MARK_MARGIN * 2)
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    square = square.resize((target, target), resampling)
    result = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    result.paste(square, (MARK_MARGIN, MARK_MARGIN), square)
    return result


def wide_logo(source_path: Path) -> Image.Image:
    """Return the supplied mark cropped to its natural horizontal lockup."""
    source = Image.open(source_path).convert("RGBA")
    visible_alpha = source.getchannel("A").point(lambda value: 255 if value > 32 else 0)
    bbox = visible_alpha.getbbox()
    if bbox is None:
        raise ValueError(f"logo source is fully transparent: {source_path}")

    cropped = source.crop(bbox)
    pad_x = max(12, int(cropped.width * 0.045))
    pad_y = max(12, int(cropped.height * 0.12))
    result = Image.new(
        "RGBA",
        (cropped.width + pad_x * 2, cropped.height + pad_y * 2),
        (0, 0, 0, 0),
    )
    result.paste(cropped, (pad_x, pad_y), cropped)
    return result


def write_svg(path: Path, png: bytes, *, width: int, height: int) -> None:
    """Write a standalone SVG wrapper so the logo remains easy to preview."""
    encoded = base64.b64encode(png).decode("ascii")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        'role="img" aria-labelledby="title desc">'
        '<title id="title">Workloop</title>'
        '<desc id="desc">Interwoven infinity loop with a cyan star at the center.</desc>'
        f'<image width="{width}" height="{height}" href="data:image/png;base64,{encoded}"/>'
        '</svg>\n'
    )
    path.write_text(svg, encoding="utf-8")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    assets = root / "assets"
    source_path = assets / "logo-source.png"
    if not source_path.is_file():
        raise FileNotFoundError(
            f"missing supplied logo artwork: {source_path}. "
            "Copy the transparent logo into assets/logo-source.png first."
        )

    icon = normalize_logo(source_path)
    wide = wide_logo(source_path)
    icon_path = assets / "icon.png"
    icon.save(icon_path)
    icon.save(
        assets / "icon.ico",
        sizes=[
            (16, 16),
            (24, 24),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )

    png_bytes = icon_path.read_bytes()
    wide_path = assets / "logo-wide.png"
    wide.save(wide_path)
    svg_path = assets / "icon.svg"
    write_svg(svg_path, png_bytes, width=icon.width, height=icon.height)

    static_dir = root / "app" / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "app-icon.png").write_bytes(png_bytes)
    wide_png = wide_path.read_bytes()
    (static_dir / "logo-wide.png").write_bytes(wide_png)
    write_svg(static_dir / "logo.svg", wide_png, width=wide.width, height=wide.height)

    print(
        "written: "
        f"{icon_path}, {assets / 'icon.ico'}, {svg_path}, {wide_path}, "
        f"{static_dir / 'app-icon.png'}, {static_dir / 'logo-wide.png'}, "
        f"{static_dir / 'logo.svg'}"
    )


if __name__ == "__main__":
    main()
