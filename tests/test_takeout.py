"""The sidecar matcher is critical path: a silent mismatch corrupts the timeline."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photobook import takeout  # noqa: E402
from tests import fixtures  # noqa: E402


def build(tmp: Path):
    fixtures.build(tmp)
    files = takeout.scan(tmp)
    return {m.media.name: m for m in takeout.match_all(files)}


EXPECTED = {
    "IMG_0001.JPG": ("IMG_0001.JPG.json", "exact"),
    "IMG_0002.JPG": ("IMG_0002.json", "stem"),
    "IMG_0003.JPG": ("IMG_0003.JPG.supplemental-metadata.json", "exact"),
    "IMG_0004.JPG": ("IMG_0004.JPG.suppl.json", "exact"),
    "IMG_0005.JPG": ("IMG_0005.JPG.json", "exact"),
    "IMG_0005(1).JPG": ("IMG_0005.JPG(1).json", "exact"),
    "PXL_20260612_143045123.PORTRAIT-01.COVER.jpg":
        ("PXL_20260612_143045123.PORTRAIT-01.COVER.j.json", "truncated"),
    "IMG_0007-edited.JPG": ("IMG_0007.JPG.json", "edited_parent"),
    "IMG_0008.MP4": ("IMG_0008.HEIC.json", "live_photo_sibling"),
}


def run(tmp: Path) -> int:
    m = build(tmp)
    failures = 0

    for name, (want_json, want_strategy) in EXPECTED.items():
        got = m.get(name)
        if got is None:
            print(f"FAIL {name}: not seen by the scanner")
            failures += 1
            continue
        if got.sidecar is None:
            print(f"FAIL {name}: unmatched (expected {want_json})")
            failures += 1
            continue
        if got.sidecar.name != want_json:
            print(f"FAIL {name}: matched {got.sidecar.name}, wanted {want_json}")
            failures += 1
            continue
        if not got.strategy.startswith(want_strategy):
            print(f"FAIL {name}: strategy {got.strategy}, wanted {want_strategy}*")
            failures += 1
            continue
        print(f"ok   {name:48s} <- {got.sidecar.name}  [{got.strategy} {got.confidence}]")

    # The orphan must stay an orphan: inventing a match is worse than reporting none.
    orphan = m.get("IMG_0009.JPG")
    if orphan is None or orphan.matched:
        print("FAIL IMG_0009.JPG: should be unmatched, "
              f"got {orphan.sidecar.name if orphan and orphan.sidecar else orphan}")
        failures += 1
    else:
        print("ok   IMG_0009.JPG                                     <- (correctly unmatched)")

    # No sidecar may be claimed by two different media files by an exact rule.
    claims: dict[str, list[str]] = {}
    for name, mt in m.items():
        if mt.matched and mt.strategy in ("exact", "stem"):
            claims.setdefault(mt.sidecar.name, []).append(name)
    for j, owners in claims.items():
        if len(owners) > 1:
            print(f"FAIL sidecar {j} claimed by {owners}")
            failures += 1

    total = len(m)
    matched = sum(1 for x in m.values() if x.matched)
    print(f"\n{matched}/{total} media matched, {failures} failures")
    return failures


def run_timestamps(tmp: Path) -> int:
    """The offset-recovery path: EXIF local time minus sidecar UTC."""
    from photobook.ingest import ingest_dir

    fixtures.build(tmp)
    rows = {r.filename: r for r in ingest_dir(tmp)}
    failures = 0

    cases = [
        ("IMG_0001.JPG", 60, "exif_vs_json"),   # derived by subtraction
        ("IMG_0002.JPG", 60, "exif_offset_tag"),  # read from the tag
        ("IMG_0100.JPG", -60, "longitude"),     # no EXIF: rough estimate, flagged
    ]
    for name, want_off, want_src in cases:
        r = rows.get(name)
        if r is None:
            print(f"FAIL {name}: not ingested")
            failures += 1
            continue
        if r.tz_offset_min != want_off or r.tz_source != want_src:
            print(f"FAIL {name}: offset {r.tz_offset_min} via {r.tz_source}, "
                  f"wanted {want_off} via {want_src}")
            failures += 1
        else:
            print(f"ok   {name:16s} offset {r.tz_offset_min:+4d} min via {r.tz_source} "
                  f"-> local {r.taken_local}")

    orphan = rows.get("IMG_0009.JPG")
    if orphan is None or orphan.taken_utc is not None:
        print("FAIL IMG_0009.JPG: should have no capture time at all")
        failures += 1
    else:
        print("ok   IMG_0009.JPG     no capture time, correctly reported")
    return failures


if __name__ == "__main__":
    import tempfile

    fails = 0
    with tempfile.TemporaryDirectory() as d:
        fails += run(Path(d))
    print("\n-- timestamp resolution --")
    with tempfile.TemporaryDirectory() as d:
        fails += run_timestamps(Path(d))
    print(f"\nTOTAL FAILURES: {fails}")
    sys.exit(1 if fails else 0)
