#!/usr/bin/env python3
"""Storydump's app icon: the PNGs Meta App Review and the Google OAuth consent
screen ask for, drawn from the site's own colour tokens. Needs Pillow: the
committed files are what 12.2 and 12.3 write byte for byte, and 9.4 draws the
edges differently.

    python3 documentation/operations/assets/app-icon/make-icon.py [out-dir]

writes, next to this file unless an out-dir is given:

- storydump-icon-1024.png: Meta's app icon (1024x1024).
- storydump-icon-120.png: Google's consent-screen logo (120x120).

Both runbooks ask for no transparency (neither provider's docs say either
way), so the image is RGB with no alpha channel. No corners are drawn in, so
each platform is free to round or crop it.

The mark is three Story frames in a receding deck, a 9:16 stack, which is what
the product schedules. It has no text and no font, so it renders the same on
any machine and still reads at 120px. The branding job proper is #304.

The colours are the site's achromatic tokens (`landing/src/app/globals.css`),
converted from OKLCH here rather than typed as hex, so a token change is a
one-line edit.
"""

import pathlib
import sys

from PIL import Image, ImageDraw

#: OKLCH lightness of the three tokens the mark uses (chroma 0 throughout).
PRIMARY = 0.205  # --primary: the ground
PRIMARY_FOREGROUND = 0.985  # --primary-foreground: the frame in front
MUTED_FOREGROUND = 0.556  # --muted-foreground: the two frames behind it

#: Drawn at this multiple of the output size, then downsampled, for clean edges.
SUPERSAMPLE = 8

SIZES = {
    1024: "storydump-icon-1024.png",  # Meta App Review: the app icon
    120: "storydump-icon-120.png",  # Google OAuth consent screen: the logo
}


def oklch_gray(lightness: float) -> tuple[int, int, int]:
    """An achromatic OKLCH colour as 8-bit sRGB: L cubed is linear luminance."""
    y = lightness**3
    v = 12.92 * y if y <= 0.0031308 else 1.055 * y ** (1 / 2.4) - 0.055
    g = round(255 * v)
    return (g, g, g)


def render(size: int) -> Image.Image:
    ground = oklch_gray(PRIMARY)
    s = size * SUPERSAMPLE
    img = Image.new("RGB", (s, s), ground)
    d = ImageDraw.Draw(img)
    cx = cy = s / 2
    # The front frame is 66% of the height, at 9:16. The two behind it are the
    # same shape at 82% of its size, shifted 21.5% of the width left and right.
    h = s * 0.66
    w = h * 9 / 16
    shift = s * 0.215
    back = 0.82
    for side in (-1, 1):
        bx = cx + side * shift
        d.rounded_rectangle(
            [
                bx - w * back / 2,
                cy - h * back / 2,
                bx + w * back / 2,
                cy + h * back / 2,
            ],
            radius=w * back * 0.22,
            fill=oklch_gray(MUTED_FOREGROUND),
        )
    # A margin of ground around the front frame keeps the three apart where
    # they overlap, which is what makes them read at 120px.
    pad = s * 0.018
    d.rounded_rectangle(
        [cx - w / 2 - pad, cy - h / 2 - pad, cx + w / 2 + pad, cy + h / 2 + pad],
        radius=(w / 2 + pad) * 0.30,
        fill=ground,
    )
    d.rounded_rectangle(
        [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
        radius=w * 0.22,
        fill=oklch_gray(PRIMARY_FOREGROUND),
    )
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    here = pathlib.Path(__file__).parent
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else here
    out.mkdir(parents=True, exist_ok=True)
    for size, name in SIZES.items():
        path = out / name
        render(size).save(path, "PNG", optimize=True)
        with Image.open(path) as im:
            print(f"{name}: {im.size[0]}x{im.size[1]} {im.mode}")


if __name__ == "__main__":
    main()
