"""Command line for the Phase 0 pipeline.

    photobook triage  <archive>     what arrived, and what we can't read
    photobook ingest  <archive>     hash, EXIF, sidecars -> store
    photobook analyze <archive>     quality metrics, hashes, burst grouping
    photobook sheet   <archive>     contact sheet of every moment

Every stage is re-runnable and nothing is destructive.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import report
from .db import connect
from .dedupe import Frame, group_bursts, hashes
from .ingest import ingest_dir
from .quality import analyze, score_archive


def _progress(i: int, n: int, label: str) -> None:
    if n and (i % 25 == 0 or i == n):
        pct = 100 * i / n
        print(f"\r  {label}: {i}/{n} ({pct:.0f}%)", end="", file=sys.stderr, flush=True)


def cmd_triage(args) -> int:
    root = Path(args.archive).expanduser().resolve()
    t0 = time.time()
    rows = ingest_dir(root)
    out = Path(args.out or root / "triage.html")
    report.triage_html(rows, out, page_long_in=args.page_inches)

    matched = sum(1 for r in rows if r.sidecar_path)
    undated = [r for r in rows if r.taken_utc is None]
    lowconf = [r for r in rows if r.sidecar_path and r.sidecar_conf < 0.8]
    print(f"{len(rows)} media files in {time.time() - t0:.1f}s")
    print(f"  sidecars matched : {matched}/{len(rows)}")
    print(f"  low confidence   : {len(lowconf)}")
    print(f"  no capture time  : {len(undated)}")
    if undated:
        for r in undated[:10]:
            print(f"      {r.filename}")
        if len(undated) > 10:
            print(f"      … and {len(undated) - 10} more")
    print(f"  report -> {out}")
    return 0


def cmd_ingest(args) -> int:
    root = Path(args.archive).expanduser().resolve()
    con = connect(root)
    before = con.execute("SELECT COUNT(*) FROM asset").fetchone()[0]
    rows = ingest_dir(root)
    with con:
        for r in rows:
            con.execute("""
                INSERT INTO asset (sha,path,filename,bytes,kind,width,height,
                    taken_utc,taken_local,tz_offset_min,tz_source,lat,lon,camera,
                    sidecar_path,sidecar_strategy,sidecar_conf,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(sha) DO UPDATE SET path=excluded.path""",
                (r.sha, r.path, r.filename, r.bytes, r.kind, r.width, r.height,
                 r.taken_utc.isoformat() if r.taken_utc else None,
                 r.taken_local.isoformat(sep=" ") if r.taken_local else None,
                 r.tz_offset_min, r.tz_source, r.lat, r.lon, r.camera,
                 r.sidecar_path, r.sidecar_strategy, r.sidecar_conf, r.notes))
    after = con.execute("SELECT COUNT(*) FROM asset").fetchone()[0]
    dupes = len(rows) - (after - before)
    print(f"ingested {len(rows)} files"
          + (f" ({dupes} were byte-identical duplicates of each other"
             " or of something already in the store)" if dupes > 0 else ""))
    return 0


def cmd_analyze(args) -> int:
    root = Path(args.archive).expanduser().resolve()
    con = connect(root)
    todo = con.execute("""
        SELECT a.sha, a.path FROM asset a
        LEFT JOIN quality q ON q.sha = a.sha
        WHERE a.kind = 'image' AND (q.sha IS NULL OR ?)""",
        (1 if args.force else 0,)).fetchall()
    n = len(todo)
    print(f"analysing {n} images (page long edge {args.page_inches}\")")

    failures: list[str] = []
    for i, row in enumerate(todo, 1):
        p = Path(row["path"])
        try:
            m = analyze(p, page_long_in=args.page_inches)
            d, ph = hashes(p)
        except Exception as e:                        # keep going; report at the end
            failures.append(f"{p.name}: {type(e).__name__}: {e}")
            continue
        with con:
            con.execute("""
                INSERT INTO quality (sha,subject_sharpness,laplacian_var,
                    sharp_tile_ratio,tenengrad,clipped_high,clipped_low,
                    brightness,contrast,jpeg_quality,blockiness,banding,
                    max_in_300,max_in_240,max_in_200,placement_cap,verdict,reasons)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(sha) DO UPDATE SET verdict=excluded.verdict,
                    reasons=excluded.reasons, placement_cap=excluded.placement_cap""",
                (row["sha"], m.subject_sharpness, m.laplacian_var, m.sharp_tile_ratio,
                 m.tenengrad, m.clipped_high, m.clipped_low, m.brightness,
                 m.contrast, m.jpeg_quality, m.blockiness, m.banding,
                 m.max_in_300, m.max_in_240, m.max_in_200,
                 m.placement_cap, m.verdict, m.reasons))
            con.execute("INSERT OR REPLACE INTO phash (sha,dhash,phash) VALUES (?,?,?)",
                        (row["sha"], f"{d:016x}", f"{ph:016x}"))
        _progress(i, n, "quality")
    print(file=sys.stderr)

    # Burst grouping needs every hash present, so it runs as a second pass.
    from datetime import datetime

    rows = con.execute("""
        SELECT a.sha, a.taken_local, p.dhash, p.phash,
               COALESCE(q.subject_sharpness,0) AS score
        FROM asset a JOIN phash p ON p.sha = a.sha
        LEFT JOIN quality q ON q.sha = a.sha""").fetchall()
    frames = [Frame(r["sha"],
                    datetime.fromisoformat(r["taken_local"]) if r["taken_local"] else None,
                    int(r["dhash"], 16), int(r["phash"], 16), r["score"])
              for r in rows]
    groups = group_bursts(frames)
    with con:
        con.execute("DELETE FROM burst")
        con.executemany("INSERT INTO burst (sha,burst_id,is_representative) VALUES (?,?,?)",
                        [(sha, bid, int(rep)) for sha, (bid, rep) in groups.items()])

    _rescore(con, args)

    if failures:
        print(f"\n{len(failures)} image(s) could not be analysed:")
        for f in failures[:10]:
            print(f"  {f}")
        if len(failures) > 10:
            print(f"  … and {len(failures) - 10} more")

    moments = len({bid for bid, _ in groups.values()})
    print(f"{len(frames)} frames -> {moments} distinct moments "
          f"({len(frames) - moments} collapsed into bursts)")
    for verdict, count in con.execute(
            "SELECT verdict, COUNT(*) c FROM quality GROUP BY verdict ORDER BY c DESC"):
        print(f"  {verdict:8s} {count}")
    return 0


def _rescore(con, args) -> None:
    """Assign verdicts across the whole archive. Cheap: no image is re-read."""
    rows = [dict(r) for r in con.execute("""
        SELECT q.sha, q.subject_sharpness, q.clipped_high, q.clipped_low,
               q.brightness, q.contrast, q.jpeg_quality, q.banding,
               q.placement_cap, q.reasons, a.width, a.height, a.camera
        FROM quality q JOIN asset a ON a.sha = q.sha""")]
    if not rows:
        return
    verdicts = score_archive(
        rows,
        reject_ratio=getattr(args, "reject_ratio", 0.20),
        review_ratio=getattr(args, "review_ratio", 0.45),
    )
    with con:
        con.executemany("UPDATE quality SET verdict=?, reasons=? WHERE sha=?",
                        [(v, why, sha) for sha, (v, why) in verdicts.items()])


def cmd_rescore(args) -> int:
    """Re-assign verdicts with different thresholds, without re-analysing."""
    con = connect(Path(args.archive).expanduser().resolve())
    _rescore(con, args)
    for verdict, count in con.execute(
            "SELECT verdict, COUNT(*) c FROM quality GROUP BY verdict ORDER BY c DESC"):
        print(f"  {verdict:8s} {count}")
    return 0


def cmd_stats(args) -> int:
    """Print the real distribution of every metric, so thresholds stop being guesses."""
    import numpy as np

    con = connect(Path(args.archive).expanduser().resolve())
    cols = ["subject_sharpness", "laplacian_var", "sharp_tile_ratio", "tenengrad",
            "brightness", "contrast", "clipped_high", "clipped_low",
            "jpeg_quality", "blockiness", "banding"]
    rows = con.execute(f"SELECT {','.join(cols)} FROM quality").fetchall()
    if not rows:
        print("nothing analysed yet")
        return 1

    pcts = [1, 5, 10, 25, 50, 75, 90, 99]
    print(f"{'metric':18s} " + " ".join(f"{'p' + str(p):>10s}" for p in pcts)
          + f" {'ratio p5/p50':>13s}")
    for i, c in enumerate(cols):
        raw = [r[i] for r in rows if r[i] is not None]
        # -1 is the "not measurable on this file" sentinel (jpeg_quality on a
        # non-JPEG). Averaging it in would be nonsense.
        vals = np.array([v for v in raw if v >= 0], dtype=float)
        missing = len(raw) - len(vals)
        if vals.size == 0:
            print(f"{c:18s} " + " " * 88 + f"  not measurable on any of {len(raw)} files")
            continue
        qs = np.percentile(vals, pcts)
        ratio = qs[1] / qs[4] if qs[4] else float("nan")
        note = f"   ({missing} not measurable)" if missing else ""
        print(f"{c:18s} " + " ".join(f"{v:10.4g}" for v in qs)
              + f" {ratio:13.3f}{note}")

    # Burst diagnostics: what do consecutive frames actually look like?
    adj = con.execute("""
        SELECT a.taken_local, p.dhash, p.phash FROM asset a
        JOIN phash p ON p.sha = a.sha
        WHERE a.taken_local IS NOT NULL ORDER BY a.taken_local""").fetchall()
    if len(adj) > 1:
        from datetime import datetime

        from .dedupe import hamming
        gaps, dists = [], []
        for a, b in zip(adj, adj[1:]):
            gap = (datetime.fromisoformat(b["taken_local"])
                   - datetime.fromisoformat(a["taken_local"])).total_seconds()
            gaps.append(gap)
            if gap <= 25:      # only pairs the burst rule would even consider
                dists.append(min(hamming(int(a["phash"], 16), int(b["phash"], 16)),
                                 hamming(int(a["dhash"], 16), int(b["dhash"], 16))))
        g = np.array(gaps)
        print(f"\nconsecutive-frame gaps (seconds): "
              + " ".join(f"p{p}={np.percentile(g, p):.0f}" for p in (10, 25, 50, 75, 90)))
        print(f"pairs within {25}s: {len(dists)} of {len(gaps)}")
        if dists:
            d = np.array(dists)
            print("hash distance for those pairs:      "
                  + " ".join(f"p{p}={np.percentile(d, p):.0f}" for p in (10, 25, 50, 75, 90))
                  + f"   (current threshold {14})")
            for t in (8, 12, 14, 18, 22, 26, 30):
                print(f"    distance <= {t:2d}: {100 * (d <= t).mean():5.1f}% of near-in-time pairs")
    return 0


def cmd_calibrate(args) -> int:
    """Small HTML showing what the metrics rank worst, so a human can check them."""
    root = Path(args.archive).expanduser().resolve()
    con = connect(root)
    out = Path(args.out or root / "calibration.html")
    report.calibration_sheet(con, out, n=args.n)
    pairs = out.with_name("calibration-bursts.html")
    report.burst_pairs_sheet(con, pairs)
    print(f"quality calibration -> {out}")
    print("  Flip through it: if the 'worst' photos are not actually bad, the "
          "metric is wrong, not the threshold.")
    print(f"burst calibration   -> {pairs}")
    print("  For each distance band: would you ever want both photos in the book?")
    return 0


def cmd_sheet(args) -> int:
    root = Path(args.archive).expanduser().resolve()
    con = connect(root)
    out = Path(args.out or root / "contact-sheet.html")
    report.contact_sheet(con, out, limit=args.limit)
    print(f"contact sheet -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="photobook", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, helptext in (
        ("triage", cmd_triage, "inventory the archive without committing to anything"),
        ("ingest", cmd_ingest, "hash, read EXIF and sidecars, populate the store"),
        ("analyze", cmd_analyze, "quality metrics, perceptual hashes, burst grouping"),
        ("sheet", cmd_sheet, "write a contact sheet of every moment"),
        ("stats", cmd_stats, "print the real distribution of every metric"),
        ("rescore", cmd_rescore, "re-assign verdicts without re-analysing"),
        ("calibrate", cmd_calibrate,
         "small sheet of the extremes, to check the metrics against your eye"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("archive", help="path to the extracted Takeout (or any folder)")
        p.add_argument("--out", help="output path for reports")
        p.set_defaults(func=fn)

    for cmd in ("triage", "analyze"):
        sub.choices[cmd].add_argument(
            "--page-inches", type=float, default=14.0,
            help="long edge of the book page in inches (default 14, i.e. 11x14)")
    sub.choices["analyze"].add_argument(
        "--force", action="store_true", help="re-analyse images already scored")
    sub.choices["sheet"].add_argument("--limit", type=int, default=400)
    sub.choices["calibrate"].add_argument(
        "--n", type=int, default=24, help="photos per band (default 24)")
    for cmd in ("analyze", "rescore"):
        sub.choices[cmd].add_argument(
            "--reject-ratio", type=float, default=0.20, dest="reject_ratio",
            help="reject below this fraction of the archive's median sharpness")
        sub.choices[cmd].add_argument(
            "--review-ratio", type=float, default=0.45, dest="review_ratio",
            help="flag for review below this fraction of median sharpness")

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
