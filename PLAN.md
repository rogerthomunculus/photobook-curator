# Photobook Curator — Design Plan (v0.6)

**Status:** Phase 0 is built and tested against a synthetic archive (see
[docs/phase0.md](docs/phase0.md) for what changed once it met Python). Phases
1–3 are designed, not written. Open questions at the bottom.

## Decisions locked (round 1)

| Question | Answer | What it changes |
|---|---|---|
| Automation ambition | **Both — assisted path first** | The layout engine is built around a `book.json` document model from day one; the automated order path becomes a later exporter, not a rewrite |
| Photo source | **Google Photos is the only copy** | Album-scoped Takeout becomes the *primary* Phase 0 ingest, not a Phase 3 fallback. The sidecar matcher is now critical path |
| Text in the book | **Light labels** — chapter titles, dates, place names | Adds a text-page template per chapter plus an *offline* reverse geocoder; no prose-review step |
| Backup quality | **Storage Saver** (confirmed) | There is no original to fall back to for *any* photo. DPI becomes a tiered soft constraint rather than a 300 DPI gate, recompression detection joins Stage 1, and the pipeline must never re-encode |
| Delivery | **Files, not an application** — a script that writes a folder of artifacts | No server, no launcher, no state machine, no update mechanism. Review is a self-contained HTML file you double-click. See §9 |
| Review | **Couch, with the people who were there** | Two passes with a batch boundary between them; the book plays, the room reacts. This removes the live re-solve requirement entirely |
| Orchestration | **A Claude Code skill, non-load-bearing** | Knows the stage order, the checkpoints and how to read the output. The CLI works identically without it |

**The job:** one trip → ~1,000–2,000 photos in Google Photos → a printed photo book
that is worth keeping, with as little human slog as possible.

---

## 1. The two things research changed about the plan

Before any architecture, two external constraints shape everything. Both are about
the *ends* of the pipeline, not the middle.

### 1.1 You can no longer read a Google Photos library programmatically

Google removed the `photoslibrary.readonly`, `photoslibrary.sharing`, and
`photoslibrary` scopes on **31 March 2025**. Calls relying on them now return
`403 PERMISSION_DENIED`. The Library API today only manages **media your own app
created**. There is no supported "give me every photo between 2026-06-01 and
2026-06-14" call anymore, for anyone.

The three surviving paths:

| Path | What you get | Cost |
|---|---|---|
| **Picker API** | User opens Google's own picker, selects items (default & hard cap **2,000 per session**), you get `mediaItems` + metadata and 60-minute `baseUrl`s to download | User does the selecting; no server-side date filter; downloads are re-encoded derivatives, not originals |
| **Google Takeout / Data Portability API** | Full-fidelity originals + `.json` sidecars with the real `photoTakenTime`, GPS, descriptions | Async archive job, multi-GB, notoriously messy filename↔sidecar matching, latency measured in hours |
| **Local folder** | Whatever is on disk / phone backup / an existing Takeout | Nothing — it just works |

**Design consequence — and Google Photos is the only copy, so this is now the
critical path.** The core is a local-folder pipeline behind a pluggable `Source`
interface, and the way photos reach that folder is:

> **Album-scoped Takeout.** In Google Photos, put the trip in one album (the
> auto-generated trip memory usually already is one). Then Takeout → Google
> Photos → *deselect all* → select that album only. You get ~1,200 originals plus
> sidecars in one archive instead of a multi-hundred-GB whole-library export.

That is a ten-minute manual chore once per trip, and in exchange it removes the
2,000-item Picker cap, the 60-minute URL expiry, the derivative-resolution
problem, and all future API-deprecation risk at once. **Recommended primary
ingest.** The Picker API stays on the roadmap as a convenience layer for
browsing and for a future multi-user version, not as the way bytes arrive.

Worth checking whether the **Data Portability API** exposes a Photos resource
group that can initiate that same album export programmatically — if it does,
the manual step disappears. Unverified; treat as a spike, not a dependency.

### 1.1a Storage Saver is confirmed — what that actually costs

The account is on Storage Saver, so this is settled fact rather than a risk to
manage. Three consequences, in order of how much they matter.

