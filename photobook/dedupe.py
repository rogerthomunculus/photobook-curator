"""Stage 2 — near-duplicate detection and burst grouping.

1,200 frames collapse to roughly 500 distinct *moments*. Everything downstream
works on moments, and the discarded frames are kept as alternates so the review
UI can offer them as one-click swaps.

A burst is a run of frames that are close in *both* time and appearance. Time
alone groups a museum room; appearance alone groups every photo of the same
building across three days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

BURST_SECONDS = 25.0     # max gap between consecutive frames in one burst
BURST_MAX_SPAN = 90.0    # max total span of a burst, to stop transitive chaining
BURST_DISTANCE = 14      # max Hamming distance from the burst anchor (64-bit hash)


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)
    m = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    m[0] *= np.sqrt(0.5)
    return m * np.sqrt(2.0 / n)


_DCT32 = _dct_matrix(32)


def dhash(img: Image.Image, size: int = 8) -> int:
    """Gradient hash: compare each pixel to its right-hand neighbour."""
    g = np.asarray(img.convert("L").resize((size + 1, size), Image.BILINEAR), dtype=np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def phash(img: Image.Image) -> int:
    """DCT hash: low-frequency structure, robust to scale and mild edits."""
    g = np.asarray(img.convert("L").resize((32, 32), Image.BILINEAR), dtype=np.float64)
    d = _DCT32 @ g @ _DCT32.T
    low = d[:8, :8].flatten()
    med = np.median(low[1:])  # skip DC, which only encodes overall brightness
    bits = low > med
    return int("".join("1" if b else "0" for b in bits), 2)


def hashes(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        im.draft("L", (64, 64))   # both hashes work from a 32x32; decode small
        im.load()
        return dhash(im), phash(im)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass
class Frame:
    sha: str
    taken: datetime | None
    dhash: int
    phash: int
    score: float = 0.0   # quality score; the best in a burst represents it


def group_bursts(frames: list[Frame], *, seconds: float = BURST_SECONDS,
                 distance: int = BURST_DISTANCE,
                 max_span: float = BURST_MAX_SPAN) -> dict[str, tuple[int, bool]]:
    """Group frames into bursts. Returns sha -> (burst_id, is_representative).

    A sequential pass, not transitive clustering. Union-find looks natural here
    and is wrong: with a 25-second window it will chain a frame at t=0 to one at
    t=0:20 to one at t=0:40 and onwards, so a slow sequence of similar shots — a
    dinner table, a walk down one street — collapses into a single "moment" and
    the rest are silently dropped from consideration. Comparing every candidate
    against the burst's *anchor* rather than its predecessor, and capping the
    total span, stops both the time drift and the appearance drift.

    Frames with no timestamp each become their own moment. We could group them
    on appearance alone, but a photo we cannot place in time is exactly the
    photo we should not be quietly merging into something else.
    """
    if not frames:
        return {}

    dated = sorted((f for f in frames if f.taken is not None), key=lambda f: f.taken)
    undated = [f for f in frames if f.taken is None]

    groups: list[list[Frame]] = []
    for f in dated:
        if groups:
            cur = groups[-1]
            anchor, prev = cur[0], cur[-1]
            joins = (
                (f.taken - prev.taken).total_seconds() <= seconds
                and (f.taken - anchor.taken).total_seconds() <= max_span
                and (hamming(f.phash, anchor.phash) <= distance
                     or hamming(f.dhash, anchor.dhash) <= distance)
            )
            if joins:
                cur.append(f)
                continue
        groups.append([f])
    groups.extend([f] for f in undated)

    out: dict[str, tuple[int, bool]] = {}
    for bid, members in enumerate(groups):
        best = max(members, key=lambda f: f.score)
        for f in members:
            out[f.sha] = (bid, f is best)
    return out
