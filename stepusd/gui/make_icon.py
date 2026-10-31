"""Generate the application icon.

A wireframe-over-solid cylinder: the thing the tool is actually about -- CAD
turned into a controlled mesh. Drawn at 4x and downsampled so the thin wires
stay clean at 16px. Run this to regenerate assets/appicon.ico and .png.
"""
from __future__ import annotations
import math
import os

ASSETS = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "assets")

BG_A = (37, 41, 48)
BG_B = (24, 27, 32)
FACE_LIT = (176, 186, 199)
FACE_DIM = (104, 114, 128)
WIRE = (18, 20, 24)
ACCENT = (92, 190, 214)


def _shade(t: float):
    """Cylinder shading: bright a third of the way round, falling off both ways."""
    k = max(0.0, math.cos((t - 0.32) * math.pi * 1.15))
    k = 0.22 + 0.78 * (k ** 1.4)
    return tuple(int(FACE_DIM[i] + (FACE_LIT[i] - FACE_DIM[i]) * k) for i in range(3))


def build(size: int = 512):
    from PIL import Image, ImageDraw
    S = size
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded background with a soft vertical gradient
    r = int(S * 0.22)
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bg)
    for y in range(S):
        t = y / (S - 1)
        bd.line([(0, y), (S, y)],
                fill=tuple(int(BG_A[i] + (BG_B[i] - BG_A[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], r, fill=255)
    img.paste(bg, (0, 0), mask)

    # cylinder geometry
    cx, cy = S * 0.5, S * 0.53
    rx, ry = S * 0.30, S * 0.115          # ellipse radii for the caps
    half = S * 0.20                        # half height
    seg = 16

    def pt(i, top):
        a = 2 * math.pi * i / seg
        return (cx + rx * math.cos(a), (cy - half if top else cy + half) + ry * math.sin(a))

    # body quads, back to front so shading reads correctly
    order = sorted(range(seg), key=lambda i: math.sin(2 * math.pi * i / seg))
    for i in order:
        j = (i + 1) % seg
        quad = [pt(i, True), pt(j, True), pt(j, False), pt(i, False)]
        t = ((i + 0.5) / seg)
        d.polygon(quad, fill=_shade(t) + (255,), outline=WIRE + (255,), width=max(1, S // 170))

    # top cap
    top = [pt(i, True) for i in range(seg)]
    d.polygon(top, fill=(198, 208, 220, 255), outline=WIRE + (255,), width=max(1, S // 170))
    for i in range(0, seg, 2):                      # a few radial wires on the cap
        d.line([(cx, cy - half), top[i]], fill=WIRE + (170,), width=max(1, S // 260))

    # accent: one axial edge picked out, the whole point of the tessellator
    d.line([pt(4, True), pt(4, False)], fill=ACCENT + (255,), width=max(2, S // 90))
    d.line([pt(5, True), pt(5, False)], fill=ACCENT + (255,), width=max(2, S // 90))
    return img


def main():
    from PIL import Image
    os.makedirs(ASSETS, exist_ok=True)
    big = build(512)
    png = os.path.join(ASSETS, "appicon.png")
    big.save(png)
    ico = os.path.join(ASSETS, "appicon.ico")
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    big.save(ico, format="ICO", sizes=sizes)
    print(f"wrote {png}")
    print(f"wrote {ico}  ({', '.join(f'{w}x{h}' for w, h in sizes)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
