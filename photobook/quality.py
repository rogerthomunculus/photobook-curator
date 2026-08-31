"""Stage 1 — technical quality. Cheap, deterministic, explainable.

Two things here matter more than the choice of metric:

1. Sharpness is measured **per tile**, and the subject score is the 90th
   percentile tile rather than the frame mean. A global blur metric rejects a
   good shallow-depth-of-field portrait and happily accepts a sharp background
   behind a blurry subject.
2. On a Storage Saver archive every file was already re-encoded once by Google,
   so recompression damage is as important as sharpness. It is estimated from
   the JPEG quantization tables, which are readable straight from the file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

Image.MAX_IMAGE_PIXELS = None

# ITU/IJG Annex K luminance table — the reference the encoder scales.
STD_LUMA = np.array([
    [16, 11, 10, 16, 24, 40, 51, 61],
    [12, 12, 14, 19, 26, 58, 60, 55],
    [14, 13, 16, 24, 40, 57, 69, 56],
    [14, 17, 22, 29, 51, 87, 80, 62],
    [18, 22, 37, 56, 68, 109, 103, 77],
    [24, 35, 55, 64, 81, 104, 113, 92],
    [49, 64, 78, 87, 103, 121, 120, 101],
    [72, 92, 95, 98, 112, 100, 103, 99],
], dtype=np.float64)

METRIC_LONG_EDGE = 1024  # fixed size so scores are comparable across the set

# Placement sizes as a fraction of the page's long edge.
PLACEMENT_FRACTION = {"full_bleed": 1.0, "half": 0.7, "quarter": 0.5}
DPI_FLOOR = 200          # below this a placement is not worth printing
DPI_COMFORTABLE = 240

# Sharpness verdicts are relative to this archive's own median, not absolute.
#
# The first version used absolute thresholds tuned on synthetic images and was
# wrong by three orders of magnitude on real photographs: a heavily blurred but
# textured frame scored ~18,000 against a reject threshold of 12. Real photos
# almost always have fine detail *somewhere* — foliage, gravel, fabric — so the
# 90th-percentile tile mostly answers "is there any texture at all", which is
# yes for everything.
#
# A ratio to the median is scale-free, so it survives a change of camera, lens
# or subject matter. It is also better than a percentile rule, which would
# always condemn a fixed share of the archive even when every photo is fine.
SHARP_REJECT_RATIO = 0.20
SHARP_REVIEW_RATIO = 0.45


@dataclass
class Metrics:
    width: int
    height: int
    laplacian_var: float      # frame mean — the naive number
    tenengrad: float
    subject_sharpness: float  # 90th-percentile tile — the one that matters
    sharp_tile_ratio: float
    clipped_high: float
    clipped_low: float
    brightness: float
    contrast: float
    jpeg_quality: float       # estimated encoder quality, 0-100; -1 if not JPEG
    blockiness: float
    banding: float
    max_in_300: float
    max_in_240: float
    max_in_200: float
    placement_cap: str
    verdict: str = "unscored"   # assigned later, by score_archive()
    reasons: str = ""


def estimate_jpeg_quality(img: Image.Image) -> float:
    """Recover the encoder's quality setting from the quantization tables.

    The IJG encoder scales the reference table by `scale`, where
    `scale = 5000/Q` for Q < 50 and `200 - 2Q` otherwise. Inverting per
    coefficient and taking the median is robust to the clamping that happens
    at the extremes of the table.
    """
    q = getattr(img, "quantization", None)
    if not q:
        return -1.0
    luma = np.array(q[0], dtype=np.float64)
    if luma.size != 64:
        return -1.0
    luma = luma.reshape(8, 8)
    # Ignore coefficients that clamped to 1 or 255 — they carry no scale info.
    usable = (luma > 1) & (luma < 255)
    if usable.sum() < 8:
        return 100.0 if luma.mean() <= 1.5 else 1.0
    scales = 100.0 * luma[usable] / STD_LUMA[usable]
    scale = float(np.median(scales))
    if scale <= 0:
        return 100.0
    q_est = 5000.0 / scale if scale > 100.0 else (200.0 - scale) / 2.0
    return float(np.clip(q_est, 1.0, 100.0))


def _gray(img: Image.Image) -> np.ndarray:
    w, h = img.size
    s = METRIC_LONG_EDGE / max(w, h)
    if s < 1.0:
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR)
    return np.asarray(img.convert("L"), dtype=np.float64)


def _normalise(g: np.ndarray) -> np.ndarray:
    """Contrast-normalise before measuring sharpness.

    Gradient magnitude scales with contrast, so an underexposed frame reads as
    blurry to a raw Laplacian even when it is perfectly sharp. Normalising to a
    fixed standard deviation separates the two questions: this function answers
    "is it in focus", and the exposure metrics answer "is it too dark".
    """
    sd = float(np.std(g))
    if sd < 1e-6:
        return np.full_like(g, 128.0)
    return (g - float(np.mean(g))) * (50.0 / sd) + 128.0


def _laplacian(g: np.ndarray) -> np.ndarray:
    return (g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4 * g[1:-1, 1:-1])


def _tenengrad(g: np.ndarray) -> float:
    gx = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[1:-1, :-2] + g[2:, :-2])
    gy = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[:-2, 1:-1] + g[:-2, 2:])
    return float(np.mean(gx * gx + gy * gy))


def _tiled_sharpness(lap: np.ndarray, tiles: int = 8) -> tuple[float, float]:
    """Per-tile Laplacian variance -> (90th percentile, share of sharp tiles).

    This is the shallow-depth-of-field fix: a portrait with a crisp face over a
    soft background scores low on the mean and high here, which is correct.
    """
    h, w = lap.shape
    th, tw = max(1, h // tiles), max(1, w // tiles)
    vals = [
        float(np.var(lap[y:y + th, x:x + tw]))
        for y in range(0, h - th + 1, th)
        for x in range(0, w - tw + 1, tw)
    ]
    if not vals:
        return 0.0, 0.0
    a = np.array(vals)
    p90 = float(np.percentile(a, 90))
    return p90, float(np.mean(a > 0.25 * p90)) if p90 > 0 else 0.0


def _blockiness(g: np.ndarray) -> float:
    """Discontinuity across 8-pixel boundaries relative to within-block change.

    ~1.0 means no visible blocking; higher means the JPEG grid is showing.
    """
    d = np.abs(np.diff(g, axis=1))
    if d.shape[1] < 16:
        return 1.0
    cols = np.arange(d.shape[1])
    on = d[:, cols % 8 == 7]
    off = d[:, cols % 8 != 7]
    if off.size == 0 or off.mean() < 1e-6:
        return 1.0
    return float(on.mean() / off.mean())


def _banding(g: np.ndarray) -> float:
    """Plateau ratio in smooth regions — the signature of posterised gradients."""
    d = np.abs(np.diff(g, axis=1))
    smooth = d < 3.0
    if smooth.sum() < 100:
        return 0.0
    return float(np.mean(d[smooth] == 0.0))


def _placement(long_px: int, page_long_in: float, jpeg_q: float,
               blockiness: float) -> tuple[str, list[str]]:
    notes: list[str] = []
    cap = "reject"
    for name in ("full_bleed", "half", "quarter"):
        need_in = page_long_in * PLACEMENT_FRACTION[name]
        if long_px / need_in >= DPI_FLOOR:
            cap = name
            break
    # Recompression damage is magnified by size, so demote a tier when it is bad.
    if cap != "reject" and 0 <= jpeg_q < 55:
        order = ["full_bleed", "half", "quarter", "reject"]
        cap = order[min(order.index(cap) + 1, 3)]
        notes.append(f"demoted: heavy recompression (q≈{jpeg_q:.0f})")
    if cap == "full_bleed" and blockiness > 1.35:
        cap = "half"
        notes.append(f"demoted: JPEG blocking visible ({blockiness:.2f})")
    return cap, notes


def analyze(path: Path, page_long_in: float = 14.0) -> Metrics:
    with Image.open(path) as im:
        w, h = im.size                      # full size, before any draft scaling
        jpeg_q = estimate_jpeg_quality(im)  # quantization tables live in the header
        # Ask libjpeg to decode straight to roughly the size we measure at.
        # No-op for formats that do not support it.
        im.draft("L", (METRIC_LONG_EDGE, METRIC_LONG_EDGE))
        im.load()
        g = _gray(im)

    gn = _normalise(g)
    lap = _laplacian(gn)
    lap_var = float(np.var(lap))
    subj, sharp_ratio = _tiled_sharpness(lap)
    ten = _tenengrad(gn)
    hi = float(np.mean(g >= 250.0))
    lo = float(np.mean(g <= 5.0))
    bright = float(np.mean(g))
    contrast = float(np.std(g))
    block = _blockiness(g)
    band = _banding(g)

    long_px = max(w, h)
    cap, notes = _placement(long_px, page_long_in, jpeg_q, block)

    return Metrics(
        width=w, height=h,
        laplacian_var=lap_var, tenengrad=ten,
        subject_sharpness=subj, sharp_tile_ratio=sharp_ratio,
        clipped_high=hi, clipped_low=lo, brightness=bright, contrast=contrast,
        jpeg_quality=jpeg_q, blockiness=block, banding=band,
        max_in_300=long_px / 300.0, max_in_240=long_px / DPI_COMFORTABLE,
        max_in_200=long_px / DPI_FLOOR,
        placement_cap=cap, reasons="; ".join(notes),
    )


def as_row(m: Metrics) -> dict:
    return asdict(m)


# --------------------------------------------------------------------------
# Verdicts, assigned across the whole archive once every photo is measured.

def score_archive(rows: list[dict], *,
                  reject_ratio: float = SHARP_REJECT_RATIO,
                  review_ratio: float = SHARP_REVIEW_RATIO) -> dict[str, tuple[str, str]]:
    """Assign a verdict to every photo, relative to this archive.

    `rows` needs `sha`, `subject_sharpness`, `clipped_high`, `clipped_low`,
    `brightness`, `contrast`, `placement_cap`, `jpeg_quality`, `banding`,
    `width`, `height`, `camera`, and any notes from the measurement pass.
    Returns sha -> (verdict, reasons).

    Separating this from measurement means re-scoring is free — no image is
    decoded again — so thresholds can be swept against the real distribution
    instead of guessed once.
    """
    sharp = [r["subject_sharpness"] for r in rows
             if r.get("subject_sharpness") is not None]
    median = float(np.median(sharp)) if sharp else 0.0
    out: dict[str, tuple[str, str]] = {}

    for r in rows:
        reasons = [x for x in [r.get("reasons") or ""] if x]
        verdict = "keep"

        def flag(level: str, why: str) -> None:
            nonlocal verdict
            reasons.append(why)
            if level == "reject" or (level == "review" and verdict == "keep"):
                verdict = level

        s_val = r.get("subject_sharpness")
        if median > 0 and s_val is not None:
            rel = s_val / median
            if rel < reject_ratio:
                flag("reject", f"out of focus ({rel:.0%} of this trip's median sharpness)")
            elif rel < review_ratio:
                flag("review", f"soft ({rel:.0%} of median sharpness)")

        # Exposure thresholds stay absolute: clipping and mean level are
        # physically meaningful and do not depend on the archive.
        if (v := r.get("clipped_high") or 0) > 0.18:
            flag("review", f"{v * 100:.0f}% blown highlights")
        if (v := r.get("clipped_low") or 0) > 0.35:
            flag("review", f"{v * 100:.0f}% crushed shadows")
        if (v := r.get("brightness")) is not None:
            if v < 45:
                flag("review", f"underexposed (mean level {v:.0f})")
            elif v > 215:
                flag("review", f"overexposed (mean level {v:.0f})")
        if (v := r.get("contrast")) is not None and v < 12:
            flag("review", "very flat contrast")

        if r.get("placement_cap") == "reject":
            flag("reject", f'too small to print ({r.get("width")}×{r.get("height")})')

        # A screenshot has no camera in EXIF and a screen-shaped aspect ratio.
        w, h = r.get("width") or 0, r.get("height") or 0
        if w and h and not r.get("camera"):
            ratio = max(w, h) / min(w, h)
            if ratio > 1.9:
                flag("review", "probably a screenshot (no camera EXIF, screen aspect)")

        jq = r.get("jpeg_quality")
        if jq is not None and 0 <= jq < 40:
            reasons.append(f"low encoder quality (q≈{jq:.0f})")

        out[r["sha"]] = (verdict, "; ".join(reasons))
    return out
