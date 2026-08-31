"""Stage 0 — ingest and normalise.

Content-addressed, so the same shot arriving from two phones dedupes for free.

The interesting part is the timestamp. Google rewrites embedded EXIF on upload,
so the sidecar's `photoTakenTime` (UTC epoch) is authoritative for *when*. But
EXIF `DateTimeOriginal`, where it survives, is local wall-clock time at the
camera. Having both lets us recover the exact UTC offset by subtraction —
better than any longitude estimate, and it matters because chapter boundaries
and "Day 3" labels are computed in local time.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from . import takeout

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

EXIF_DATETIME_ORIGINAL = 36867
EXIF_OFFSET_TIME_ORIGINAL = 36881   # present on newer iPhones: the real offset
EXIF_IFD = 0x8769
GPS_IFD = 0x8825


@dataclass
class Ingested:
    sha: str
    path: Path
    filename: str
    bytes: int
    kind: str
    width: int | None
    height: int | None
    taken_utc: datetime | None
    taken_local: datetime | None
    tz_offset_min: int | None
    tz_source: str
    lat: float | None
    lon: float | None
    camera: str | None
    sidecar_path: str | None
    sidecar_strategy: str
    sidecar_conf: float
    notes: str


def content_hash(path: Path, chunk: int = 1 << 20) -> str:
    """BLAKE2b of the file bytes — the identity every stage keys on."""
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in takeout.VIDEO_EXT:
        return "video"
    if ext in takeout.MEDIA_EXT:
        return "image"
    return "unknown"


def _exif_bits(path: Path) -> tuple[int | None, int | None, datetime | None,
                                    int | None, float | None, float | None, str | None]:
    """(width, height, exif local datetime, exif utc offset minutes, lat, lon, camera)."""
    try:
        # No im.load(): dimensions and EXIF are available from the header, and
        # decoding every pixel here would double the work for an archive whose
        # pixels stage 1 is about to decode anyway.
        with Image.open(path) as im:
            w, h = im.size
            exif = im.getexif()
    except (UnidentifiedImageError, OSError, ValueError):
        return None, None, None, None, None, None, None

    camera = None
    make, model = exif.get(271), exif.get(272)
    if make or model:
        camera = " ".join(str(x).strip() for x in (make, model) if x)

    local = None
    offset_min = None
    try:
        sub = exif.get_ifd(EXIF_IFD)
    except Exception:
        sub = {}
    if raw := sub.get(EXIF_DATETIME_ORIGINAL):
        try:
            local = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            local = None
    if raw := sub.get(EXIF_OFFSET_TIME_ORIGINAL):
        # Format is "+01:00" / "-07:00".
        try:
            txt = str(raw).strip()
            sign = -1 if txt[0] == "-" else 1
            hh, mm = txt[1:].split(":")
            offset_min = sign * (int(hh) * 60 + int(mm))
        except (ValueError, IndexError):
            offset_min = None

    lat = lon = None
    try:
        gps = exif.get_ifd(GPS_IFD)
    except Exception:
        gps = {}
    if gps:
        lat = _dms(gps.get(2), gps.get(1))
        lon = _dms(gps.get(4), gps.get(3))

    return w, h, local, offset_min, lat, lon, camera


def _dms(value, ref) -> float | None:
    if not value:
        return None
    try:
        d, m, s = (float(x) for x in value)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60.0 + s / 3600.0
    if str(ref).upper() in ("S", "W"):
        deg = -deg
    return deg


def _offset_from_longitude(lon: float | None) -> int | None:
    """Rough UTC offset from longitude — accurate to about an hour.

    Only used when we cannot derive the real offset. Good enough for grouping
    a trip into days, and flagged as approximate so it is never mistaken for
    ground truth.
    """
    if lon is None:
        return None
    return int(round(lon / 15.0)) * 60


def ingest_one(match: takeout.Match) -> Ingested:
    p = match.media
    notes: list[str] = []
    kind = _kind(p)
    w = h = None
    exif_local = exif_offset = lat = lon = camera = None
    if kind == "image":
        w, h, exif_local, exif_offset, lat, lon, camera = _exif_bits(p)

    side = takeout.load_sidecar(match.sidecar) if match.sidecar else {}
    taken_utc = None
    if ts := (side.get("photoTakenTime") or {}).get("timestamp"):
        try:
            taken_utc = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except (ValueError, OSError):
            taken_utc = None
    geo = side.get("geoData") or {}
    if lat is None and geo.get("latitude"):
        lat = float(geo["latitude"]) or None
    if lon is None and geo.get("longitude"):
        lon = float(geo["longitude"]) or None

    # Resolve the local wall clock, best source first.
    tz_source = "none"
    offset_min = None
    if exif_offset is not None:
        offset_min, tz_source = exif_offset, "exif_offset_tag"
    elif exif_local is not None and taken_utc is not None:
        delta = (exif_local - taken_utc.replace(tzinfo=None)).total_seconds() / 60.0
        offset_min = int(round(delta / 15.0)) * 15
        if abs(offset_min) > 14 * 60:
            notes.append(f"implausible EXIF/JSON offset ({offset_min} min), ignored")
            offset_min, tz_source = None, "none"
        else:
            tz_source = "exif_vs_json"
    if offset_min is None:
        offset_min = _offset_from_longitude(lon)
        if offset_min is not None:
            tz_source = "longitude"

    if taken_utc is not None:
        local = (taken_utc + timedelta(minutes=offset_min or 0)).replace(tzinfo=None)
    elif exif_local is not None:
        local = exif_local
        taken_utc = (exif_local - timedelta(minutes=offset_min or 0)).replace(tzinfo=timezone.utc)
        notes.append("no sidecar time; using EXIF")
    else:
        local = None
        notes.append("no capture time from any source")

    if not match.matched:
        notes.append("no sidecar matched")
    elif match.confidence < 0.8:
        notes.append(f"low-confidence sidecar match ({match.strategy})")

    return Ingested(
        sha=content_hash(p), path=str(p), filename=p.name, bytes=p.stat().st_size,
        kind=kind, width=w, height=h,
        taken_utc=taken_utc, taken_local=local,
        tz_offset_min=offset_min, tz_source=tz_source,
        lat=lat, lon=lon, camera=camera,
        sidecar_path=str(match.sidecar) if match.sidecar else None,
        sidecar_strategy=match.strategy, sidecar_conf=match.confidence,
        notes="; ".join(notes),
    )


def ingest_dir(root: Path) -> list[Ingested]:
    files = takeout.scan(root)
    return [ingest_one(m) for m in takeout.match_all(files)]
