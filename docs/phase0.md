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

## Two bugs the audit found

Both were silent. Neither would have announced itself on real data — they would
have produced a subtly worse book.

**Burst grouping chained transitively.** The first implementation used union-find
over pairs within a 25-second window, which merges a frame at t=0 with one at
t=0:20, then t=0:40, and onwards. Twelve visually similar frames spanning three
minutes forty collapsed into a single "moment", silently removing eleven
distinct frames from consideration — a dinner table, or a walk down one street,
would vanish into one photo.

Replaced with a sequential pass that compares each candidate against the burst's
**anchor** rather than its predecessor, and caps the total span at 90 seconds.
That stops both the time drift and the appearance drift a slow pan produces. A
genuine eight-frame burst three seconds apart still groups as one.

**Two media files could claim one truncated sidecar.** Truncation matching is a
prefix guess, and `PXL_..._PORTRAIT-01.COVER.jpg` and `...-02.COVER.jpg` both
reach a sidecar truncated before the digits. Both were awarded it at confidence
0.8, so both silently inherited one capture time — exactly the corruption the
module's docstring claims to prevent.

Now a firm claim (`exact`, `stem`) beats any guess on the same sidecar, and two
guesses with no firm claim cancel each other out. A withdrawn match becomes
`contested(...)` and unmatched, so the photo falls back to EXIF or is reported
undated. Both outcomes are visible; a wrong timestamp is not. Derivative claims
— an `-edited` copy, a live-photo video sharing its still's sidecar — are
legitimate and unaffected.

Both are covered by regression tests in `tests/test_takeout.py`.

## Smaller findings from the same pass

- `ingest` was calling `im.load()` purely to read dimensions and EXIF, decoding
  every pixel of every photo for data that lives in the header — then stage 1
  decoded them all again. Removed.
- Both stage 1 and the hashing now call `Image.draft()` before loading, so
  libjpeg decodes at roughly the size the metrics are computed at instead of
  full resolution. On the fixture the whole pipeline runs in about 6 seconds.
- The "byte-identical duplicates" count was computed against the whole table
  rather than the delta, so it was wrong on any re-run.
- `sha256_file` computed BLAKE2b. Renamed to `content_hash`.
- `exact_duplicates()` was unused, O(n²), and duplicated what burst grouping
  already does. Deleted.

## The first real run failed, and how

Running stage 1 on the real 969-photo archive returned **965 keep, 4 review, 0
reject**. That is not a pass; no archive of 969 trip photos contains zero bad
frames. The quality stage was rubber-stamping.

**Cause: the thresholds were calibrated on synthetic images and are wrong by
three orders of magnitude on photographs.** The fixture's scenes are smooth
gradients with a few hard-edged circles, so the per-tile Laplacian variance
spread out nicely and a reject threshold of 12 separated sharp from blurred. A
real photograph has fine detail *somewhere* almost always — foliage, gravel,
fabric, hair — so the 90th-percentile tile mostly answers "is there any texture
in this frame at all", and the answer is yes. On a synthetic-but-textured test,
a heavily blurred frame scored **18,249** against that threshold of 12.

Three changes came out of it.

**Measurement is now separate from judgment.** `analyze()` only measures;
`score_archive()` assigns verdicts once every photo has been measured. Re-scoring
therefore costs nothing — no image is decoded again — so thresholds can be swept
against the real distribution instead of guessed once and shipped.

**Sharpness verdicts are relative to the archive's own median**, not absolute. A
ratio is scale-free, so it survives a change of camera, lens or subject. It is
also better than a percentile rule, which would always condemn a fixed share of
the archive even when every photo is genuinely fine — a ratio rule can correctly
reject nothing.

**Two new commands exist so the next threshold is measured rather than
invented.** `photobook stats` prints percentiles for every metric plus the
distribution of hash distances between temporally adjacent frames.
`photobook calibrate` writes a small sheet of what each metric ranks worst
beside a random sample — the falsifiable test, because if the photos the metric
calls softest are not soft to the eye then the metric is wrong and no threshold
will save it.

## Burst detection was also too strict

The same run collapsed 969 frames into 808 moments — a ratio of 1.2:1 where
2.5:1 was expected. Two contributing causes. Comparing each candidate against
the burst *anchor* rather than its predecessor, introduced to stop transitive
chaining, is too strict for a real burst that drifts as people move; the 90
second span cap already prevents chaining, so the comparison is back to the
previous frame. And the 14-bit Hamming threshold is still a guess —
`photobook stats` now reports what adjacent-frame distances actually look like,
so it can be set from data.

## Open calibration work, once the real archive is here

- Banding threshold — currently a guess.
- `BURST_SECONDS` (25s) and `BURST_DISTANCE` (14 bits) — plausible, unvalidated.
- The verdict thresholds for sharpness and exposure want a pass over a few
  hundred real frames with a human disagreeing where it matters.
