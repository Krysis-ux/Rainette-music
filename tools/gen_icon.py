"""Render Rainette Music's raindrop app mark and bundled icon assets.

The artwork is intentionally built from geometry instead of a platform font so
the output remains deterministic and the silhouette stays clear at Windows'
smallest icon sizes. Run this script whenever the icon design changes.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent.parent / "web" / "assets"
OUT_ICO = OUT_DIR / "rainette-icon.ico"
OUT_PNG = OUT_DIR / "rainette-icon-256.png"

CANVAS = 1024  # supersampled, then downsampled for crisp edges at small sizes
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def oklch_to_srgb(l: float, c: float, h_deg: float) -> tuple[int, int, int]:
    h = math.radians(h_deg)
    a = c * math.cos(h)
    b = c * math.sin(h)

    l_ = l + 0.3963377774 * a + 0.2158037573 * b
    m_ = l - 0.1055613458 * a - 0.0638541728 * b
    s_ = l - 0.0894841775 * a - 1.2914855480 * b

    l3, m3, s3 = l_**3, m_**3, s_**3

    r_lin = 4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3
    g_lin = -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3
    b_lin = -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3

    def to_srgb(c_lin: float) -> int:
        c_lin = max(0.0, min(1.0, c_lin))
        c_srgb = c_lin * 12.92 if c_lin <= 0.0031308 else 1.055 * (c_lin ** (1 / 2.4)) - 0.055
        return round(max(0.0, min(1.0, c_srgb)) * 255)

    return to_srgb(r_lin), to_srgb(g_lin), to_srgb(b_lin)


# Matches web/rainette_tokens.css :root values.
ACCENT = oklch_to_srgb(0.64, 0.120, 38)
ACCENT_STRONG = oklch_to_srgb(0.50, 0.130, 35)
ON_ACCENT = oklch_to_srgb(0.99, 0.010, 80)


def lerp_color(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


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


def raindrop_points() -> list[tuple[float, float]]:
    """Return a broad, balanced drop that survives reduction to 16 px."""
    top = (CANVAS * 0.5, CANVAS * 0.17)
    bottom = (CANVAS * 0.5, CANVAS * 0.82)
    right = cubic_points(
        top,
        (CANVAS * 0.55, CANVAS * 0.25),
        (CANVAS * 0.72, CANVAS * 0.43),
        (CANVAS * 0.72, CANVAS * 0.59),
    )
    right_bottom = cubic_points(
        right[-1],
        (CANVAS * 0.72, CANVAS * 0.73),
        (CANVAS * 0.62, CANVAS * 0.82),
        bottom,
    )
    left_bottom = cubic_points(
        bottom,
        (CANVAS * 0.38, CANVAS * 0.82),
        (CANVAS * 0.28, CANVAS * 0.73),
        (CANVAS * 0.28, CANVAS * 0.59),
    )
    left = cubic_points(
        left_bottom[-1],
        (CANVAS * 0.28, CANVAS * 0.43),
        (CANVAS * 0.45, CANVAS * 0.25),
        top,
    )
    return [top, *right, *right_bottom, *left_bottom, *left]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))

    # Diagonal gradient background (matches the app's own
    # linear-gradient(160deg, --rw-accent, --rw-accent-strong) treatment
    # used for the play button/pill), inside a squircle-ish rounded square.
    grad = Image.new("RGBA", (CANVAS, CANVAS))
    for y in range(CANVAS):
        t = y / (CANVAS - 1)
        row_color = lerp_color(ACCENT, ACCENT_STRONG, t)
        grad.paste(row_color + (255,), (0, y, CANVAS, y + 1))

    mask = Image.new("L", (CANVAS, CANVAS), 0)
    mask_draw = ImageDraw.Draw(mask)
    radius = round(CANVAS * 0.225)  # squircle-like corner radius
    mask_draw.rounded_rectangle([0, 0, CANVAS - 1, CANVAS - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    # A single high-contrast silhouette remains legible in the 16 px ICO frame.
    draw = ImageDraw.Draw(img)
    draw.polygon(raindrop_points(), fill=ON_ACCENT + (255,))

    preview = img.resize((256, 256), Image.Resampling.LANCZOS)
    preview.save(OUT_PNG, format="PNG")

    # Pillow's ICO writer produces one embedded frame for each requested size.
    img.save(OUT_ICO, format="ICO", sizes=[(size, size) for size in ICO_SIZES])
    print(f"wrote {OUT_ICO} and {OUT_PNG}")
    print(f"ico sizes: {', '.join(f'{size}x{size}' for size in ICO_SIZES)}")
    print(f"colors: accent={ACCENT} accent_strong={ACCENT_STRONG} on_accent={ON_ACCENT}")


if __name__ == "__main__":
    main()
