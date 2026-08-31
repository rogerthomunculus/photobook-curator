# Phase 0 — notes from building it

What the code does, and the things that turned out to be different from the
plan once they were written down in Python.

## The sidecar matcher

Google's naming is inconsistent enough that a naive `name + ".json"` join
silently loses a double-digit percentage of an archive. The matcher tries, in
order: exact (`IMG_1234.JPG.json`), extension-less (`IMG_1234.json`), the
`-edited` parent, the live-photo sibling, and finally prefix matching for the
cases where Google truncated the sidecar filename. Every match records the
strategy and a confidence, and **an unmatched file stays unmatched** — inventing
a plausible-looking match is worse than reporting none, because a wrong
timestamp corrupts chaptering invisibly.

The `(N)` duplicate marker migrates across the extension —
`IMG_0005(1).JPG` pairs with `IMG_0005.JPG(1).json` — so both spellings are
normalised before comparison.

## Recovering the real timezone

This worked better than expected. The sidecar carries UTC; EXIF
`DateTimeOriginal`, where it survives Google's rewrite, is *local wall clock*.
Subtracting the two recovers the exact offset. Newer iPhones also write
`OffsetTimeOriginal`, which is better still. Three sources, best first:

1. `exif_offset_tag` — the phone recorded the offset explicitly
2. `exif_vs_json` — derived by subtraction, rounded to 15 minutes
3. `longitude` — a rough estimate, and genuinely rough

**The longitude fallback is worse than it looks.** For Lisbon it produces −60
minutes when the truth is +60: longitude ignores both political timezone
boundaries and DST, so it can be two hours out — enough to move an evening photo
into the wrong day and split a chapter in the wrong place. It is a last resort
and is flagged as such, not a quiet default.

## Sharpness has to be exposure-invariant

The first implementation scored a correctly-focused but underexposed frame as
"soft everywhere". Gradient magnitude scales with contrast, so a dark image
looks blurry to a raw Laplacian. Normalising to a fixed standard deviation
before measuring separates the two questions — *is it in focus* and *is it too
dark* — which are different problems with different fixes.

Sharpness is also measured **per tile**, with the subject score taken as the
90th-percentile tile rather than the frame mean. A portrait with a crisp face
over a soft background scores low on the mean and high on the tile percentile,
which is the correct reading.

## Recompression, from the quantization tables

On a Storage Saver archive every file was re-encoded once by Google already.
The encoder's quality setting is recoverable from the JPEG quantization tables
by inverting the IJG scaling formula per coefficient and taking the median.
That, plus a blockiness ratio measured across 8-pixel boundaries, decides
whether a photo can carry a large placement even when its pixel count says it
could.

The banding metric is currently a plateau ratio and its threshold is a
**placeholder** — synthetic gradients band far more readily than photographs do,
so it needs calibrating against the real archive before it is trusted.

## Placement caps, not a DPI gate

A hard 300 DPI gate would reject usable photos: an uncropped 12 MP phone shot
is ~288 DPI across a 14-inch page. So resolution resolves to a *placement cap*
— `full_bleed` / `half` / `quarter` / `reject` — against a 200 DPI floor, and a
photo is demoted a tier when its recompression is heavy or its blocking is
visible.

## What the synthetic fixture is for

`tests/fixtures.py` builds an archive containing every naming pathology, a
spread of quality defects, an eight-frame burst, two geographic chapters, an
orphan with no sidecar at all, and a screenshot. It exists so the pipeline can
fail against something *before* the real Takeout lands, and so a regression has
somewhere to show up later.

Two bugs it caught during development, both of which would have been invisible
on real data: SQLite rejecting 64-bit perceptual hashes as integers (they are
stored as hex text now), and content-addressing silently collapsing fixture
frames whose blur radii happened to round to the same kernel — correct
behaviour, misleading test.

## Open calibration work, once the real archive is here

- Banding threshold — currently a guess.
- `BURST_SECONDS` (25s) and `BURST_DISTANCE` (14 bits) — plausible, unvalidated.
- The verdict thresholds for sharpness and exposure want a pass over a few
  hundred real frames with a human disagreeing where it matters.
