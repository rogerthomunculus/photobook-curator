"""SQLite store. One file, content-addressed, every stage re-runnable."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
    sha              TEXT PRIMARY KEY,
    path             TEXT NOT NULL,
    filename         TEXT NOT NULL,
    bytes            INTEGER NOT NULL,
    kind             TEXT NOT NULL,          -- image | video | unknown
    width            INTEGER,
    height           INTEGER,
    -- provenance of the timestamp, because Google rewrites EXIF on upload
    taken_utc        TEXT,                   -- ISO8601 UTC
    taken_local      TEXT,                   -- ISO8601 naive local wall clock
    tz_offset_min    INTEGER,
    tz_source        TEXT,                   -- exif_vs_json | longitude | none
    lat              REAL,
    lon              REAL,
    camera           TEXT,
    -- sidecar bookkeeping
    sidecar_path     TEXT,
    sidecar_strategy TEXT,                   -- how the match was made
    sidecar_conf     REAL,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS quality (
    sha              TEXT PRIMARY KEY REFERENCES asset(sha),
    subject_sharpness REAL,                  -- 90th-percentile tile; the one that matters
    laplacian_var    REAL,                   -- frame mean; kept for comparison
    sharp_tile_ratio REAL,
    tenengrad        REAL,
    clipped_high     REAL,
    clipped_low      REAL,
    brightness       REAL,
    contrast         REAL,
    jpeg_quality     REAL,                   -- estimated from quantization tables
    blockiness       REAL,
    banding          REAL,
    max_in_300       REAL,                   -- long edge inches at 300/240/200 dpi
    max_in_240       REAL,
    max_in_200       REAL,
    placement_cap    TEXT,                   -- full_bleed | half | quarter | reject
    verdict          TEXT,
    reasons          TEXT
);

CREATE TABLE IF NOT EXISTS phash (
    sha              TEXT PRIMARY KEY REFERENCES asset(sha),
    dhash            TEXT NOT NULL,          -- 16-char hex; a 64-bit hash
    phash            TEXT NOT NULL           -- overflows SQLite's signed INTEGER
);

CREATE TABLE IF NOT EXISTS burst (
    sha              TEXT PRIMARY KEY REFERENCES asset(sha),
    burst_id         INTEGER NOT NULL,
    is_representative INTEGER NOT NULL DEFAULT 0,
    burst_score      REAL
);

CREATE INDEX IF NOT EXISTS idx_asset_taken ON asset(taken_utc);
CREATE INDEX IF NOT EXISTS idx_burst_group ON burst(burst_id);
"""


def connect(root: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the store under `root/.photobook/db.sqlite`."""
    d = Path(root) / ".photobook"
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(d / "db.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con
