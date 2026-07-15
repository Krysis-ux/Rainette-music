"""Render Rainette Music's Kagebana app mark and bundled icon assets.

The artwork is intentionally built from geometry instead of a platform font so
the output remains deterministic and the silhouette stays clear at Windows'
smallest icon sizes. Run this script whenever the icon design changes.
"""
from __future__ import annotations

from math import cos, pi, sin
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent.parent / "web" / "assets"
OUT_ICO = OUT_DIR / "rainette-icon.ico"
OUT_PNG = OUT_DIR / "rainette-icon-256.png"
ANDROID_RES = Path(__file__).resolve().parent.parent / "mobile" / "android" / "app" / "src" / "main" / "res"

CANVAS = 1024  # supersampled, then downsampled for crisp edges at small sizes
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# The canonical editable source is brand/rainette-kagebana.svg. This renderer
# repeats its geometry with Pillow so the Windows and Android raster outputs
# are deterministic and crisp at the system's smallest icon sizes.
INK = (11, 15, 13)
PAPER = (218, 224, 216)
SAGE = (126, 156, 135)
CORE = (23, 52, 39)


def cubic_points(
    start: tuple[float, float],
    control_1: tuple[float, float],
    control_2: tuple[float, float],
    end: tuple[float, float],
    *,
    steps: int = 48,
) -> list[tuple[float, float]]:
    """Sample a cubic Bezier segment for Pillow's polygon renderer."""
    points = []
    for index in range(1, steps + 1):
        t = index / steps
        inverse = 1.0 - t
        x = (
            inverse**3 * start[0]
            + 3 * inverse**2 * t * control_1[0]
            + 3 * inverse * t**2 * control_2[0]
            + t**3 * end[0]
        )
        y = (
            inverse**3 * start[1]
            + 3 * inverse**2 * t * control_1[1]
            + 3 * inverse * t**2 * control_2[1]
            + t**3 * end[1]
        )
        points.append((x, y))
    return points


def _rotated(x: float, y: float, angle: float) -> tuple[float, float]:
    return (
        CANVAS * 0.5 + x * cos(angle) - y * sin(angle),
        CANVAS * 0.5 + x * sin(angle) + y * cos(angle),
    )


def petal_points(angle: float, *, inner: bool = False) -> list[tuple[float, float]]:
    """A tapered paper-cut petal with enough mass to remain legible at 16 px."""
    width = 64 if inner else 92
    base = 61 if inner else 92
    tip = 278 if inner else 388
    points = []
    for index in range(25):
        t = index / 24
        points.append(_rotated(-width * sin(t * pi), -base - (tip - base) * t, angle))
    for index in range(24, -1, -1):
        t = index / 24
        points.append(_rotated(width * sin(t * pi), -base - (tip - base) * t, angle))
    return points


def draw_flower(draw: ImageDraw.ImageDraw) -> None:
    for index in range(7):
        draw.polygon(petal_points(index * 2 * pi / 7), fill=PAPER + (255,))
    for index in range(7):
        draw.polygon(petal_points((index + 0.5) * 2 * pi / 7, inner=True), fill=SAGE + (255,))
    draw.ellipse([392, 392, 632, 632], fill=CORE + (255,), outline=PAPER + (255,), width=14)


def render_mark(*, round_tile: bool = False) -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    mask_draw = ImageDraw.Draw(mask)
    if round_tile:
        mask_draw.ellipse([0, 0, CANVAS - 1, CANVAS - 1], fill=255)
    else:
        radius = round(CANVAS * 0.235)
        mask_draw.rounded_rectangle([0, 0, CANVAS - 1, CANVAS - 1], radius=radius, fill=255)
    tile = Image.new("RGBA", (CANVAS, CANVAS), INK + (255,))
    img.paste(tile, (0, 0), mask)
    draw = ImageDraw.Draw(img)
    draw_flower(draw)
    return img


def render_foreground() -> Image.Image:
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw_flower(draw)
    return img


def write_android_icons(mark: Image.Image) -> None:
    round_mark = render_mark(round_tile=True)
    foreground = render_foreground()
    sizes = {
        "mdpi": (48, 108),
        "hdpi": (72, 162),
        "xhdpi": (96, 216),
        "xxhdpi": (144, 324),
        "xxxhdpi": (192, 432),
    }
    for density, (legacy_size, foreground_size) in sizes.items():
        directory = ANDROID_RES / f"mipmap-{density}"
        directory.mkdir(parents=True, exist_ok=True)
        mark.resize((legacy_size, legacy_size), Image.Resampling.LANCZOS).save(directory / "ic_launcher.png")
        round_mark.resize((legacy_size, legacy_size), Image.Resampling.LANCZOS).save(directory / "ic_launcher_round.png")
        foreground.resize((foreground_size, foreground_size), Image.Resampling.LANCZOS).save(directory / "ic_launcher_foreground.png")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    img = render_mark()

    preview = img.resize((256, 256), Image.Resampling.LANCZOS)
    preview.save(OUT_PNG, format="PNG")

    # Pillow's ICO writer produces one embedded frame for each requested size.
    img.save(OUT_ICO, format="ICO", sizes=[(size, size) for size in ICO_SIZES])
    write_android_icons(img)
    print(f"wrote {OUT_ICO} and {OUT_PNG}")
    print(f"wrote Android launcher icons under {ANDROID_RES}")
    print(f"ico sizes: {', '.join(f'{size}x{size}' for size in ICO_SIZES)}")
    print(f"colors: ink={INK} paper={PAPER} sage={SAGE} core={CORE}")


if __name__ == "__main__":
    main()
