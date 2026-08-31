"""Contact sheets and triage reports — the human-readable end of Phase 0."""

from __future__ import annotations

import base64
import html
import io
import sqlite3
from collections import Counter
from pathlib import Path

from PIL import Image

THUMB = 260

CSS = """
:root{--bg:#F4F6F4;--card:#fff;--ink:#141917;--dim:#5C6461;--rule:#DBE0DA;
--keep:#1F6B5A;--review:#B07B22;--reject:#A2352B}
@media(prefers-color-scheme:dark){:root{--bg:#0F1413;--card:#171C1A;--ink:#E7EBE6;
--dim:#A3AEA8;--rule:#28312E;--keep:#5CBBA1;--review:#D6A44B;--reject:#E08A7D}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);margin:0;padding:28px;
font:15px/1.5 ui-sans-serif,system-ui,sans-serif}
h1{font-size:1.6rem;margin:0 0 4px}
.sub{color:var(--dim);margin:0 0 24px}
.stats{display:flex;flex-wrap:wrap;gap:1px;background:var(--rule);border:1px solid var(--rule);
margin:0 0 28px}
.stat{background:var(--card);padding:12px 18px;flex:1 1 150px}
.stat b{display:block;font-size:1.5rem;font-variant-numeric:tabular-nums}
.stat span{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
h2{font-size:1.05rem;margin:32px 0 10px;padding-top:12px;border-top:1px solid var(--rule)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.cell{background:var(--card);border:1px solid var(--rule);overflow:hidden}
.cell img{display:block;width:100%;height:auto;background:#222}
.cell .m{padding:9px 11px;font-size:12.5px}
.cell .n{font-weight:600;word-break:break-all}
.cell .r{color:var(--dim);margin-top:3px}
.tag{display:inline-block;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
padding:1px 6px;border:1px solid currentColor;margin-right:5px}
.keep{color:var(--keep)}.review{color:var(--review)}.reject{color:var(--reject)}
.rep{outline:2px solid var(--keep);outline-offset:-2px}
table{border-collapse:collapse;width:100%;font-size:13.5px;background:var(--card);
border:1px solid var(--rule)}
th,td{text-align:left;padding:7px 12px;border-bottom:1px solid var(--rule)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim)}
td.n{font-variant-numeric:tabular-nums;text-align:right}
"""


def thumb_data_uri(path: Path, px: int = THUMB) -> str:
    try:
        with Image.open(path) as im:
            im.load()
            im = im.convert("RGB")
            im.thumbnail((px, px), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=78)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def _stat(label: str, value) -> str:
    return f'<div class="stat"><b>{value}</b><span>{html.escape(label)}</span></div>'


