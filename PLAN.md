# Photobook Curator — Design Plan (v0.1)

**Status:** planning only. Nothing built. Open questions at the bottom.

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

**Design consequence:** the app's core must be a **local-folder pipeline** with a
pluggable `Source` interface. Ship `LocalFolder` first, add `GooglePhotosPicker`
second, `Takeout` third. Do not build the system around a cloud API that can be
deprecated out from under it again — it already happened once.

**Print-quality consequence:** if the Google account is on *Storage Saver*, the
stored images are already recompressed and may be ~16MP-capped. Picker `baseUrl`
downloads are derivatives on top of that. For a full-bleed 11×14 page you want
≥ 300 DPI at placement size. So: **an explicit DPI budget check per placement**,
and a warning when a photo can only be used small. This is a real risk of the
Picker path and a strong argument for Takeout or original phone files for the
"hero" images.

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
(JSON), then render that model to *both* a proof PDF and a print PDF. Path A and
Path B become two exporters over one engine, not two products. Ship A, keep B one
flag away.

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
- Resolution → **max printable size at 300/240/180 DPI**. Stored as a hard
  constraint for the layout engine.
- Face-aware checks: face detect → per-face sharpness, **eyes-closed / blink
  detection**, gaze direction, and (for group shots) "how many faces are
  simultaneously acceptable." A blink in the only group photo of the trip is the
  single most common photo-book regret.

### Stage 2 — Dedupe & burst handling
- Perceptual hashes (pHash + dHash) → exact and near-identical.
- Time-clustered bursts: photos within N seconds + high embedding similarity =
  one burst. Pick a **best-in-burst** representative using Stage 1 + Stage 3
  scores, keep the rest as "alternates" (the review UI offers them as swaps).
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
  text/quote page.
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

### Stage 8 — Human review (non-negotiable)
A local web UI. Not optional, not phase 3 — the whole system is a *proposal
engine*, and a proposal you can't edit is worthless.
- Contact-sheet view of the selection, grouped by chapter, with reasons shown.
- **Lock** (never remove), **reject** (never suggest again, and remember why),
  **swap** (shows burst alternates and semantic neighbours ranked).
- Every edit re-runs Stages 6–7 downstream with locks respected, in < 2 seconds.
- The reject/lock log is training data for the preference model. Year two is
  better than year one automatically.

### Stage 9 — Export
- **Path A**: `01_001_hero_lake.jpg …` ordered/renamed set + printable proof PDF
  + a per-spread cheat sheet, ready to drag into a vendor editor.
- **Path B**: print PDF to vendor spec → Blurb / Lulu / Peecho API → order.
- Always also emit an archival JSON of the whole decision graph.

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

**Before the real order:** print the same 3 test spreads (one high-contrast, one
skin-tone-heavy, one dark/night) at your top two vendors. ~$60 total, and it
settles both vendor choice and how to profile colour. Do this once; reuse the
answer for years.

Target format: **11×14 or 12×12 layflat, 80–100 pages, ~180–220 photos.**

---

## 6. Phasing

**Phase 0 — "Is this even useful?" (a weekend).** Local folder in → ingest,
technical quality, dedupe/bursts → a contact sheet of the top 300 with reasons.
No ML beyond OpenCV, no UI beyond an HTML file. If this doesn't already feel
useful, the plan is wrong and we should find out for $0.

**Phase 1 — The brain.** Embeddings, face clustering, VLM captions, chaptering,
the constrained selector, the preference calibration session, the review UI.
This is where the product actually lives.

**Phase 2 — The book.** Layout engine, spread grammar, pacing, crop suggestions,
proof PDF, ordered export for Path A. First real book ordered from Mixbook.

**Phase 3 — Automation & polish.** Print PDF + Blurb/Lulu API ordering, Google
Photos Picker source, captions/text drafting, multi-year memory.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| "Good" is subjective and the model will be confidently wrong | Preference calibration + review UI with locks; the app proposes, you decide |
| Storage-Saver / Picker images too low-res for large prints | DPI budget as a hard constraint; prefer originals for heroes; warn early |
| Google changes API access again | Local-folder core, sources are plugins |
| Auto-layout produces subtly ugly spreads | Small hand-designed template vocabulary beats free-form placement; proof PDF before ordering |
| Face data leaving the machine | Face clustering is fully local; VLM pass is opt-out and can be run on crops-free thumbnails |
| Scope explosion (this plan is big) | Phase 0 is two days and proves the thesis |

---

## 8. Open questions for you

1. **Ingest**: are the trip photos also on a phone/laptop as originals, or is
   Google Photos genuinely the only copy? Changes Phase 0 substantially.
2. **Who's in the book** — one household, or multi-family trips? Drives how hard
   the fairness constraints need to be.
3. **Text**: captions, dates, place names, short narrative? Or photos only? The
   VLM can draft all of it, but it changes the layout grammar.
4. **Videos / Live Photos**: extract stills and treat as photos, or ignore?
5. **Automation ambition**: is "app hands me a curated set + proof, I finish in
   Mixbook" the win — or is a fully hands-off "one command → book on my doorstep"
   the actual goal? This is the single biggest fork in the plan.
6. **Budget/format**: 80 pages at ~$150, or the big 12×12 layflat at ~$350?
7. Is this **just for you**, or eventually something other people use? Multi-user
   changes the auth/hosting story completely — and makes the Google Photos
   Picker path mandatory rather than optional.
