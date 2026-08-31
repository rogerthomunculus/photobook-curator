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

BURST_SECONDS = 25.0     # frames further apart than this start a new burst
BURST_DISTANCE = 14      # max Hamming distance within a burst (64-bit hash)
DUPLICATE_DISTANCE = 6   # at or below this, effectively the same frame


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


class _Union:
    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def group_bursts(frames: list[Frame], *, seconds: float = BURST_SECONDS,
                 distance: int = BURST_DISTANCE) -> dict[str, tuple[int, bool]]:
    """Group frames into bursts. Returns sha -> (burst_id, is_representative).

    Frames without a timestamp are grouped on appearance alone, which is the
    honest fallback: we would rather over-group an untimed frame than scatter
    it across the book.
    """
    if not frames:
        return {}
    order = sorted(range(len(frames)),
                   key=lambda i: (frames[i].taken is None,
                                  frames[i].taken or datetime.min))
    uf = _Union(len(frames))

    for pos, i in enumerate(order):
        fi = frames[i]
        # Only look forward while the time gap can still qualify; with the list
        # in time order this keeps the comparison linear in practice.
        for j in order[pos + 1:]:
            fj = frames[j]
            if fi.taken and fj.taken:
                gap = abs((fj.taken - fi.taken).total_seconds())
                if gap > seconds:
                    break
            close = (hamming(fi.phash, fj.phash) <= distance
                     or hamming(fi.dhash, fj.dhash) <= distance)
            if close:
                uf.union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(frames)):
        groups.setdefault(uf.find(i), []).append(i)

    out: dict[str, tuple[int, bool]] = {}
    for bid, (_, members) in enumerate(sorted(groups.items(),
                                              key=lambda kv: min(kv[1]))):
        best = max(members, key=lambda i: frames[i].score)
        for i in members:
            out[frames[i].sha] = (bid, i == best)
    return out


def exact_duplicates(frames: list[Frame]) -> list[tuple[str, str]]:
    """Pairs that are effectively the same frame, e.g. the same shot off two phones."""
    pairs = []
    for a in range(len(frames)):
        for b in range(a + 1, len(frames)):
            if hamming(frames[a].phash, frames[b].phash) <= DUPLICATE_DISTANCE:
                pairs.append((frames[a].sha, frames[b].sha))
    return pairs
