# Schema Builder - Document Viewer

A full-stack web application for viewing and analyzing documents (PDF, DOCX, PPTX) with intelligent text extraction and visualization.

> **Features:** see the full list and detailed descriptions on the
> [GitHub Pages site](https://it-rec.github.io/schemabuilder/) (source in
> [`docs/`](docs/index.html)).

## Prerequisites

- Python 3.11+ (3.14 supported; see `backend/requirements.txt`)
- Node.js with npm
- **DOCX/PPTX rendering** requires a converter — the backend turns those formats into PDF before rasterizing with `pypdfium2`, and picks the converter by platform:
  - **Windows:** Microsoft Word and PowerPoint must be installed; conversion goes through Office COM automation (`pywin32`).
  - **Linux/macOS:** LibreOffice must be installed with `soffice` on `PATH` (e.g. `apt install libreoffice-writer libreoffice-impress`); conversion runs `soffice --headless --convert-to pdf`.

  PDF-only workflows need neither — there is no Office or LibreOffice dependency for PDFs.

## Getting Started

### 1. Backend

```bash
cd backend

# (Optional, for GPU acceleration) install a CUDA build of torch BEFORE
# installing the rest. Docling pulls torch transitively; pre-installing the
# right wheel avoids a slow CPU-only fallback.
pip install torch --index-url https://download.pytorch.org/whl/cu121
# Or for ROCm / MPS, see https://pytorch.org/get-started/locally/

# Install the remaining dependencies
pip install -r requirements.txt

# (Optional) Generate sample test documents
python generate_test_docs.py

# Start the API server
python main.py
```

The backend runs at http://localhost:8000.

### 2. Frontend

In a separate terminal:

```bash
cd frontend
npm install
npm start
```

The frontend runs at http://localhost:3000.

### 3. Open the App

Navigate to http://localhost:3000 in your browser.

### Run both at once (Windows)

`run.ps1` at the repo root starts the backend and frontend in parallel and stops both on Ctrl+C:

```powershell
.\run.ps1
```

## Project Structure

```
schemabuilder/
├── backend/
│   ├── main.py                 # FastAPI application
│   ├── requirements.txt        # Python dependencies
│   ├── generate_test_docs.py   # Test document generator
│   ├── definitions/            # Document class definitions (JSON)
│   ├── test_documents/         # Sample documents
│   └── tests/                  # pytest suite
└── frontend/
    ├── public/
    ├── src/
    │   ├── App.js              # Main layout (3-panel)
    │   ├── components/
    │   │   ├── DocumentList.js
    │   │   ├── DocumentViewer.js
    │   │   ├── FieldsPanel.js
    │   │   └── TextEntriesPanel.js
    │   └── services/
    │       └── api.js          # Backend API client
    └── package.json
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `REACT_APP_API_URL` | `http://localhost:8000` | Backend URL used by the frontend |
| `CORS_ALLOW_ORIGINS` | `http://localhost:3000` | Comma-separated list of origins allowed by the backend's CORS middleware |
| `DOCLING_DEVICE` | (auto) | Force accelerator: `cpu`, `cuda`, `mps`, or `auto`. Auto-detection order is CUDA → MPS → CPU |
| `DOCLING_DO_OCR` | (auto) | Force OCR on/off (`1`/`0`). Otherwise decided per-document by sampling extractable text via `pypdfium2`: digital PDFs take the fast no-OCR path, image-only PDFs route through the OCR converter automatically |
| `DOCLING_NUM_THREADS` | `os.cpu_count()` | Docling worker threads on CPU |
| `SCHEMABUILDER_RENDER_CACHE_MAX` | `64` | Max cached rendered-page entries (LRU) |
| `SCHEMABUILDER_TEXT_CACHE_MAX` | `64` | Max cached text-extraction entries (LRU) |
| `SCHEMABUILDER_PDF_CACHE_MAX` | `64` | Max cached DOCX/PPTX→PDF conversions (LRU; evicting also deletes the on-disk temp PDF) |
| `SCHEMABUILDER_SHUTDOWN_GRACE` | `30` | Seconds the lifespan finalizer waits for in-flight `/extract` calls to finish on SIGTERM before tearing down the converter |
| `SCHEMABUILDER_MAX_CONCURRENT_EXTRACTS` | `4` | Concurrency cap for `/extract`. Excess requests are rejected fast with `503 Retry-After: 5` so clients (and any upstream LB) can shed load instead of stacking up behind a serialized Docling pipeline |
| `SCHEMABUILDER_MAX_BODY_BYTES` | `2000000` | Reject request bodies larger than this many bytes with HTTP 413 (enforced via `Content-Length` so the body is never streamed into memory) |
| `SCHEMABUILDER_PREFETCH_WORKERS` | `4` | Background thread pool size for page warm-up + Docling text prefetch |
| `REACT_APP_API_TIMEOUT_MS` | `30000` | Per-request timeout for frontend `fetch()` calls (overrides via `AbortSignal`) |

