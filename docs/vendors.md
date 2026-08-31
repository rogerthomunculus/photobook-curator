# Photo book vendor research (Aug 2026)

Two separate questions, often conflated:

1. **Who prints the best book?** → consumer services, manual upload.
2. **Who lets software place an order?** → print-on-demand APIs, PDF in.

Almost nobody is good at both.

---

## 1. Consumer services (Path A — app curates, you finish in their editor)

| Vendor | Print quality | Editor | Price | Layflat | Verdict |
|---|---|---|---|---|---|
| **Printique** | Best tested — thickest pages, most accurate colour and skin tones | Slow, confusing | Highest | Standard on every book | Pick if quality is the only axis. The clunky editor hurts less when the app has pre-decided everything |
| **Mixbook** | Top-tier, consistent across image sizes, true-to-life colour | Best of the group; fast, flexible, AI auto-create | Mid, with frequent 40–50% promos | Optional | **Recommended default.** Best quality-per-unit-of-friction; handles a 200-image ordered upload well |
| **Artifact Uprising** | Premium finish and materials, real design point of view | Good but deliberately constrained | High | Yes | A *keepsake* format — minimal, few photos per page. Wrong shape for a 200-photo trip retrospective |
| **Shutterfly** | Weakest of the four — reviewers flag trim issues, seam misalignment, weaker colour, at a similar price to Mixbook | Simple, drag-and-drop | Low with constant promos | Limited | Skip |

