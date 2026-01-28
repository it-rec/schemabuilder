# Schema Builder - Document Viewer

A full-stack web application for viewing and analyzing documents (PDF, DOCX, PPTX) with intelligent text extraction and visualization.

## Prerequisites

- Python 3
- Node.js with npm

## Getting Started

### 1. Backend

```bash
cd backend

# Install dependencies
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

# Install dependencies
npm install

# Start the development server
npm start
```

The frontend runs at http://localhost:3000.

### 3. Open the App

Navigate to http://localhost:3000 in your browser.

## Project Structure

```
schemabuilder/
├── backend/
│   ├── main.py                 # FastAPI application
│   ├── requirements.txt        # Python dependencies
│   ├── generate_test_docs.py   # Test document generator
│   └── test_documents/         # Sample documents
└── frontend/
    ├── public/
    ├── src/
    │   ├── App.js              # Main layout (3-panel)
    │   ├── components/
    │   │   ├── DocumentList.js      # Document selection sidebar
    │   │   ├── DocumentViewer.js    # Page rendering and navigation
    │   │   └── TextEntriesPanel.js  # Extracted text entries
    │   └── services/
    │       └── api.js          # Backend API client
    └── package.json
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| Backend port | `8000` | Configured in `backend/main.py` |
| Frontend port | `3000` | Default Create React App port |
| `REACT_APP_API_URL` | `http://localhost:8000` | Backend URL used by the frontend |