**Pixel count is mostly fine.** Storage Saver resizes anything above 16 MP down
to 16 MP and re-encodes everything; a 12 MP phone photo keeps its 4032×3024
dimensions and is only recompressed. Long edge at a given density:

| Source | Pixels | 300 DPI | 240 DPI | 200 DPI |
|---|---|---|---|---|
| 12 MP phone, uncropped | 4032×3024 | 13.4″ | 16.8″ | 20.2″ |
| 16 MP cap (48/50 MP resized) | 4619×3464 | 15.4″ | 19.2″ | 23.1″ |
| 12 MP cropped to half area | 2851×2138 | 9.5″ | 11.9″ | 14.3″ |
| 12 MP cropped to a quarter | 2016×1512 | 6.7″ | 8.4″ | 10.1″ |
| 1080p video / Live Photo frame | 1920×1080 | 6.4″ | 8.0″ | 9.6″ |

A full-bleed 11×14 needs 14″, so an uncropped phone shot lands at ~288 DPI —
visually indistinguishable from 300 at arm's length. **So drop the hard 300 DPI
gate**; it would reject usable photos. Use tiers instead: 300+ preferred, 240
fine, **200 the floor for a full-bleed page**, below 180 restricted to a quarter
page or smaller. DPI becomes a placement-size-aware ranking term with a hard
floor, not a binary filter.

**Crops and video frames are the real limit.** A half-area crop can still carry a
full-bleed page; a quarter-area crop cannot, and Storage Saver also caps video at
1080p, so any still pulled from a video or Live Photo is a ~2 MP asset — small
placements only. Worth knowing before we decide whether to mine Live Photos at all.

**Recompression is the genuine loss, and it's unrecoverable.** Google re-encoded
every one of these files once already. That shows up as banding in skies and
mottling in skin gradients — far more visible in print than on screen. Two
responses:

- **Detect it in Stage 1.** JPEG quantization tables are readable straight from
  the file, so we can estimate the encoder's quality setting, spot Google's own
  encoder signature, and flag heavy blocking or banding. Cheap, deterministic,
  and it tells us which photos should never carry a large placement even though
  their pixel count says they could. It also identifies any photos predating the
  switch to Storage Saver, which Google does *not* retroactively compress — so
  the archive is probably mixed, and the pipeline should measure per photo rather
  than assume.
- **Never re-encode.** Every subsequent JPEG save compounds the artifacts already
  baked in. Decode once, work in lossless intermediates, and encode exactly once
  at the end — PNG or maximum-quality JPEG for the spread-as-image export. This
  matters much more on a Storage Saver archive than it would on originals.

And it removes a mitigation the earlier draft leaned on: **there is no original
anywhere to fetch for a hero image.** What Takeout delivers is the best that will
ever exist. It also sharpens the case for disabling vendor auto-enhance, since
their sharpening will amplify existing JPEG artifacts rather than reveal detail.

### 1.2 No consumer photo-book vendor has a public API

