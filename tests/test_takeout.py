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


def run_regressions() -> int:
    """Two bugs found in audit. Both were silent; both stay fixed here."""
    from datetime import datetime, timedelta
    import tempfile

    from photobook.dedupe import Frame, group_bursts

    failures = 0

    # 1. Transitive chaining. Twelve frames 20s apart are inside a 25s window
    #    pairwise, but span 3m40s in total. Union-find merged all twelve into
    #    one "moment", silently discarding eleven distinct frames.
    base = datetime(2026, 6, 12, 12, 0)
    same = 0xABCD1234ABCD1234
    frames = [Frame(f"f{i}", base + timedelta(seconds=20 * i), same, same, float(i))
              for i in range(12)]
    sizes = {}
    for sha, (bid, _) in group_bursts(frames).items():
        sizes[bid] = sizes.get(bid, 0) + 1
    if len(sizes) < 2 or max(sizes.values()) > 6:
        print(f"FAIL burst chaining: 12 frames over 3m40s became {sizes}")
        failures += 1
    else:
        print(f"ok   slow sequence splits into {len(sizes)} moments, not 1")

    # A genuine burst must still hold together.
    tight = [Frame(f"b{i}", base + timedelta(seconds=3 * i), same, same, float(i))
             for i in range(8)]
    g = group_bursts(tight)
    if len({bid for bid, _ in g.values()}) != 1:
        print("FAIL genuine burst was split")
        failures += 1
    elif not g["b7"][1]:
        print("FAIL burst representative is not the highest-scoring frame")
        failures += 1
    else:
        print("ok   genuine 3s burst stays one moment, best frame represents it")

    # 2. Contested truncation. Two media sharing a prefix both claimed one
    #    truncated sidecar, so both silently inherited the same capture time.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        for n in ("PXL_A_PORTRAIT-01.COVER.jpg", "PXL_A_PORTRAIT-02.COVER.jpg"):
            (p / n).write_bytes(b"x")
        (p / "PXL_A_PORTRAIT-0.json").write_text("{}")
        got = takeout.match_all(takeout.scan(p))
        if any(m.matched for m in got):
            print("FAIL contested sidecar was awarded to "
                  f"{[m.media.name for m in got if m.matched]}")
            failures += 1
        else:
            print("ok   contested truncation withdrawn from both claimants")

    # …but a single claimant must still get its truncated sidecar.
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "PXL_A_PORTRAIT-01.COVER.jpg").write_bytes(b"x")
        (p / "PXL_A_PORTRAIT-0.json").write_text("{}")
        got = takeout.match_all(takeout.scan(p))
        if not got[0].matched:
            print("FAIL unambiguous truncation should still match")
            failures += 1
        else:
            print("ok   unambiguous truncation still matches")

    return failures


if __name__ == "__main__":
    import tempfile

    fails = 0
    with tempfile.TemporaryDirectory() as d:
        fails += run(Path(d))
    print("\n-- timestamp resolution --")
    with tempfile.TemporaryDirectory() as d:
        fails += run_timestamps(Path(d))
    print("\n-- audit regressions --")
    fails += run_regressions()
    print(f"\nTOTAL FAILURES: {fails}")
    sys.exit(1 if fails else 0)
