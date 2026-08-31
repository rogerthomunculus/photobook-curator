# photobook-curator

Turn a trip's worth of photos into a curated, print-ready photo book.

One trip → ~1,200 photos in Google Photos → a book of ~200 that is worth
keeping. The ranking model is a commodity; the constrained selector is the
product. See **[PLAN.md](PLAN.md)** for the full design and
**[docs/vendors.md](docs/vendors.md)** for the print-vendor research.

## Status

Phase 0 is built and tested against a synthetic archive. Phases 1–3 are
designed, not written.

| Stage | State |
|---|---|
| 0 · Ingest — sidecars, hashing, EXIF, timezone recovery | **built** |
| 1 · Technical quality — sharpness, exposure, recompression, DPI tiers | **built** |
| 2 · Dedupe & bursts — pHash/dHash, best-in-burst | **built** |
| 3 · Aesthetic scoring & preference calibration | planned |
| 4 · Semantics — embeddings, faces, captions | planned |
| 5 · Narrative — chapters, motifs, beat sheet | planned |
| 6 · Selection — constrained submodular optimisation | planned |
| 7 · Layout — spread grammar, pacing, crops | planned |
| 8 · Review UI | planned |
| 9 · Export — ordered set, proof PDF, print PDF | planned |

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

## Use

```bash
# 1. What actually arrived? Run this first, before committing to anything.
photobook triage  ~/Takeout

# 2. Hash, read EXIF and sidecars, populate the store (.photobook/db.sqlite)
photobook ingest  ~/Takeout

# 3. Quality metrics, perceptual hashes, burst grouping
photobook analyze ~/Takeout --page-inches 14

# 4. Contact sheet of every moment, with the reason for each verdict
photobook sheet   ~/Takeout
```

Every stage is re-runnable and nothing is destructive. The store lives in
`.photobook/` beside the archive.

## Print test target

For the free vendor validation described in PLAN.md §6 — upload it twice, with
the vendor's auto-enhance on and off, and compare previews at maximum zoom.

```bash
python tools/make_test_target.py --trim 11x14 --bleed 0.125 --label "Mixbook"
```

## Claude Code skill

`.claude/skills/photobook/` drives the pipeline conversationally — it knows the
stage order, the checkpoints, and how to read the output. It works from inside
this directory as-is. To reach it from anywhere:

```bash
ln -s "$PWD/.claude/skills/photobook" ~/.claude/skills/photobook
```

The skill is convenience, not a dependency: every stage runs standalone from the
CLI above and produces the same result.

## Tests

```bash
.venv/bin/python -m tests.test_takeout
```

Builds a synthetic Takeout archive reproducing every sidecar-naming pathology
seen in real exports, then asserts the matcher and the timestamp resolution
handle each one.
