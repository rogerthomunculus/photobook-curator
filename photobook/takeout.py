"""Match Google Takeout media files to their JSON sidecars.

Google strips or rewrites embedded EXIF on upload, so the sidecar is the
authoritative source for capture time and location. Matching them is the
single most failure-prone step in the whole pipeline: a silent mismatch
assigns the wrong timestamp, which corrupts chaptering and therefore the
entire book.

So every match records *which strategy produced it* and a confidence, and
anything unmatched is reported loudly rather than skipped.

Known Takeout naming behaviours, all observed in the wild:

    IMG_1234.JPG  ->  IMG_1234.JPG.json                    (common)
                  ->  IMG_1234.json                        (older exports)
                  ->  IMG_1234.JPG.supplemental-metadata.json  (2024+)
                  ->  IMG_1234.JPG.suppl.json              (truncated tail)
    IMG_1234(1).JPG -> IMG_1234.JPG(1).json                (paren migrates!)
    very_long_name... -> sidecar stem truncated to a fixed budget
    IMG_1234-edited.JPG -> no sidecar; inherits IMG_1234.JPG's
    IMG_1234.HEIC + IMG_1234.MP4 -> one shared sidecar (live photo)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff",
    ".mp4", ".mov", ".m4v", ".avi", ".3gp",
}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".3gp"}

# Google appends an editor marker in several localisations; these are the ones
# that show up in English-language exports.
EDIT_MARKERS = ("-edited", "-bearbeitet", "-modifié", "-editado", "-modificato")

_PAREN = re.compile(r"^(?P<stem>.*?)\((?P<n>\d+)\)$")
# Sidecar tails Google has used, longest first so truncation matching is greedy.
_SIDECAR_TAILS = (
    ".supplemental-metadata",
    ".supplemental-meta",
    ".supplemental",
    ".suppl",
)


@dataclass(frozen=True)
class Match:
    media: Path
    sidecar: Path | None
    strategy: str
    confidence: float

    @property
    def matched(self) -> bool:
        return self.sidecar is not None


def _strip_edit_marker(stem: str) -> str | None:
    """`IMG_1234-edited` -> `IMG_1234`; None if there is no marker."""
    low = stem.lower()
    for m in EDIT_MARKERS:
        if low.endswith(m):
            return stem[: -len(m)]
    return None


def _sidecar_key(json_name: str) -> str:
    """Reduce a sidecar filename to the media name it is trying to describe.

    Strips the `.json`, any supplemental tail, and normalises a trailing
    duplicate marker so `IMG_1234.JPG(1)` and `IMG_1234(1).JPG` compare equal.
    """
    stem = json_name[:-5] if json_name.lower().endswith(".json") else json_name
    low = stem.lower()
    for tail in _SIDECAR_TAILS:
        if low.endswith(tail):
            stem = stem[: -len(tail)]
            break
    return _normalise_dup(stem)


def _normalise_dup(name: str) -> str:
    """Move a trailing `(N)` in front of the extension, so both spellings agree.

    `IMG_1234.JPG(1)` and `IMG_1234(1).JPG` both become `img_1234(1).jpg`.
    """
    name = name.lower()
    m = _PAREN.match(name)
    if m:
        stem, n = m.group("stem"), m.group("n")
        base, dot, ext = stem.rpartition(".")
        if dot:
            return f"{base}({n}).{ext}"
        return f"{stem}({n})"
    base, dot, ext = name.rpartition(".")
    if dot:
        m2 = _PAREN.match(base)
        if m2:
            return f"{m2.group('stem')}({m2.group('n')}).{ext}"
    return name


def index_sidecars(files: list[Path]) -> dict[str, list[Path]]:
    """Group every `.json` in the archive by the media name it refers to."""
    idx: dict[str, list[Path]] = {}
    for f in files:
        if f.suffix.lower() != ".json":
            continue
        idx.setdefault(_sidecar_key(f.name), []).append(f)
    return idx


def match_all(files: list[Path]) -> list[Match]:
    """Resolve every media file in `files` to a sidecar, best strategy first."""
    by_dir: dict[Path, dict[str, list[Path]]] = {}
    for f in files:
        if f.suffix.lower() == ".json":
            by_dir.setdefault(f.parent, {}).setdefault(_sidecar_key(f.name), []).append(f)

    out: list[Match] = []
    for f in sorted(files):
        if f.suffix.lower() == ".json" or f.suffix.lower() not in MEDIA_EXT:
            continue
        out.append(_match_one(f, by_dir.get(f.parent, {})))
    return out


def _match_one(media: Path, idx: dict[str, list[Path]]) -> Match:
    full = _normalise_dup(media.name)          # img_1234(1).jpg
    stem_only = _normalise_dup(media.stem)     # img_1234(1)

    # 1. Exact: the sidecar names the file, extension and all.
    if hit := idx.get(full):
        return Match(media, hit[0], "exact", 1.0)

    # 2. Extension-less: older exports drop it.
    if hit := idx.get(stem_only):
        return Match(media, hit[0], "stem", 0.95)

    # 3. Editor output inherits the original's sidecar.
    if (base := _strip_edit_marker(media.stem)) is not None:
        for key, strategy in ((_normalise_dup(f"{base}{media.suffix}"), "edited_parent"),
                              (_normalise_dup(base), "edited_parent_stem")):
            if hit := idx.get(key):
                return Match(media, hit[0], strategy, 0.9)

    # 4. Live-photo sibling: the video half often has no sidecar of its own.
    if media.suffix.lower() in VIDEO_EXT:
        for ext in (".heic", ".jpg", ".jpeg", ".HEIC", ".JPG"):
            if hit := idx.get(_normalise_dup(media.stem + ext)):
                return Match(media, hit[0], "live_photo_sibling", 0.85)

    # 5. Truncation: Google caps the sidecar filename, so the key we built is a
    #    prefix of the real media name. Only accept an unambiguous prefix.
    cands = [(k, v) for k, v in idx.items() if len(k) >= 8 and full.startswith(k)]
    if len(cands) == 1:
        key, paths = cands[0]
        conf = 0.8 if len(key) >= 16 else 0.6
        return Match(media, paths[0], f"truncated:{len(key)}", conf)
    if len(cands) > 1:
        # Longest prefix wins, but flag the ambiguity in the strategy name.
        key, paths = max(cands, key=lambda kv: len(kv[0]))
        return Match(media, paths[0], f"truncated_ambiguous:{len(cands)}", 0.4)

    return Match(media, None, "unmatched", 0.0)


def load_sidecar(path: Path) -> dict:
    """Read a sidecar, tolerating the BOM some exports carry."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def scan(root: Path) -> list[Path]:
    """Every file under `root`, ignoring the store and OS cruft."""
    return [
        p for p in sorted(root.rglob("*"))
        if p.is_file()
        and ".photobook" not in p.parts
        and not p.name.startswith("._")
        and p.name != ".DS_Store"
    ]