def _table(headers: list[str], rows: list[tuple]) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(
            f'<td class="n">{html.escape(str(c))}</td>' if isinstance(c, (int, float))
            else f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
        for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


SIDECAR_FORMS = (
    (".supplemental-metadata.json", "name.ext.supplemental-metadata.json"),
    (".supplemental-meta.json", "name.ext.supplemental-meta.json (truncated tail)"),
    (".supplemental.json", "name.ext.supplemental.json (truncated tail)"),
    (".suppl.json", "name.ext.suppl.json (truncated tail)"),
)
STORAGE_SAVER_MP = 16.0     # the documented Storage Saver ceiling
FULL_BLEED_FLOOR = 200      # dpi; see PLAN.md 1.1a


def _sidecar_form(path: str) -> str:
    """Which spelling of the sidecar name this is.

    The matcher strips these tails before comparing, so every form reports as
    `exact` — which is correct behaviour but useless for telling what Google
    actually wrote. Recovered here from the filename.
    """
    low = path.lower()
    for suffix, label in SIDECAR_FORMS:
        if low.endswith(suffix):
            return label
    return "name.ext.json" if low.count(".") > 1 else "name.json"


def triage_html(rows: list, out: Path, page_long_in: float = 14.0) -> Path:
    """Archive shape: what arrived, what matched, what we can't date or print."""
    n = len(rows)
    images = [r for r in rows if r.kind == "image"]
    matched = [r for r in rows if r.sidecar_path]
    low_conf = [r for r in rows if r.sidecar_path and r.sidecar_conf < 0.8]
    undated = [r for r in rows if r.taken_utc is None]
    rough = [r for r in rows if r.tz_source in ("longitude", "none")
             and r.taken_utc is not None]
    strategies = Counter(r.sidecar_strategy.split(":")[0] for r in rows)
    forms = Counter(_sidecar_form(r.sidecar_path) for r in matched)
    tz = Counter(r.tz_source for r in rows)
    cameras = Counter(r.camera or "(no camera in EXIF)" for r in rows)
    dims = Counter(f"{r.width}×{r.height}" for r in images if r.width)

    dated = [r for r in rows if r.taken_local]
    span = ""
    if dated:
        lo = min(r.taken_local for r in dated)
        hi = max(r.taken_local for r in dated)
        days = (hi.date() - lo.date()).days + 1
        span = f"{lo:%-d %b} – {hi:%-d %b %Y} ({days} days)"

    sized = [r for r in images if r.width and r.height]
    over_cap = [r for r in sized if r.width * r.height / 1e6 > STORAGE_SAVER_MP]
    need_px = page_long_in * FULL_BLEED_FLOOR
    short = [r for r in sized if max(r.width, r.height) < need_px]
    portrait = [r for r in sized if r.height > r.width]

    parts = [
        f"<style>{CSS}</style><h1>Takeout triage</h1>",
        f'<p class="sub">{n} media files. This is the shape of the archive before '
        "any curation — the numbers that decide whether the ingest plan survives "
        "contact with reality.</p>",
        '<div class="stats">',
        _stat("media files", n),
        _stat("date range", span or "unknown"),
        _stat("sidecars matched", f"{len(matched)}/{n}"),
        _stat("rough timestamps", len(rough)),
        _stat("no capture time", len(undated)),
        _stat("too small to print big", len(short)),
        "</div>",
        "<h2>How each sidecar was matched</h2>",
        _table(["strategy", "files"],
               sorted(strategies.items(), key=lambda kv: -kv[1])),
        "<h2>What Google named the sidecars</h2>",
        _table(["filename form", "files"],
               sorted(forms.items(), key=lambda kv: -kv[1])),
        "<h2>Where the local timestamp came from</h2>",
        _table(["source", "files"], sorted(tz.items(), key=lambda kv: -kv[1])),
        "<h2>Devices</h2>",
        _table(["camera", "files"], sorted(cameras.items(), key=lambda kv: -kv[1])[:15]),
        "<h2>Resolution and print headroom</h2>",
        _table(["measure", "files"], [
            (f"above the {STORAGE_SAVER_MP:.0f} MP Storage Saver ceiling "
             "(so NOT downsized)", len(over_cap)),
            (f"at or below {STORAGE_SAVER_MP:.0f} MP", len(sized) - len(over_cap)),
            (f"under {FULL_BLEED_FLOOR} dpi for a full-bleed "
             f'{page_long_in:g}" page', len(short)),
            ("portrait orientation", len(portrait)),
            ("landscape orientation", len(sized) - len(portrait)),
        ]),
        "<h2>Pixel dimensions</h2>",
        _table(["dimensions", "megapixels", "long edge at 300 dpi", "files"],
               [(d, f"{int(d.split('×')[0]) * int(d.split('×')[1]) / 1e6:.1f} MP",
                 f'{max(int(x) for x in d.split("×")) / 300:.1f}"', c)
                for d, c in sorted(dims.items(), key=lambda kv: -kv[1])[:20]]),
    ]
    attention = (
        [(r.filename, r.notes or "no sidecar matched") for r in undated]
        + [(r.filename, f"low-confidence sidecar: {r.sidecar_strategy}") for r in low_conf]
        + [(r.filename, f"timestamp from {r.tz_source} — can be two hours out, "
                        "enough to move it into the wrong day") for r in rough]
    )
    if attention:
        parts.append("<h2>Needs a human look</h2>")
        parts.append(_table(["file", "problem"], attention[:80]))

    out.write_text("".join(parts), encoding="utf-8")
    return out


def contact_sheet(con: sqlite3.Connection, out: Path, limit: int = 400) -> Path:
    """Every moment, best-of-burst first, with the reason it scored as it did."""
    rows = con.execute("""
        SELECT a.sha, a.filename, a.path, a.taken_local, a.width, a.height,
               q.verdict, q.reasons, q.placement_cap, q.subject_sharpness,
               q.jpeg_quality, b.burst_id, b.is_representative
        FROM asset a
        LEFT JOIN quality q ON q.sha = a.sha
        LEFT JOIN burst   b ON b.sha = a.sha
        WHERE a.kind = 'image'
        ORDER BY a.taken_local IS NULL, a.taken_local, a.filename
        LIMIT ?""", (limit,)).fetchall()

    verdicts = Counter(r["verdict"] or "unscored" for r in rows)
    bursts = {r["burst_id"] for r in rows if r["burst_id"] is not None}
    reps = [r for r in rows if r["is_representative"]]

    cells = []
    for r in rows:
        v = r["verdict"] or "unscored"
        rep = " rep" if r["is_representative"] else ""
        bid = r["burst_id"]
        extra = f"burst {bid}" if bid is not None else ""
        meta = " · ".join(x for x in [
            r["taken_local"] or "no date",
            f'{r["width"]}×{r["height"]}' if r["width"] else "",
            (r["placement_cap"] or "").replace("_", " "),
            extra,
        ] if x)
        cells.append(
            f'<div class="cell{rep}">'
            f'<img loading="lazy" src="{thumb_data_uri(Path(r["path"]))}" alt="">'
            f'<div class="m"><div class="n">{html.escape(r["filename"])}</div>'
            f'<div class="r"><span class="tag {v}">{v}</span>{html.escape(meta)}</div>'
            f'<div class="r">{html.escape(r["reasons"] or "")}</div></div></div>')

    body = [
        f"<style>{CSS}</style><h1>Contact sheet</h1>",
        f'<p class="sub">{len(rows)} frames · {len(bursts)} distinct moments · '
        f"{len(reps)} burst representatives. Green outline marks the frame chosen "
        "to represent its burst.</p>",
        '<div class="stats">',
        *[_stat(k, v) for k, v in verdicts.most_common()],
        _stat("moments", len(bursts)),
        "</div>",
        f'<div class="grid">{"".join(cells)}</div>',
    ]
    out.write_text("".join(body), encoding="utf-8")
    return out
