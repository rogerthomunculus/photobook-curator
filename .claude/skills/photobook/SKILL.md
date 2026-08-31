---
name: photobook
description: Build a curated photo book from a Google Photos Takeout export. Use when the user wants to make a photo book, photobook, or printed photo album from a trip; when they mention a Takeout export of photos; or when they ask to curate, cull, or pick the best photos out of a large set for printing. Drives the local photobook-curator pipeline and interprets what it reports.
---

# Photobook Curator

Turn a trip's Takeout export into a curated, ordered, print-ready photo book.

This is an **annual** task. The user will not remember the procedure, the flags,
the checkpoints, or what the numbers mean. That is what this skill is for.

## What you do and do not do

**The pipeline does the analysis. You do the judgment.**

Run the CLI. Read what it reports. Say whether it looks right. Decide what to
re-run. Never reimplement any stage of the analysis yourself, never estimate a
photo's quality by looking at it, and **never state a number the pipeline did
not produce.** If a stage fails, report the failure — do not paper over it with
a plausible-sounding summary.

The pipeline must also work without you. If the user runs the CLI directly they
get the same result; this skill is convenience, never a dependency.

## Setup

The project lives at `photobook-curator`. Check for it before anything else; if
it is missing, ask the user where it is rather than guessing.

```bash
cd photobook-curator
python3 -m venv .venv && .venv/bin/pip install -qe .   # first run only
```

## Procedure

Four stages, two checkpoints. **Stop at both checkpoints and show the user what
came back** — each is a thirty-second glance that catches an error that would
otherwise cost an hour.

### Stage A — triage (~2 min)

```bash
.venv/bin/photobook triage <archive>
```

Report the counts in plain language and judge them against what the user
expected. Things that mean something is wrong:

| Signal | What it means |
|---|---|
| Photo count far below expectation | The Takeout album selection did not stick. Re-export before doing anything else. |
| Date range narrower than the trip | Same — probably a partial album. |
| Sidecar match rate below ~95% | Unusual. Look at the strategy breakdown in `triage.html` before continuing. |
| Many `contested(...)` strategies | Two files claiming one truncated sidecar. Those are deliberately left unmatched; they will fall back to EXIF or be reported undated. |
| `tz_source` mostly `longitude` | EXIF was stripped. Local timestamps are then rough — up to two hours out, enough to shift a photo across a day boundary and split a chapter wrongly. Warn the user explicitly. |

**Do not continue past a bad triage.** Re-exporting costs the user an hour of
waiting; analysing the wrong archive costs more.

### Stage B — quality and dedupe (~15 min, unattended)

```bash
.venv/bin/photobook ingest  <archive>
.venv/bin/photobook analyze <archive> --page-inches <book long edge>
.venv/bin/photobook sheet   <archive>
```

`--page-inches` matters: it sets the placement caps. 14 for an 11×14, 12 for a
12×12. Ask if you do not know the book size.

Report the collapse ratio (frames → moments) and the verdict split, and **treat
an implausible result as a failure, not a pass**:

- **Near-zero rejects** means the quality stage is rubber-stamping, not that the
  archive is flawless. Any real trip has blurry frames, pocket shots and
  screenshots.
- **A collapse ratio near 1:1** means bursts are not being detected. 2:1 to 3:1
  is normal.
- **Almost everything collapsing** means the hashing is broken.

Sharpness verdicts are relative to the archive's own median, so they adapt to
the camera — but the ratios themselves are provisional. When a result looks
wrong, do not invent new numbers. Run the instruments:

```bash
.venv/bin/photobook stats     <archive>   # the real distribution of every metric
.venv/bin/photobook calibrate <archive>   # what each metric ranks worst
.venv/bin/photobook rescore   <archive> --reject-ratio 0.25 --review-ratio 0.5
```

`rescore` re-assigns verdicts without re-analysing, so sweeping thresholds is
seconds rather than minutes. **The calibration sheet is the falsifiable test:**
if the photos the metric calls softest are not soft to the user's eye, the
metric is wrong and no threshold will fix it — say so rather than tuning.

### Stage C — understanding (~25 min, unattended) — NOT YET BUILT

Embeddings, face clusters, captions, chapters. When it exists it writes a story
summary and pauses. This is the second checkpoint, and the high-leverage one: if
chapters are wrong, every page allocation downstream is wrong. Show the user the
chapter list and offer to merge or split before continuing.

### Stage D — select and lay out (~1 min) — NOT YET BUILT

Produces the output bundle described in PLAN.md §9.

## The review loop

The user reviews on a sofa with the people they travelled with, from
`proof.html` — a self-contained file, not an app. Two passes:

1. They watch the book through and flag spreads. No editing.
2. They export `rejects.txt`, you re-run, they check the changes.

Re-running with a rejects file re-solves around those rejections. Expect several
rounds; each is fast. Do not try to talk them out of a rejection or explain why
the selection was right — the point of the loop is that their judgment wins.

## When something breaks

Report exactly what failed and what you tried. Useful context to gather: the
Python version, the triage summary, the failing command and its full error, and
whether the archive path is what the user thinks it is. Never invent a
workaround that changes the analysis — a wrong book is worse than a late one.

## Hard rules

- Never claim a stage ran that did not run.
- Never report counts, verdicts or chapter names you did not read from output.
- Never modify the user's Takeout archive. The pipeline only writes to
  `.photobook/` and the output bundle.
- Never skip a checkpoint to save time.
