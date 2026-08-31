#!/usr/bin/env python3
"""Generate a print test target for the free (Tier 0) vendor validation.

Upload this as a single full-bleed page image and you learn, for nothing:

  * whether the vendor crops where it says it does      -> the trim ruler
  * whether it resamples or softens your pixels          -> resolution wedges
  * whether auto-enhance re-grades your page             -> grey ramp + patches
  * what each effective DPI actually looks like on paper -> the DPI ladder

Upload it twice, once with auto-enhance on and once off, and compare the
on-screen previews at maximum zoom. Differences visible there are differences
you never have to pay to discover.

    python tools/make_test_target.py --trim 8.5x8.5 --bleed 0.125 --out target.png

Output is PNG by default and deliberately so: re-encoding a test target as JPEG
would add exactly the artifacts the target exists to detect.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_DIRS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
             "/System/Library/Fonts/Helvetica.ttc",
             "C:/Windows/Fonts/arial.ttf")
MONO_DIRS = ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf")

INK = (26, 26, 26)
MID = (128, 128, 128)
RULE = (90, 90, 90)

# Representative skin tones across the range, sRGB. Auto-enhance shifts these
# first and most visibly, which is exactly why they are here.
SKIN = [(255, 224, 196), (240, 200, 168), (222, 176, 140), (198, 148, 110),
        (166, 118, 84), (128, 88, 62), (94, 63, 45), (64, 42, 30)]
PRIMARY = [(200, 40, 40), (40, 140, 70), (40, 80, 180), (230, 190, 40),
           (190, 60, 150), (40, 170, 190)]


def _font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont:
    for p in (MONO_DIRS if mono else FONT_DIRS):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _label(d: ImageDraw.ImageDraw, xy, text, size=28, fill=INK, mono=False, anchor="la"):
    d.text(xy, text, font=_font(size, mono), fill=fill, anchor=anchor)


def resolution_wedge(w: int, h: int, dpi: int) -> Image.Image:
    """Converging line pairs. Where they smear into grey is the effective limit."""
    a = np.zeros((h, w), dtype=np.float64)
    xs = np.arange(w)
    # Line-pair frequency sweeps from 2 lp/mm-ish to well past what print resolves.
    lo, hi = 1.0, dpi / 6.0
    freq = lo * (hi / lo) ** (xs / max(1, w - 1))       # cycles per inch
    phase = np.cumsum(freq / dpi) * 2 * np.pi
    a[:] = (np.sin(phase)[None, :] * 0.5 + 0.5) * 255
    return Image.fromarray(a.astype(np.uint8)).convert("RGB")


def dpi_ladder(size: int, dpi: int, effective: list[int]) -> list[Image.Image]:
    """The same detailed patch resampled to each effective DPI, then back up.

    Shows what 300 / 240 / 200 / 150 DPI actually look like on their paper —
    the number the layout engine's placement cap turns on. The patch mixes a
    radial chirp, hairlines and small type, because those are what degrade
    visibly; a smooth gradient would look identical at every density and prove
    nothing.
    """
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    cx = cy = size / 2.0
    r = np.hypot(xx - cx, yy - cy)
    # Zone plate: spatial frequency rises with radius, so the exact radius where
    # it turns to mush is a direct read-out of resolving power.
    chirp = 128 + 110 * np.cos(np.pi * (r ** 2) / (size * 0.62))
    base = Image.fromarray(np.clip(chirp, 0, 255).astype(np.uint8)).convert("RGB")

    d = ImageDraw.Draw(base)
    d.rectangle([0, 0, size - 1, int(size * 0.30)], fill=(255, 255, 255))
    d.rectangle([0, int(size * 0.72), size - 1, size - 1], fill=(255, 255, 255))
    for i in range(0, int(size * 0.28), 3):            # 1px hairlines
        d.line([i, 0, i, int(size * 0.14)], fill=(0, 0, 0), width=1)
    ty = int(size * 0.16)
    for pt in (7, 5, 4):                                # small type
        f = _font(max(6, int(pt / 72 * dpi)))
        d.text((5, ty), "Handgloves 0123", font=f, fill=(20, 20, 20))
        ty += int(f.size * 1.45)
    d.text((4, int(size * 0.75)), "hairlines · chirp · type",
           font=_font(max(6, int(6 / 72 * dpi))), fill=(90, 90, 90))

    out = []
    for eff in effective:
        small = max(8, int(round(size * eff / dpi)))
        out.append(base.resize((small, small), Image.LANCZOS)
                       .resize((size, size), Image.LANCZOS))
    return out


def build(trim_w: float, trim_h: float, bleed: float, dpi: int, label: str) -> Image.Image:
    W = int(round((trim_w + 2 * bleed) * dpi))
    H = int(round((trim_h + 2 * bleed) * dpi))
    B = int(round(bleed * dpi))
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    inch = dpi

    # --- bleed band: solid so any white edge on the print means under-trimming
    d.rectangle([0, 0, W - 1, H - 1], fill=(232, 236, 232))
    d.rectangle([B, B, W - B - 1, H - B - 1], fill=(255, 255, 255))
    d.rectangle([B, B, W - B - 1, H - B - 1], outline=RULE, width=max(1, dpi // 300))

    # --- trim ruler: ticks every 1/8", labelled every inch, from the trim edge.
    #     Whatever number sits at the paper edge is how much they actually cut.
    for i in range(int(trim_w * 8) + 1):
        x = B + int(i * inch / 8)
        long = (i % 8 == 0)
        d.line([x, B, x, B + (int(0.22 * inch) if long else int(0.10 * inch))],
               fill=RULE, width=2)
        if long:
            _label(d, (x + 6, B + int(0.24 * inch)), f'{i // 8}"', size=int(0.10 * inch),
                   fill=RULE, mono=True)
    for i in range(int(trim_h * 8) + 1):
        y = B + int(i * inch / 8)
        long = (i % 8 == 0)
        d.line([B, y, B + (int(0.22 * inch) if long else int(0.10 * inch)), y],
               fill=RULE, width=2)

    pad = B + int(0.42 * inch)
    x0, x1 = pad, W - pad
    y = pad

    _label(d, (x0, y), "PRINT TEST TARGET", size=int(0.20 * inch))
    y += int(0.26 * inch)
    _label(d, (x0, y),
           f'{trim_w:g}×{trim_h:g}" trim + {bleed:g}" bleed · {dpi} dpi · {label}',
           size=int(0.11 * inch), fill=MID, mono=True)
    y += int(0.30 * inch)

    # --- 1. resolution wedges
    _label(d, (x0, y), "1 · RESOLUTION — where the lines smear to grey is the real limit",
           size=int(0.105 * inch))
    y += int(0.16 * inch)
    wedge_h = int(0.55 * inch)
    img.paste(resolution_wedge(x1 - x0, wedge_h, dpi), (x0, y))
    d.rectangle([x0, y, x1 - 1, y + wedge_h - 1], outline=RULE)
    y += wedge_h + int(0.06 * inch)
    for frac, txt in ((0.0, "coarse"), (0.5, "→"), (0.97, f"{dpi // 6} lp/in")):
        _label(d, (x0 + int((x1 - x0) * frac), y), txt, size=int(0.085 * inch),
               fill=MID, mono=True, anchor="ra" if frac > 0.9 else "la")
    y += int(0.26 * inch)

    # --- 2. DPI ladder
    _label(d, (x0, y), "2 · EFFECTIVE DPI — what the placement cap actually buys you",
           size=int(0.105 * inch))
    y += int(0.16 * inch)
    effective = [300, 240, 200, 150]
    patch = int(0.95 * inch)
    gap = int(0.14 * inch)
    for i, (p, eff) in enumerate(zip(dpi_ladder(patch, dpi, effective), effective)):
        px = x0 + i * (patch + gap)
        img.paste(p, (px, y))
        d.rectangle([px, y, px + patch - 1, y + patch - 1], outline=RULE)
        _label(d, (px, y + patch + int(0.04 * inch)), f"{eff} dpi",
               size=int(0.09 * inch), fill=MID, mono=True)
    y += patch + int(0.28 * inch)

    # --- 3. tone: smooth ramp (banding) over a stepped wedge (tone curve)
    _label(d, (x0, y), "3 · TONE — smooth ramp shows banding; steps show any re-grading",
           size=int(0.105 * inch))
    y += int(0.16 * inch)
    ramp_h = int(0.42 * inch)
    ramp = np.linspace(0, 255, x1 - x0)[None, :].repeat(ramp_h, 0)
    img.paste(Image.fromarray(ramp.astype(np.uint8)).convert("RGB"), (x0, y))
    y += ramp_h
    steps, step_h = 16, int(0.38 * inch)
    for i in range(steps):
        v = int(round(255 * i / (steps - 1)))
        sx0 = x0 + int((x1 - x0) * i / steps)
        sx1 = x0 + int((x1 - x0) * (i + 1) / steps)
        d.rectangle([sx0, y, sx1 - 1, y + step_h], fill=(v, v, v))
        _label(d, ((sx0 + sx1) // 2, y + step_h + int(0.03 * inch)), str(v),
               size=int(0.072 * inch), fill=MID, mono=True, anchor="ma")
    d.rectangle([x0, y - ramp_h, x1 - 1, y + step_h], outline=RULE)
    y += step_h + int(0.26 * inch)

    # --- 4. colour: skin tones first, because they shift first
    _label(d, (x0, y), "4 · COLOUR — skin tones shift first under auto-enhance",
           size=int(0.105 * inch))
    y += int(0.16 * inch)
    sw = (x1 - x0) // len(SKIN)
    sh = int(0.60 * inch)
    for i, c in enumerate(SKIN):
        d.rectangle([x0 + i * sw, y, x0 + (i + 1) * sw - 1, y + sh], fill=c)
        _label(d, (x0 + i * sw + 6, y + sh - int(0.16 * inch)),
               "%02X%02X%02X" % c, size=int(0.065 * inch),
               fill=(255, 255, 255) if sum(c) < 380 else (40, 40, 40), mono=True)
    y += sh
    pw = (x1 - x0) // len(PRIMARY)
    for i, c in enumerate(PRIMARY):
        d.rectangle([x0 + i * pw, y, x0 + (i + 1) * pw - 1, y + int(0.34 * inch)], fill=c)
    d.rectangle([x0, y - sh, x1 - 1, y + int(0.34 * inch)], outline=RULE)
    y += int(0.34 * inch) + int(0.26 * inch)

    # --- 5. fine detail: 1px grid and a type ladder
    _label(d, (x0, y), "5 · FINE DETAIL — a soft grid or fuzzy small type means resampling",
           size=int(0.105 * inch))
    y += int(0.16 * inch)
    grid = int(1.15 * inch)
    g = Image.new("RGB", (grid, grid), (255, 255, 255))
    gd = ImageDraw.Draw(g)
    for i in range(0, grid, 4):
        gd.line([i, 0, i, grid], fill=(0, 0, 0), width=1)
        gd.line([0, i, grid, i], fill=(0, 0, 0), width=1)
    img.paste(g, (x0, y))
    d.rectangle([x0, y, x0 + grid - 1, y + grid - 1], outline=RULE)
    _label(d, (x0, y + grid + int(0.04 * inch)), "1px grid, 4px pitch",
           size=int(0.085 * inch), fill=MID, mono=True)

    ty = y
    tx = x0 + grid + int(0.35 * inch)
    for pt in (14, 11, 9, 7, 6, 5):
        _label(d, (tx, ty), f"{pt}pt  the quick brown fox jumps over the lazy dog 0123456789",
               size=int(pt / 72 * inch))
        ty += int(pt / 72 * inch * 1.7)
    y += grid + int(0.30 * inch)

    # --- footer: what to compare
    _label(d, (x0, y),
           "Upload twice — auto-enhance ON and OFF — and compare previews at max zoom.",
           size=int(0.10 * inch))
    y += int(0.15 * inch)
    _label(d, (x0, y),
           "Differences in 3 or 4 mean the vendor re-grades your pages; "
           "softening in 1 or 5 means it resamples them.",
           size=int(0.09 * inch), fill=MID)

    # --- corner registration marks, sitting exactly on the trim line
    m = int(0.22 * inch)
    for cx, cy, dx, dy in ((B, B, 1, 1), (W - B, B, -1, 1),
                           (B, H - B, 1, -1), (W - B, H - B, -1, -1)):
        d.line([cx, cy, cx + dx * m, cy], fill=INK, width=3)
        d.line([cx, cy, cx, cy + dy * m], fill=INK, width=3)
    return img


def parse_size(text: str) -> tuple[float, float]:
    a, _, b = text.lower().partition("x")
    return float(a), float(b or a)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trim", default="8.5x8.5", help='page trim in inches, e.g. 11x14')
    ap.add_argument("--bleed", type=float, default=0.125, help="bleed in inches")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--label", default="", help="vendor name, printed on the page")
    ap.add_argument("--out", default="print-test-target.png")
    a = ap.parse_args()

    tw, th = parse_size(a.trim)
    img = build(tw, th, a.bleed, a.dpi, a.label or "unlabelled")
    out = Path(a.out)
    if out.suffix.lower() in (".jpg", ".jpeg"):
        img.save(out, "JPEG", quality=100, subsampling=0)
    else:
        img.save(out, "PNG")
    print(f"{out}  {img.width}×{img.height}px  "
          f"({tw + 2 * a.bleed:g}×{th + 2 * a.bleed:g}\" at {a.dpi} dpi)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