Mixbook, Shutterfly, Printique, Artifact Uprising: no developer API, no bulk
layout import. The vendors that *do* expose APIs (Lulu, Peecho, Cloudprinter,
Blurb's PDF-to-Book) all take a **finished print-ready PDF**, not a photo set.

So "submit an order" forks into two genuinely different products:

- **Path A — Assisted (recommended first).** The app outputs a *curated, ordered,
  renamed* photo set plus a spread-by-spread layout plan and a proof PDF. You
  upload that set to Mixbook/Printique, let their auto-flow place them in your
  order, then nudge. Keeps access to the best consumer print quality; the last
  20% is manual.
- **Path B — Automated.** The app renders a real print PDF (bleed, gutter, CMYK
  or vendor ICC) and posts it to Blurb PDF-to-Book, Lulu, or Peecho. Fully
  programmatic ordering. Constrains you to POD-grade product — though Blurb
  layflat + ProLine paper is genuinely good, capped at **110 pages**.

**Design consequence:** build the layout engine to emit a **document model**
(`book.json`), then render that model to *both* a proof PDF and a print PDF. Path
A and Path B become two exporters over one engine, not two products. Ship A, keep
B one flag away. (This is the locked decision — "both, A first".)

### The trick that nearly collapses the A/B fork

Path A as described gives up layout control: you upload an ordered set and let
the vendor's auto-flow place it, then nudge. But there is a better move —
**export each page as a single flattened, full-bleed image at the vendor's exact
trim + bleed dimensions, and place one image per page.** Every consumer editor
supports a one-photo full-bleed page. That gets you *pixel-exact* reproduction of
your own layout inside Mixbook or Printique, with their print quality.

Same renderer, three targets:

| Target | Output | Vendor |
|---|---|---|
| Proof | Screen PDF | — |
| Path A | Flattened per-page images at trim+bleed | Mixbook / Printique |
| Path B | Print PDF with real bleed and ICC | Blurb / Lulu / Peecho |

Caveats to validate on the test print: **turn off the vendor's auto-enhance /
auto-correct on upload** or it will re-grade your composited pages; text becomes
raster (fine at 300 DPI, but no vendor spellcheck); and a full-spread image has
to be split at the exact trim into left and right pages unless the vendor offers
a true spread slot. Layflat binding makes this much easier — no gutter loss.

If this works, Path A stops being a compromise, and Path B becomes purely about
who prints it rather than how much control you keep. **Validate it before building
the rest of the layout engine** — and cheaply: most of the caveats above show up
in the vendor's own on-screen preview at maximum zoom, for free. See the tiered
validation in §6, Phase 0.5.

---

## 2. Reframing: quality auditing is the easy 10%

Your step (2), "audit the pictures for quality," is the part that's nearly solved
and nearly worthless on its own. Blur/exposure/duplicate filters will cut 1,200
photos to maybe 700. That doesn't help — you need ~150–250.

The hard problem is **selection under competing constraints**: the set must be
*good*, *non-redundant*, *representative of the whole trip*, *fair to every person
in the family*, and *sequenceable into a story*. Top-N by score fails at all five
— it gives you nine near-identical golden-hour shots of the same overlook and
zero photos of day 4.

That reframing is the thesis of the whole app: **the ranking model is a
commodity; the constrained selector is the product.**

---

## 3. Pipeline

```
0 INGEST ─→ 1 TECHNICAL ─→ 2 DEDUPE ─→ 3 AESTHETIC ─→ 4 SEMANTIC ─→
            QUALITY        & BURSTS     SCORING        UNDERSTANDING
                                                            │
5 NARRATIVE ─→ 6 SELECTION ─→ 7 LAYOUT ─→ 8 REVIEW ─→ 9 EXPORT
  STRUCTURE      (optimizer)    ENGINE      UI (human)   / ORDER
                     ↑_____________________________|
                          (locks & rejects re-run downstream)
```

Every stage writes to one SQLite database keyed by content hash. Every stage is
re-runnable in isolation. Nothing is destructive.

### Stage 0 — Ingest & normalize
- Content-addressed store (BLAKE3 of original bytes) → dedupe across devices for free.
- Decode HEIC/HEIF, RAW (rawpy/libraw), and Live Photos / motion photos (extract
  the still, keep the video pointer).
- Metadata via `exiftool`: capture time, sub-second, camera/lens, focal length,
  orientation, GPS.
- **Timezone correction from GPS** — trips cross time zones and phone clocks lie.
  Everything downstream (chaptering!) depends on a correct local timeline.
- **Multi-photographer merge with clock-skew correction**: two phones will be
  minutes apart. Estimate per-device offset by aligning GPS tracks, then merge
  into one timeline. Without this, chaptering and burst detection break.
- **Takeout sidecar matcher** — now critical path, not a nicety. Google strips or
  rewrites embedded EXIF on upload, so the `.json` sidecar is the *authoritative*
  source for capture time and GPS. Matching is inconsistent and needs a real
  resolver, not a naive join: `IMG123.jpg.json` vs `IMG123.json`, 51-char
  filename truncation, `-edited` variants, `(1)` collision suffixes, and
  live-photo pairs. Build it with a fixture set of the ugly cases and test it —
  a silent mismatch here corrupts the timeline and therefore every chapter.
- Junk pre-filter: screenshots (no EXIF camera + device-screen dimensions),
  receipts/documents/whiteboards, accidental pocket shots.

### Stage 1 — Technical quality
Cheap, deterministic, explainable. Runs on everything.
- Sharpness: variance-of-Laplacian **and** Tenengrad, computed **on the subject
  crop, not the whole frame**. Global blur metrics reject good shallow-depth-of-field
  portraits and accept sharp-background/blurry-subject failures. This one detail
  matters more than the choice of metric.
- Exposure: clipped-highlight and crushed-shadow fraction, histogram spread.
- Noise estimate; motion-blur direction (distinguishes camera shake from intentional pan).
- Resolution → **max printable size at 300/240/200 DPI**, stored as a tiered
  constraint for the layout engine (see §1.1a — a hard 300 DPI gate would reject
  usable Storage Saver photos).
- **Recompression estimate** — JPEG quantization-table analysis plus blocking and
  banding metrics. On a Storage Saver archive this is as important as sharpness:
  it separates photos that merely *look* big enough from photos that will
  actually hold up as a full-bleed page.
- Face-aware checks: face detect → per-face sharpness, **eyes-closed / blink
  detection**, gaze direction, and (for group shots) "how many faces are
  simultaneously acceptable." A blink in the only group photo of the trip is the
  single most common photo-book regret.

### Stage 2 — Dedupe & burst handling
- Perceptual hashes (pHash + dHash) → exact and near-identical.
- Time-clustered bursts: photos within N seconds + high embedding similarity =
  one burst. Pick a **best-in-burst** representative using Stage 1 + Stage 3
  scores, keep the rest as "alternates" (written to `alternates/`, ranked, so a
  swap is a drag in Finder).
- Cross-device dedupe: same scene shot by two phones is a burst too.
- Result: 1,200 → ~400–600 *distinct moments*, which is the real working set.

### Stage 3 — Aesthetic / IQA scoring
Use an off-the-shelf no-reference model — this is a well-served commodity
(`pyiqa` bundles most of them): MUSIQ, MANIQA, CLIP-IQA, LIQE, Q-Align, or a
LAION-style aesthetic head on CLIP embeddings.

Two warnings that matter more than model choice:
1. These models predict *generic prettiness*. They love sunsets and hate a
   perfectly composed shot of your kid eating gelato. Aesthetic score is **one
   term in an objective, never a filter.**
2. **Calibrate to you.** A ~15-minute pairwise-preference session ("which of these
   two belongs in the book?") over ~100 pairs, fit with Bradley–Terry, produces a
   personal ranking model that beats any off-the-shelf scorer for this task. Store
   the preference data — it compounds across years of trips. This is the highest
   ROI feature in the whole app and it's cheap.

### Stage 4 — Semantic understanding
- **Embeddings**: SigLIP / OpenCLIP vectors for every keeper → semantic
  similarity, diversity terms, and text search ("show me all the boat photos").
  Store in SQLite via `sqlite-vec`.
- **Face clustering**: local model (InsightFace/ArcFace) → stable per-person
  cluster IDs, named once and remembered across years. Powers the "everyone gets
  fair coverage" constraint.
- **VLM pass** (Claude, batched): one call per keeper returning *structured* JSON,
  not prose — `{scene, setting, activity, people_count, shot_type
  (wide/medium/detail/portrait), posed_vs_candid, landmark, food, notable_objects,
  emotional_register, hero_potential_0_5, one_line_caption}`.
  Structured outputs + the Batch API; cache the shared instruction prefix.

### Stage 5 — Narrative structure
- **Chapters**: cluster on (local time, GPS) — a gap of >4h or >30km starts a new
  chapter. Trips have a natural chapter grammar: travel day, city day, hike,
  dinner, museum, beach.
- Name and summarize each chapter with a VLM over the chapter's captions
  ("Day 3 — the drive over the pass and the lake we didn't plan on").
- Detect **motifs** across chapters (recurring subject, colour, "kid asleep in a
  different vehicle every day") — motifs make a book feel authored rather than
  chronological.
- **Place names, offline.** Locked decision: the book carries light labels —
  chapter title, date range, place name. Reverse-geocode from a local GeoNames
  extract rather than a web API, so the pipeline stays offline and reproducible
  and place names don't silently change between runs. Resolve to the level a
  human would say ("Lake Bled", not "Bled, Upper Carniola, Slovenia").
- Emit a **beat sheet**: chapter list, weight (share of pages) proportional to
  moment-count with damping so a 400-photo beach day doesn't eat the book.

### Stage 6 — Selection (the core)
Not top-N. A constrained submodular maximization:

**Maximize** `Σ (w₁·personal_pref + w₂·technical + w₃·hero_potential) + w₄·diversity(S)`

**Subject to:**
- total photos ≈ page_budget × avg_photos_per_page (± slack)
- per-chapter count within [floor, ceiling] of its narrative weight
- every named person appears ≥ K times; no person > X% of the book
- ≥ 1 strong portrait of each person
- no two selected photos exceed similarity threshold τ (dedupe as a *constraint*)
- shot-type mix per chapter (at least one wide establishing, some detail shots)
- every photo satisfies the DPI floor for its intended placement size
- at least one usable full-bleed candidate (aspect + resolution) per chapter

**Method:** greedy submodular (facility-location + MMR for the diversity term)
gets ~63% of optimum instantly and is trivially explainable; follow with local
search (swap/eject) for 30 seconds to close most of the gap. If the constraint set
grows hairier, it's an ILP (`OR-Tools CP-SAT`) — but start greedy.

**Explainability is a feature.** Every selection carries a reason ("best of a
12-shot burst; only wide shot of Day 5; Maya's only portrait"), and every
*rejection* is queryable. This is what makes the human review fast instead of
suspicious.

### Stage 7 — Layout
- **Spread grammar** — a small vocabulary of templates: full-bleed hero, hero+3
  supporting, symmetric 2-up, 3-up detail strip, 6-up grid, single-with-whitespace,
  and a **chapter-opener text page** carrying title, date range and place name
  (the locked "light labels" decision). Type is set once, in the engine — pick two
  faces and a scale and never touch it again.
- **Pacing**: a scored sequence problem, not a per-page one. Alternate density,
  never three grids in a row, open each chapter on a hero, close on something
  quiet. Score candidate sequences on rhythm + colour continuity between facing
  pages.
- **Aspect-aware packing** — placing a portrait-orientation photo into a landscape
  slot is where auto-layout tools produce garbage. Crop suggestions from a
  saliency/face map, never a centre crop.
- **Gutter and trim safety**: no face within the gutter zone of a spread; bleed
  margins per vendor spec; layflat vs perfect-bound changes the rules.
- Output: `book.json` document model → proof PDF (Path A) or print PDF (Path B).

### Stage 8 — Review (non-negotiable, and it is a file)
The whole system is a *proposal engine*, and a proposal you can't argue with is
worthless. But the review does not need an application — it needs `proof.html`:
one self-contained file, images embedded, that you double-click and put on a TV.

It is a **couch activity** with the people who were on the trip, in two passes:

1. **Watch.** The book plays full-screen, auto-advancing every ~8 seconds.
   Nobody edits. Anyone objects, the driver clicks; the spread is flagged and
   it moves on. 44 spreads in about fifteen minutes, and everyone has seen the
   whole arc — which is the only way to catch pacing, an underweighted day, or
   an ending that doesn't land. Per-spread deliberation never surfaces those,
   and reliably strands a group on spread three.
2. **Fix.** One button writes `rejects.txt`. Re-run. Flip through what changed.

**The batch boundary is a feature.** An earlier draft had rejections re-solving
live, which forced a stability requirement on the optimizer — a minimal-change
term so the book didn't reshuffle under the user. That requirement was entirely
self-inflicted: nobody is editing during pass one, so a thirty-second batch
re-run between passes does the same job with none of the complexity.

Two things the review must carry beyond the book itself:
- **`rejected.html`, sorted by margin** — not by filename. Someone will ask
  whether it threw away the good one. Fifteen seconds of scrolling settles it,
  and the fear is the main reason a tool like this gets abandoned.
- **`all-photos.html`, searchable** — the group's collective memory beats the
  model's, and "where's the one of the boat?" must not be a dead end. Filtering
  runs client-side over the captions and embeddings; no server.

Every rejection is a labelled pair. The review *is* the preference calibration,
which means the explicit pairwise session may never be needed — year one
bootstraps from the first book's rejects.

### Stage 9 — Export: a folder, not an application

```
Portugal-2026/
  proof.html          ← the couch session. double-click this.
  all-photos.html     ← searchable index of all 1,213
  assemble.md         ← how to build it at the vendor, for this exact run
  pages/
    page-001/  p001_1_hero_arrival-lisbon.jpg
    page-002/  p002_1_alfama-steps.jpg
               p002_2_laundry-lines.jpg
  alternates/         ← per page, ranked. swap by dragging.
  rejected.html       ← everything cut, sorted by how close it came
  book.json           ← document model; lets a re-run respect earlier edits
  00-triage.html  01-moments.html  02-story.html
```

**The page number goes in the filename, not just the folder.** Consumer editors
have you upload everything into one project library and then place it, so the
folder structure evaporates on the way in. With the page number in the name, 200
photos land in one alphabetical list that is already in book order.

If spread-as-image passes the free vendor check (§5), `pages/` instead holds one
flattened image per page and the handoff becomes 88 drags into full-bleed slots
with no arrangement decisions at all. Same document model, different renderer.

The bundle is self-explanatory in five years, inspectable in Finder, and does not
depend on this project still existing.

---

## 3a. The run, end to end

```
photobook build ~/Downloads/Takeout        # or: ask Claude to make the book
```

Four stages, **two checkpoints**, roughly 45 minutes of which about 3 are yours.
Both checkpoints are thirty-second glances that catch errors which otherwise
cost an hour.

| | Stage | Time | Output | Your part |
|---|---|---|---|---|
| **A** | Triage | 2 min | `00-triage.html` | **Checkpoint.** "1,213 photos, 12–24 June, 1,210 sidecars matched." Catches the Takeout that only exported 40 photos, before the expensive stages run. |
| **B** | Quality & dedupe | ~15 min | `01-moments.html` | Unattended. 1,213 frames → ~480 moments. |
| **C** | Understanding | ~25 min | `02-story.html` | **Checkpoint.** The chapter breakdown. If the drive to Sintra got merged into Sintra, every page allocation below is wrong — and it is a config line and a one-minute re-run to fix. |
| **D** | Select & lay out | ~1 min | the bundle | Unattended. |

`--preview` runs all four on a 150-photo sample and produces a 12-page book in
about five minutes. Worth doing first: it establishes whether the thing has any
taste before you commit the full run.

Timings are estimates until it runs on a real archive.

**Then:** couch session from `proof.html` → `rejects.txt` → `photobook build
--again` (30 s) → second pass → upload `pages/` to the vendor and work down
`assemble.md`. Call the manual assembly an hour for 88 pages; tedious but
mindless, and resumable.

### Why a Claude Code skill

`.claude/skills/photobook/` carries the stage order, the checkpoints, the
thresholds that mean "something is wrong", and the recovery paths — so the
annual-recall problem is solved by a conversation rather than by a launcher, a
bootstrapper and an auto-updater. Claude Code becomes the entry point, which is
why §6 no longer contains a desktop application.

Two constraints on it, both load-bearing:

- **The skill must never be load-bearing itself.** Every stage runs standalone
  from the CLI and produces identical output. The skill is convenience.
- **It orchestrates; it never analyses.** It runs the pipeline and interprets
  what the pipeline reports. It must never estimate a photo's quality by looking
  at it, or state a number no stage produced.

---

## 4. Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | The entire CV/ML ecosystem; no contest here |
| Store | SQLite + `sqlite-vec` + content-addressed blob dir | Single-file, portable, no services, survives being untouched for a year |
| Image I/O | Pillow + `pillow-heif` + `rawpy`, `exiftool` via subprocess | HEIC is non-negotiable for iPhone trips |
| CV | OpenCV, InsightFace (faces), `pyiqa` (IQA) | All local, all free |
| Embeddings | OpenCLIP / SigLIP, local, on GPU if present | ~1,200 images in minutes |
| VLM | Claude via Batch API + structured outputs | Captions, chaptering, spread narrative |
| Optimizer | Custom greedy + local search; OR-Tools CP-SAT if needed | Start simple |
| UI | FastAPI + a single-page React reviewer | Local-only, no auth, no deploy |
| PDF | ReportLab or `pikepdf`+Pillow compositing | Bleed/ICC control matters more than convenience |

**LLM cost per trip** (1,200 photos, one structured caption call each, images
downscaled to ~1024px ≈ ~1.1–1.5k input tokens each, ~200 output tokens each):

| Model | Standard | With Batch API (−50%) |
|---|---|---|
| Claude Opus 5 ($5/$25 per MTok) | ≈ $14 | **≈ $7** |
| Claude Sonnet 5 ($2/$10) | ≈ $6 | ≈ $3 |
| Claude Haiku 4.5 ($1/$5) | ≈ $3 | ≈ $1.50 |

Against a $120–250 book, this is noise — **use Opus 5 with the Batch API** and
spend the quality where it matters. Add prompt caching on the shared instruction
prefix and the numbers drop further. (Verify current pricing before relying on it.)

Everything except the VLM pass runs locally and offline.

---

## 5. Vendor recommendation (detail in `docs/vendors.md`)

**Update — professional labs change the picture.** Consumer vendors still have
no API of any kind. But professional photo labs do, because wedding
photographers order albums at volume and demand automation. **WHCC's Order
Submit API takes albums as `Individual Page JPG` and `Album 2 Page Spread`
attributes** — spread-as-image, natively, over an API. Prodigi and Gelato also
expose photo-book endpoints. See `docs/vendors.md` §1a; the open questions are
whether an individual can get WHCC credentials and what an album costs.

- **First book, Path A: Mixbook.** Best balance of print quality and editor in the
  2026 reviews, frequent 40–50% promos, layflat available, tolerant of a
  200-image upload. Best fit for "here is my curated set in order."
- **If print quality is the only axis: Printique.** Thickest pages, most accurate
  skin tones, layflat standard on every book. Slower, clunkier editor, notably
  pricier — which matters less when the app already made every decision.
- **If we go Path B (full automation): Blurb PDF-to-Book**, layflat + ProLine
  paper, ≤110 pages. The only route to a programmatic order at genuinely good
  photographic quality.
- **Artifact Uprising**: gorgeous materials and a real design point of view, but
  it's a *minimalist keepsake* format. Wrong shape for a 200-photo trip retrospective;
  right shape for a 40-photo "best of the year."
- **Shutterfly**: cheapest with promos, but 2026 reviews repeatedly flag
  inconsistent trim and weaker colour. Skip.

**Validation, cheaply.** An earlier draft called for ~$60 of test books across two
vendors. That's the wrong trade — the three questions have very different prices:

| Question | Substrate-dependent? | Cheapest honest answer |
|---|---|---|
| Does the vendor re-grade or resample my flattened pages? | No | **$0** — upload a test target, compare previews with auto-enhance on and off, and ask support directly |
| Does spread-as-image survive to paper? | No — upload processing is the same across their product line | **~$20** — the cheapest softcover they sell, on promo |
| How does *their paper* render my shadows and skin tones? | Yes | **$0 extra** — fold it into book one |

And drop the two-vendor bake-off. It's the expensive half, the published reviews
already give a defensible default, and if book one disappoints you switch next
year having lost nothing. **Never pay list price** — these vendors run 40–50% off
more or less continuously, which matters far more to total cost than the test.

For Path B specifically, Blurb publishes ICC profiles for its papers, so
soft-proofing is free and the print test matters less.

Target format: **11×14 or 12×12 layflat, 80–100 pages, ~180–220 photos.**

---

## 6. Phasing

**Phase 0 — "Is this even useful?" (a weekend).** Album-scoped Takeout in →
sidecar matcher, ingest, technical quality, dedupe/bursts → a contact sheet of the
top 300 with reasons. No ML beyond OpenCV, no UI beyond an HTML file. If this
doesn't already feel useful, the plan is wrong and we should find out for $0.
**Do the Takeout of last year's trip now** — it is the only step with a real
wall-clock delay, and it derisks the whole ingest story before any code exists.

**Phase 0.5 — validation, tiered (runs in parallel).** Buy the cheapest answer to
each question, not one expensive answer to all three:

- **Tier 0 — free, do it today.** Composite a test target (resolution wedges, a
  grey ramp, a fine grid, a skin-tone patch) into one page image at Mixbook's
  exact trim + bleed. Upload it twice, auto-enhance on and off, and compare the
  previews at maximum zoom. Catches trim misalignment, resampling, and whether
  auto-enhance is applied at upload — the failure modes that would sink
  spread-as-image. Ask their support the same questions in writing; it costs
  nothing and they answer.
- **Tier 1 — ~$20, only if Tier 0 is ambiguous.** The cheapest softcover Mixbook
  sells, on promo, containing the test target plus one normal spread and one
  flattened spread. Upload processing doesn't change across their product line,
  so a cheap book answers the technical question as well as an expensive one.
- **Tier 2 — $0 extra.** Fold the paper-and-colour question into **book one**:
  build it mostly as normal placements but make three or four pages flattened
  spread-images. One order answers both, and the shadow-lift correction it
  teaches you is reusable for every year after.

What you give up: book one may come back with slightly dark shadows. That's a
recoverable mistake next year, not a disaster — and cheaper than $60 of test
books to prevent it.

**Phase 1 — The brain.** Embeddings, face clustering, VLM captions, chaptering,
and the constrained selector. This is where the product actually lives. The
preference model bootstraps from the first book's rejections rather than from a
separate calibration chore.

**Phase 2 — The book.** Layout engine, spread grammar, pacing, crop suggestions,
`proof.html`, and the output bundle. First real book ordered.

**Phase 3 — Polish.** Spread-as-image renderer if the vendor check passes,
`all-photos.html` search, multi-year memory, Google Photos Picker as a second
source.

**Not planned, deliberately:** a desktop application, a local server, accounts,
cloud sync, a layout editor, or scoring-weight sliders. Every one of those
solves a problem the application itself would create. If the selection is wrong
the fix is rejecting things, not tuning parameters nobody has intuitions for.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| "Good" is subjective and the model will be confidently wrong | The proof is the argument surface: watch, flag, re-run. Rejections are labelled pairs, so it improves each year |
| A silent wrong answer beats a loud failure to nobody | Every fuzzy sidecar match records its strategy and confidence; contested guesses are withdrawn rather than resolved (see `docs/phase0.md`) |
| Storage Saver recompression (confirmed, unrecoverable) | Detect via quantization tables in Stage 1 and cap placement size accordingly; never re-encode in our own pipeline; disable vendor auto-enhance. There is no original to fall back to |
| Google changes API access again | Local-folder core, sources are plugins |
| Auto-layout produces subtly ugly spreads | Small hand-designed template vocabulary beats free-form placement; proof PDF before ordering |
| Face data leaving the machine | Face clustering is fully local; VLM pass is opt-out and can be run on crops-free thumbnails |
| Scope explosion (this plan is big) | Phase 0 is two days and proves the thesis |

---

## 8. Open questions — round 2

Answered in round 1: automation ambition (both, A first), source (Google Photos
only), text (light labels). Still open:

1. **Who's in the book** — one household, or multi-family trips? Drives how hard
   the fairness constraints in Stage 6 need to be, and whether face clusters need
   to survive people appearing in some years and not others.
2. **Videos / Live Photos**: extract stills and treat them as photos, or ignore
   them? Live Photos in particular often hide the better frame.
3. **Budget and format**: 80 pages at ~$150, or the big 12×12 layflat at ~$350?
   Page budget is a direct input to the selection optimizer and to
   `--page-inches`, so it is a real parameter rather than a preference. This is
   the one that blocks work.
5. **How many trips of backlog?** If there are five years of past trips sitting in
   Google Photos, the preference-calibration model gets much better much faster,
   and "catch-up mode" becomes a feature worth designing for.
