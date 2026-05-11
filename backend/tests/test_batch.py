"""Tests for the batch extraction endpoint trio."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    main._invalidate_definitions_cache()
    main._render_cache.clear()
    main._text_cache.clear()
    main._doc_path_cache.clear()
    main._ocr_decision_cache.clear()
    main._invalidate_doc_listing_cache()
    with main._batch_jobs_lock:
        main._batch_jobs.clear()
    return TestClient(main.app)


def _wait_until(predicate, timeout=5.0, interval=0.02):
    """Spin-wait helper. We can't use threading.Event here because the
    worker doesn't expose one; we just poll the public state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _setup_two_docs(monkeypatch) -> list[str]:
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    ids = []
    for name in ("a.pdf", "b.pdf"):
        path = docs_dir / name
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        doc_id = main._get_document_id(name)
        main._render_cache[doc_id] = {
            "filename": name,
            "num_pages": 1,
            "page_dimensions": {1: {"width": 100, "height": 100}},
            "pdf_path": str(path),
            "page_images": {},
            "_sig": main._file_signature(path),
        }
        ids.append(doc_id)

    def fake_extract_text(_filepath):
        return [
            {"id": 0, "text": "INV-001", "type": "TextItem", "page": 1, "bbox": None},
        ], {1: {"width": 100, "height": 100}}

    monkeypatch.setattr(main, "_extract_text", fake_extract_text)
    return ids


def _make_definition(client):
    client.post(
        "/api/definitions",
        json={
            "document": {
                "document_type": "Test Type",
                "fields": [{"name": "invoice_id", "examples": ["INV-001"]}],
            }
        },
    )


def test_batch_happy_path_completes_and_records_results(client, monkeypatch):
    doc_ids = _setup_two_docs(monkeypatch)
    _make_definition(client)

    resp = client.post(
        "/api/extract/batch",
        json={"document_ids": doc_ids, "definition_id": "test_type"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert resp.json()["total"] == 2

    assert _wait_until(
        lambda: client.get(f"/api/extract/batch/{job_id}").json()["status"] == "done"
    )
    final = client.get(f"/api/extract/batch/{job_id}").json()
    assert final["completed"] == 2
    assert set(final["results"].keys()) == set(doc_ids)
    # Each result carries the per-field match (invoice_id matched "INV-001").
    for r in final["results"].values():
        inv = next(f for f in r["fields"] if f["name"] == "invoice_id")
        assert inv["extracted_value"] == "INV-001"
    assert final["errors"] == {}


def test_batch_records_per_doc_errors_without_failing_the_job(client, monkeypatch):
    doc_ids = _setup_two_docs(monkeypatch)
    # Add an unknown doc id to the batch — should be recorded under errors
    # without aborting the rest.
    doc_ids.append("does-not-exist")
    _make_definition(client)

    job_id = client.post(
        "/api/extract/batch",
        json={"document_ids": doc_ids, "definition_id": "test_type"},
    ).json()["job_id"]

    assert _wait_until(
        lambda: client.get(f"/api/extract/batch/{job_id}").json()["status"] == "done"
    )
    final = client.get(f"/api/extract/batch/{job_id}").json()
    assert final["completed"] == 3  # counter includes failures
    assert "does-not-exist" in final["errors"]
    assert len(final["results"]) == 2


def test_batch_rejects_unknown_definition(client, monkeypatch):
    doc_ids = _setup_two_docs(monkeypatch)
    resp = client.post(
        "/api/extract/batch",
        json={"document_ids": doc_ids, "definition_id": "no_such_def"},
    )
    assert resp.status_code == 404


def test_batch_rejects_empty_document_list(client):
    _make_definition(client)
    resp = client.post(
        "/api/extract/batch",
        json={"document_ids": [], "definition_id": "test_type"},
    )
    assert resp.status_code == 422


def test_batch_rejects_doc_list_above_cap(client, monkeypatch):
    _make_definition(client)
    monkeypatch.setattr(main, "_BATCH_MAX_DOCS", 3)
    resp = client.post(
        "/api/extract/batch",
        json={"document_ids": ["a", "b", "c", "d"], "definition_id": "test_type"},
    )
    assert resp.status_code == 413


def test_get_unknown_job_returns_404(client):
    assert client.get("/api/extract/batch/nope").status_code == 404


def test_cancel_marks_job_cancelled(client, monkeypatch):
    """Cancellation flag is honored before processing the next doc. The
    fake extractor returns instantly, so cancellation may land before or
    after the worker finishes the queue — we accept either, but assert
    the cancel call itself succeeds and the final status is terminal."""
    doc_ids = _setup_two_docs(monkeypatch)
    _make_definition(client)

    job_id = client.post(
        "/api/extract/batch",
        json={"document_ids": doc_ids, "definition_id": "test_type"},
    ).json()["job_id"]

    resp = client.delete(f"/api/extract/batch/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["cancelling"] is True

    assert _wait_until(
        lambda: client.get(f"/api/extract/batch/{job_id}").json()["status"]
        in ("done", "cancelled")
    )


def test_cancel_unknown_job_returns_404(client):
    assert client.delete("/api/extract/batch/nope").status_code == 404


def test_public_view_returns_detached_dicts(client):
    """Regression for the torn-dict race: `_public_batch_view` must
    snapshot `results` / `errors` so the worker can keep mutating the
    live job dict while FastAPI iterates the response. Without the
    `dict(...)` shallow copy, this raises RuntimeError when iterated
    while mutated."""
    job = {
        "id": "j1",
        "definition_id": "d",
        "document_ids": ["a"],
        "status": "running",
        "total": 1,
        "completed": 0,
        "results": {"a": {"ok": True}},
        "errors": {},
        "started_at": 0.0,
        "completed_at": None,
        "_cancelled": __import__("threading").Event(),
    }
    view = main._public_batch_view(job)
    # Mutating the source after the view is taken must NOT affect the view.
    job["results"]["b"] = {"ok": True}
    job["errors"]["c"] = "boom"
    assert view["results"] == {"a": {"ok": True}}
    assert view["errors"] == {}


def test_extra_keys_in_batch_body_rejected(client):
    _make_definition(client)
    resp = client.post(
        "/api/extract/batch",
        json={
            "document_ids": ["a"],
            "definition_id": "test_type",
            "definition_id_typo": "x",
        },
    )
    assert resp.status_code == 422
