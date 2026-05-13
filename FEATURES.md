# Feature Backlog

Proposed enhancements for Schema Builder. Ordered by impact within each
group; each item lists the rationale and the smallest sensible scope.

## Core functionality (highest leverage)

1. **Definition Editor in the frontend** — Currently definitions can only
   be uploaded as raw JSON via POST. A form-based editor with live
   validation against the Pydantic schema, examples/options chips, and a
   preview closes the largest UX gap.
2. **Interactive field mapping by click** — User selects text / a region
   in `DocumentViewer`; the backend stores the bounding box as an
   additional training example on the definition. Closes the loop
   between extraction and definition improvement.
3. **Bounding-box overlay on rendered pages** — Today matches surface
   only as text in `FieldsPanel`. Highlights drawn on the page PNG
   (hover-linked to the field row) make `match_reason` / `match_score`
   tangible.
4. **Target-table export endpoint** — `target_tables` with transforms is
   already modeled but never executed. Add
   `GET /api/documents/{id}/export?format=csv|json|parquet` to complete
   the workflow.

## Robustness & operations

5. **Batch extraction** — `POST /api/extract/batch` across many docs
   with Server-Sent Events for progress. The existing concurrency
   semaphore already bounds this.
6. **Persisted extraction results** — Each `/extract` recomputes from
   scratch. SQLite cache keyed on `(doc_id, definition_hash)` removes
   the expensive path on re-visits.
7. **Authentication + multi-user** — At minimum, an API-token scheme so
   `/api/definitions` isn't publicly writable in any real deployment.
8. **Document upload endpoint** — `test_documents/` is static today.
   `POST /api/documents` with size / MIME validation reusing
   `SCHEMABUILDER_MAX_BODY_BYTES`.

## Extraction quality

9. **Per-field confidence thresholds** — Today the best match always
   wins. Configurable minimum-score threshold per field plus a "review
   needed" indicator when matches are borderline.
10. **LLM fallback** — When the rule-based `match_reason` paths
    (`example_exact`, `date_format`, `option_substring`) return nothing,
    optionally call the Claude API with the field description + page
    text. Cache by `(doc, field, page)`.
11. **Regex / format slot on fields** — Alongside `examples` and
    `available_options`, add a `pattern` slot (IBAN, VAT-ID, ISO date)
    as an additional match reason.

## DX / UX

12. **Definition version diff view** — Keep a revision on every PATCH,
    show side-by-side diff in the UI.
13. **Dark mode + keyboard navigation** between pages / fields.
14. **OpenAPI client generation in CI** to replace the hand-maintained
    `frontend/src/services/api.js`.

---

Status: ideas captured 2026-05-11. Shipped on branch
`claude/suggest-project-features-HguxY`:
- #1 Definition Editor (Carbon modal + form, preserves extras)
- #2 Click-to-teach (overlay every text entry on the page; clicking
  opens a modal that appends the value to a chosen field's
  `examples`; the next extract re-runs automatically)
- #3 Bounding-box overlay (persistent ghost overlays for every match,
  reverse hover into FieldsPanel, label on active)
- #4 Target-table export (transform engine + JSON/CSV download)
- #9 Per-field confidence thresholds (`min_confidence` 0–1 on each
  field overrides the default 0.5 cutoff; the editor exposes it as a
  percent input; sub-threshold candidates surface as a "review" hint
  in the panel instead of being silently dropped)
- #11 Per-field regex pattern (`pattern` slot on each field; matched
  text scores 92 in the matcher and capture-group 1 — falling back to
  group 0 — becomes the extracted value, so an IBAN regex returns just
  the IBAN; Pydantic refuses uncompilable regexes at upload time and
  the editor surfaces compile errors live)
- #8 Document upload (POST /api/documents writes to TEST_DOCS_DIR with
  filename sanitization + collision suffix + a separate 50 MB body cap;
  DELETE /api/documents/{id} purges the render/text caches so a re-
  upload doesn't serve stale pages; sidebar gets an Upload button and
  a trash-can per row)
