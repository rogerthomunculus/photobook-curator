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
from .quality import analyze


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

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
