# Schema Builder - Document Viewer

A full-stack web application for viewing and analyzing documents (PDF, DOCX, PPTX) with intelligent text extraction and visualization.

## Prerequisites

- Python 3.11+ (3.14 supported; see `backend/requirements.txt`)
- Node.js with npm
- **Windows only:** Microsoft Word and PowerPoint must be installed for DOCX/PPTX rendering. The backend converts those formats to PDF via Office COM automation (`pywin32`) before rasterizing with `pypdfium2`. PDF-only workflows have no Office dependency.

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

The accelerator chosen at startup is logged once, so the answer to "is it actually using my GPU?" appears in the server log without waiting for the first `/extract`.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/documents` | List documents in `test_documents/` |
| `GET` | `/api/documents/{doc_id}` | Document metadata (number of pages, dimensions) |
| `GET` | `/api/documents/{doc_id}/pages/{page_no}` | Rasterized page PNG (ETag + max-age caching) |
| `POST` | `/api/documents/{doc_id}/extract` | Run Docling extraction + field matching for a definition. Body: `{"definition_id": "..."}` |
| `GET` | `/api/definitions` | List uploaded definitions |
| `GET` | `/api/definitions/{def_id}` | Fetch a definition |
| `POST` | `/api/definitions` | Upload a definition. Validated against a Pydantic schema before being written to disk |
| `DELETE` | `/api/definitions/{def_id}` | Remove a definition |

## Tests

Backend (pytest, no Docling models loaded):

```bash
cd backend
python -m pytest tests/
```

Frontend (CRA + Jest):

```bash
cd frontend
npm test
```