- #13 Dark mode + keyboard navigation (g10 ↔ g90 toggle in the header,
  persisted to localStorage, falls back to the OS prefers-color-scheme
  on first load; j/ArrowDown + k/ArrowUp cycle documents and
  ArrowLeft/Right scroll pages, all suppressed when focus is in a
  form control so they don't fight with typing)
- #5 Batch extraction (POST /api/extract/batch enqueues a job that
  runs sequentially behind the existing concurrency semaphore; GET /
  /api/extract/batch/{id} returns progress for polling; DELETE
  cancels after the current document. UI: "Run all" button in the
  sidebar, modal with a ProgressBar + cancel + JSON download of the
  aggregated results)
- #6 SQLite extraction cache (full /extract response cached on
  doc-signature + matcher-relevant definition hash; target_tables
  edits don't invalidate; survives restarts; `?refresh=true` bypass;
  delete-doc invalidates by signature; bounded by an LRU on
  created_at)
- #10 LLM fallback (per-field `use_llm_fallback` opt-in; when the
  rule-based matcher returns empty, calls Claude via the Anthropic
  SDK with structured outputs + prompt caching; lazy import so the
  SDK isn't a hard dependency; `SCHEMABUILDER_LLM_MODEL` /
  `_LLM_ENABLED` env knobs; FieldsPanel surfaces an "LLM" tag)
- #12 Definition version history (every overwrite / patch / delete
  snapshots the previous content to `definitions/.versions/{id}/`
  with a timestamp-ms filename; GET .../versions lists metadata,
  GET .../versions/{id} returns the full content; the editor gains
  a History modal with a side-by-side recursive-sorted-JSON diff
  and a Restore-this-version button that PATCHes back)
- #14 OpenAPI snapshot drift check (`backend/openapi-snapshot.json`
  committed; `export_openapi.py` regenerates it; a pytest case + a
  dedicated CI step diff the live schema against the snapshot and
  fail with a clear "run python export_openapi.py SNAPSHOT" message
  on drift)

Shipped on branch `claude/brainstorm-features-Klzn8`:
- Definitions Templates / Library (`backend/templates/` ships starter
  JSONs for invoice, receipt, business card, purchase order, bank
  statement; `GET /api/templates` / `GET /api/templates/{id}` expose
  them read-only; create-mode editor gets a "Start from template"
  dropdown that hydrates the draft via `fetchTemplate`)
- Per-field normalizer (`normalizer` slot on `FieldSpec`; supports
  `number`, `currency`, `date[:FORMAT]`, `percent`, `boolean`, `trim`,
  `lowercase`, `uppercase`; new `backend/normalizers.py` module;
  Pydantic rejects unknown keywords at upload time; matcher attaches
  `normalized_value` to every field result and array sub-field result;
  LLM-fallback values also pass through the normalizer; FE editor
  exposes the choice as a Dropdown; FieldsPanel renders the parsed
  value next to the raw text)
- Field dependencies (`visible_if` / `required_if` on `FieldSpec`;
  grammar supports `{field, equals|in|present|absent}` plus
  `{all|any: [...]}` combinators and a bare `true` for "always
  required"; `backend/dependencies.py` evaluates after the matcher +
  LLM fallback; suppressed fields have their `extracted_value` wiped
  and `match_reason: "hidden_by_dependency"`; required-but-missing
  fields surface `required_satisfied: false`; FE editor lets users
  type `field=value`, `field in a,b`, `field present`, or raw JSON;
  FieldsPanel hides suppressed rows and badges missing-required fields
  with a red `required` tag)
- Multi-page tables (`multi_page` flag + `header_pattern` regex on
  array fields; `_match_array_field` filters header rows by regex
  and, when `multi_page` is on, auto-skips rows whose tokens are a
  subset of sub-field-name tokens (the column-header repeat on page
  2+); array-field result includes `pages_spanned: [1,2,...]` and
  `is_multi_page` so the FE can badge "pages 1–3"; `header_pattern`
  validated as a compilable regex at upload time)