Sources: [PetaPixel](https://petapixel.com/best-photo-book-services/),
[Tom's Guide](https://tomsguide.com/best-picks/best-photo-books),
[PhotoWorkout](https://www.photoworkout.com/best-photo-book-printing-services/),
[Reviewed](https://www.reviewed.com/home-outdoors/best-right-now/best-photo-books).

**None of these has a public API or a bulk layout-import format.** But automation
does *not* have to stop at "upload an ordered folder and let auto-flow place it":

> **Spread-as-image.** Render each page as a single flattened, full-bleed image at
> the vendor's exact trim + bleed size, then place one image per page. Every
> consumer editor supports a one-photo full-bleed page. This reproduces your own
> layout pixel-exactly inside Mixbook or Printique, at their print quality.

Vendor-selection criteria therefore gain three rows, all of which must be
confirmed on the test print:

- **Auto-enhance can be turned off on upload.** Otherwise the vendor re-grades
  your composited pages and the whole point is lost. Non-negotiable.
- **A true full-bleed single-image page exists** at the chosen trim size, with a
  published bleed spec.
- **Spread handling** — either a true spread slot, or clean splitting at trim into
  left/right pages. Layflat binding makes this dramatically easier: no gutter loss,
  no faces disappearing into the fold.

Mixbook and Printique both look viable on the first two; the third is the one to
verify with a real order.

---

## 2. Programmatic / PDF-in (Path B — app places the order)

| Vendor | Interface | Photo-book fit | Notes |
|---|---|---|---|
| **[Blurb PDF-to-Book](https://www.blurb.com/pdf-to-book)** | Upload a finished print PDF | **Best of the automatable options.** Layflat binding (true no-gutter spreads), Premium Lustre / Premium Matte / ProLine papers | Layflat capped at **110 pages**. Web upload; API access is not a self-serve public product |
| **[Lulu Print API](https://developers.lulu.com/)** | Free, well-documented REST API: cost calc, PDF validation, shipping, order webhooks | Trade/book-grade rather than photo-grade | The cleanest true end-to-end automation available |
| **[Peecho](https://www.peecho.com/print-api-documentation)** | REST print API, explicitly markets photo books | Good; positioned for products, not one-off personal orders | Worth a quote |
| **Cloudprinter** | REST API, broad printer network | Varies by partner | Least-verified of the four |

The tradeoff is honest: Path B gets you a fully hands-off pipeline at Blurb/Lulu
quality; Path A gets you Printique/Mixbook quality at the cost of ~30 minutes of
manual finishing.

**Both paths share the same layout engine** — the engine emits a `book.json`
document model; the exporters differ. This is the reason to design the layout
stage around a document model rather than around a vendor.

---

## 3. Recommendation

- **Book 1:** Path A → **Mixbook**, 11×14 or 12×12 layflat, 80–100 pages.
- **Keep Path B warm:** design the PDF renderer against **Blurb layflat specs**
  (≤110 pages fits the target anyway).
- **Don't buy a $60 bake-off.** Separate the questions by what they actually
  cost to answer:
  - *Does the vendor re-grade or resample my pages?* — **free**. Upload a test
    target twice, auto-enhance on and off, compare previews at max zoom; and ask
    support in writing, they answer.
  - *Does spread-as-image survive to paper?* — **~$20**, the cheapest softcover
    on promo. Upload processing doesn't vary across a vendor's product line, so
    a cheap book answers this as well as an expensive one.
  - *How does their paper render shadows and skin?* — **$0 extra**, folded into
    book one by making three or four of its pages flattened spread-images.
  - Skip the second vendor. It's the expensive half, the reviews give a
    defensible default, and switching in year two costs nothing.
- **Never pay list price.** These vendors discount 40–50% more or less
  continuously; timing the order matters more to total spend than any test.
- **Free substitutes worth using:** Blurb publishes ICC profiles for its papers
  (free soft-proofing, relevant to Path B), and several vendors send free or
  near-free paper swatch and sample kits — no help on your own images, but they
  settle paper and binding questions for nothing.
- **Printique's own quirk:** their books are bound true photographic prints
  rather than press sheets, so a single 8×10 print (~$3) is an unusually good
  proxy for one of their book pages. That shortcut does *not* transfer to
  press-printed vendors like Mixbook.
- **Colour management:** export sRGB unless the vendor publishes an ICC profile;
  soft-proof the dark spreads. Print is consistently darker than a monitor —
  a global +1/3 to +1/2 stop on shadow-heavy images is a normal correction.

---

## 4. Google Photos access (input side)

Relevant to ordering only in that it determines whether you have print-resolution
originals at all.

- The `photoslibrary.readonly` / `.sharing` / `photoslibrary` scopes were removed
  **31 March 2025**; calls relying on them return `403 PERMISSION_DENIED`.
  ([Google Developers Blog](https://developers.googleblog.com/en/google-photos-picker-api-launch-and-library-api-updates/),
  [Updates to the Google Photos APIs](https://developers.google.com/photos/support/updates))
- The **Picker API** is the replacement: user-driven selection, `maxItemCount`
  defaults to and is capped at **2,000**, `baseUrl`s live **60 minutes**, delete
  sessions when done. No server-side date-range query.
- **Data Portability API / Takeout** is the only full-fidelity route: originals
  plus `.json` sidecars carrying the true capture time and GPS (Google strips or
  rewrites embedded EXIF on upload). Filename↔sidecar matching is inconsistent
  (`IMG123.jpg.json` vs `IMG123.json`, truncation, `-edited` variants) and needs
  a real matcher, not a naive join.
- **Storage Saver** caps at 16 MP ≈ 4608×3456 — which is 15.4″ × 11.5″ at 300 DPI,
  so a full-bleed 11×14 page is fine on pixel count alone. The real exposure is
  cropped shots, legacy *Express quality* (~5 MP) uploads, and JPEG recompression
  artifacts, which show in skies and skin gradients in print far more than on
  screen. Check pixel dimensions *and* the account's current upload-quality
  setting at ingest.
- **Practical ingest recipe when Google Photos is the only copy:** put the trip in
  one album, then Takeout → Google Photos → deselect all → that album only. One
  manageable archive of originals plus sidecars, no 2,000-item Picker cap, no
  60-minute URL expiry, no derivative resolution loss.
