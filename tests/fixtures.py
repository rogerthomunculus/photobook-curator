"""Build a synthetic Google Takeout archive that reproduces the real one's mess.

The point is to have something to fail against before the real archive lands.
Every naming pathology here has been observed in actual exports.
"""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

try:  # HEIC support, so the fixture exercises the same decode path as the archive
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - optional
    pillow_heif = None

VIDEO_SUFFIX = {".mp4", ".mov", ".m4v"}

BASE = datetime(2026, 6, 12, 9, 30, tzinfo=timezone.utc)


def _scene(w: int, h: int, seed: int, blur: float = 0.0, dark: bool = False,
           blown: bool = False) -> Image.Image:
    """A deterministic synthetic photo with controllable defects."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    sky = 210 - 90 * (yy / h)
    land = 60 + 50 * np.sin(xx / (w / 7.0) + seed)
    horizon = h * 0.45
    img = np.where(yy < horizon, sky, land)
    # A few high-contrast "subjects" so sharpness metrics have something to bite.
    for i in range(6):
        cx, cy = rng.integers(w // 8, w - w // 8), rng.integers(int(horizon), h)
        r = rng.integers(min(w, h) // 30, min(w, h) // 12)
        m = (xx - cx) ** 2 + (yy - cy) ** 2 < r * r
        img[m] = 20 + i * 35
    img += rng.normal(0, 3, img.shape)

    if blur > 0:
        k = max(1, int(blur))
        pad = np.pad(img, k, mode="edge")
        acc = np.zeros_like(img)
        for dy in range(-k, k + 1):
            for dx in range(-k, k + 1):
                acc += pad[k + dy:k + dy + h, k + dx:k + dx + w]
        img = acc / ((2 * k + 1) ** 2)
    if dark:
        img *= 0.18
    if blown:
        img = img * 1.9 + 40

    rgb = np.stack([img, img * 0.97, img * 0.9], axis=-1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))


def _exif(local: datetime | None, offset: str | None) -> Image.Exif | None:
    """Build the EXIF a phone would embed: local wall clock, optional offset tag."""
    if local is None:
        return None
    ex = Image.Exif()
    ex[271], ex[272] = "Apple", "iPhone 15 Pro"
    sub = ex.get_ifd(0x8769)
    sub[36867] = local.strftime("%Y:%m:%d %H:%M:%S")
    if offset:
        sub[36881] = offset
    return ex


def _write(img: Image.Image, path: Path, quality: int = 92,
           exif: Image.Exif | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        kw = {"exif": exif} if exif is not None else {}
        img.save(path, "JPEG", quality=quality, subsampling=2, **kw)
    elif ext in VIDEO_SUFFIX:
        # We do not decode video in Phase 0; a stub is enough to exercise the
        # sidecar matcher's live-photo sibling rule.
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256)
    elif ext in (".heic", ".heif"):
        if pillow_heif is None:
            img.save(path.with_suffix(".png"))
        else:
            img.save(path, "HEIF", quality=quality)
    else:
        img.save(path)


def _sidecar(path: Path, taken: datetime, lat: float, lon: float,
             title: str | None = None) -> dict:
    return {
        "title": title or path.name,
        "photoTakenTime": {"timestamp": str(int(taken.timestamp())),
                           "formatted": taken.strftime("%d %b %Y, %H:%M:%S UTC")},
        "creationTime": {"timestamp": str(int(taken.timestamp()))},
        "geoData": {"latitude": lat, "longitude": lon, "altitude": 0.0},
        "googlePhotosOrigin": {"mobileUpload": {"deviceType": "IOS_PHONE"}},
    }


def build(root: Path, seed: int = 7) -> dict:
    """Create the archive. Returns the ground truth for assertions."""
    rng = random.Random(seed)
    album = root / "Takeout" / "Google Photos" / "Trip 2026 — Portugal"
    album.mkdir(parents=True, exist_ok=True)
    truth: dict[str, dict] = {}

    def emit(name: str, sidecar_name: str | None, *, minutes: int, lat: float,
             seconds: int = 0,
             lon: float, w: int = 4032, h: int = 3024, quality: int = 92,
             blur: float = 0.0, dark: bool = False, blown: bool = False,
             seed_off: int = 0, small: bool = False,
             exif_offset_min: int | None = None,
             exif_offset_tag: str | None = None) -> Path:
        if small:
            w, h = w // 4, h // 4
        p = album / name
        taken = BASE + timedelta(minutes=minutes, seconds=seconds)
        # A phone writes local wall-clock time into EXIF; Google writes UTC into
        # the sidecar. Emitting both is what lets ingest recover the real offset.
        exif_local = (taken + timedelta(minutes=exif_offset_min)).replace(tzinfo=None) \
            if exif_offset_min is not None else None
        _write(_scene(w // 6, h // 6, seed + seed_off, blur, dark, blown).resize((w, h)),
               p, quality, _exif(exif_local, exif_offset_tag))
        if sidecar_name:
            (album / sidecar_name).write_text(
                json.dumps(_sidecar(p, taken, lat, lon), indent=2), encoding="utf-8")
        truth[name] = {"taken": taken, "sidecar": sidecar_name, "lat": lat, "lon": lon}
        return p

    lat, lon = 38.7223, -9.1393  # Lisbon
    n = 0

    # -- the four sidecar spellings ----------------------------------------
    # IMG_0001 carries EXIF local time but no offset tag -> offset by subtraction.
    emit("IMG_0001.JPG", "IMG_0001.JPG.json", minutes=0, lat=lat, lon=lon,
         exif_offset_min=60)
    # IMG_0002 carries the explicit offset tag -> read it directly.
    emit("IMG_0002.JPG", "IMG_0002.json", minutes=3, lat=lat, lon=lon, seed_off=1,
         exif_offset_min=60, exif_offset_tag="+01:00")
    emit("IMG_0003.JPG", "IMG_0003.JPG.supplemental-metadata.json",
         minutes=7, lat=lat, lon=lon, seed_off=2)
    emit("IMG_0004.JPG", "IMG_0004.JPG.suppl.json", minutes=11, lat=lat, lon=lon, seed_off=3)

    # -- the paren migration -----------------------------------------------
    emit("IMG_0005.JPG", "IMG_0005.JPG.json", minutes=14, lat=lat, lon=lon, seed_off=4)
    emit("IMG_0005(1).JPG", "IMG_0005.JPG(1).json", minutes=15, lat=lat, lon=lon, seed_off=4)

    # -- truncation ---------------------------------------------------------
    long_name = "PXL_20260612_143045123.PORTRAIT-01.COVER.jpg"
    emit(long_name, "PXL_20260612_143045123.PORTRAIT-01.COVER.j.json",
         minutes=20, lat=lat, lon=lon, seed_off=5)

    # -- an edited derivative with no sidecar of its own --------------------
    emit("IMG_0007.JPG", "IMG_0007.JPG.json", minutes=25, lat=lat, lon=lon, seed_off=6)
    emit("IMG_0007-edited.JPG", None, minutes=25, lat=lat, lon=lon, seed_off=6)

    # -- live photo: still + video share one sidecar ------------------------
    emit("IMG_0008.HEIC", "IMG_0008.HEIC.json", minutes=30, lat=lat, lon=lon, seed_off=7)
    emit("IMG_0008.MP4", None, minutes=30, lat=lat, lon=lon, small=True, seed_off=7)

    # -- an orphan: media with nothing at all -------------------------------
    emit("IMG_0009.JPG", None, minutes=33, lat=lat, lon=lon, seed_off=8)

    # -- quality spread: sharp, blurred, dark, blown, heavily recompressed ---
    emit("IMG_0100.JPG", "IMG_0100.JPG.json", minutes=60, lat=lat, lon=lon, seed_off=20)
    emit("IMG_0101.JPG", "IMG_0101.JPG.json", minutes=61, lat=lat, lon=lon,
         seed_off=20, blur=6)
    emit("IMG_0102.JPG", "IMG_0102.JPG.json", minutes=62, lat=lat, lon=lon,
         seed_off=21, dark=True)
    emit("IMG_0103.JPG", "IMG_0103.JPG.json", minutes=63, lat=lat, lon=lon,
         seed_off=22, blown=True)
    emit("IMG_0104.JPG", "IMG_0104.JPG.json", minutes=64, lat=lat, lon=lon,
         seed_off=23, quality=28)
    # too small to carry a full-bleed page
    emit("IMG_0105.JPG", "IMG_0105.JPG.json", minutes=65, lat=lat, lon=lon,
         seed_off=24, small=True)

    # -- a burst: 8 near-identical frames seconds apart ---------------------
    for i in range(8):
        # Distinct blur radii, so the frames are near-identical but not byte-equal;
        # frame 3 is the sharp one the burst picker should choose.
        emit(f"IMG_02{i:02d}.JPG", f"IMG_02{i:02d}.JPG.json",
             minutes=120, seconds=i * 3, lat=lat, lon=lon, seed_off=30,
             blur=0.0 if i == 3 else float(i + 1))

    # -- a second chapter, 200km and 6 hours away ---------------------------
    for i in range(6):
        emit(f"IMG_03{i:02d}.JPG", f"IMG_03{i:02d}.JPG.json",
             minutes=600 + i * 12, lat=41.1579, lon=-8.6291, seed_off=40 + i)

    # -- junk: a screenshot with no camera EXIF -----------------------------
    emit("Screenshot_20260612-201133.png", "Screenshot_20260612-201133.png.json",
         minutes=700, lat=lat, lon=lon, w=1179, h=2556)

    n = len(truth)
    (root / "Takeout" / "archive_browser.html").write_text("<html></html>")
    return {"album": album, "truth": truth, "count": n}


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake-takeout")
    info = build(out)
    print(f"built {info['count']} media files under {info['album']}")