The accelerator chosen at startup is logged once, so the answer to "is it actually using my GPU?" appears in the server log without waiting for the first `/extract`. Every response includes an `X-Request-ID` header (echoed from the incoming `X-Request-ID` if present, otherwise generated); log lines emitted while handling a request are prefixed with the same id so a single failed call can be traced end-to-end.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness probe. Always 200 if the process is up. Reports directory existence, warm-up status, ready flag, in-flight count |
| `GET` | `/ready` | Readiness probe (separate from liveness). Returns 503 with `Retry-After: 5` until the no-OCR Docling pipeline has finished loading; 200 once warm |
| `GET` | `/metrics` | Cache utilization, hit/miss counters, OCR decisions, completed extractions, concurrency rejects |
| `GET` | `/api/documents` | List documents in `test_documents/`. Paginated: `?limit=100&offset=0`. Response: `{items, total, limit, offset}` |
| `GET` | `/api/documents/{doc_id}` | Document metadata (number of pages, dimensions) |
| `GET` | `/api/documents/{doc_id}/pages/{page_no}` | Rasterized page PNG (ETag + max-age caching). `page_no` validated `>= 1`; out-of-range returns 400 |
| `POST` | `/api/documents/{doc_id}/extract` | Run Docling extraction + field matching for a definition. Body: `{"definition_id": "..."}`. Each field carries `match_reason` (e.g. `example_exact`, `date_format`, `option_substring`) and `match_score`. Surfaces `extraction_error` if Docling raised |
| `GET` | `/api/definitions` | List uploaded definitions. Paginated like `/api/documents` |
| `GET` | `/api/definitions/{def_id}` | Fetch a definition |
| `POST` | `/api/definitions` | Upload a definition. Validated against a Pydantic schema. Returns `409 Conflict` if the slug already exists; pass `?overwrite=true` to force-replace |
| `PATCH` | `/api/definitions/{def_id}` | Replace an existing definition. Returns `404` if the id is unknown (use POST to create) |
| `DELETE` | `/api/definitions/{def_id}` | Remove a definition |

All write paths on `/api/definitions` (POST/PATCH/DELETE) serialize on a single lock and publish via an atomic temp-file + `os.replace`, so concurrent mutations can't tear a JSON file or leave the in-memory cache observing a half-written state.

`/extract` is bounded by a process-global semaphore (`SCHEMABUILDER_MAX_CONCURRENT_EXTRACTS`); excess requests get `503 Retry-After: 5` rather than queuing behind the serialized Docling pipeline. The frontend HTTP client retries idempotent GETs on transient errors (5xx / network / timeout) with exponential backoff, honors per-request `AbortSignal` for cancellation, and applies a default 30s timeout (120s for `/extract`).

## Tests

Backend (pytest, no Docling models loaded):

```bash
cd backend
python -m pytest tests/
```

Frontend (CRA + Jest, plus ESLint with hooks/a11y plugins):

```bash
cd frontend
npm test           # interactive
npm run test:ci    # one-shot for CI
npm run lint
```

CI runs both suites on push/PR via `.github/workflows/ci.yml`.
