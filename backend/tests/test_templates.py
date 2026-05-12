"""HTTP tests for the read-only templates catalog."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    # Point templates at a private tmp dir so test fixtures don't depend
    # on whatever ships under backend/templates/.
    tdir = tmp_path / "templates"
    tdir.mkdir()
    monkeypatch.setattr(main, "TEMPLATES_DIR", tdir)
    return TestClient(main.app), tdir


def _write(tdir: Path, tid: str, payload: dict) -> None:
    import json as _json

    (tdir / f"{tid}.json").write_text(_json.dumps(payload))


def test_list_templates_returns_metadata(client):
    c, tdir = client
    _write(
        tdir,
        "invoice",
        {
            "document": {
                "document_type": "Invoice",
                "document_description": "An invoice.",
                "fields": [{"name": "a"}, {"name": "b"}],
            }
        },
    )
    _write(
        tdir,
        "receipt",
        {"document": {"document_type": "Receipt", "fields": [{"name": "x"}]}},
    )
    resp = c.get("/api/templates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    ids = {item["id"] for item in body["items"]}
    assert ids == {"invoice", "receipt"}
    inv = next(i for i in body["items"] if i["id"] == "invoice")
    assert inv["field_count"] == 2
    assert inv["document_type"] == "Invoice"


def test_get_template_returns_full_payload(client):
    c, tdir = client
    _write(
        tdir,
        "po",
        {"document": {"document_type": "Purchase Order", "fields": []}},
    )
    resp = c.get("/api/templates/po")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "po"
    assert body["document"]["document_type"] == "Purchase Order"


def test_get_template_404_for_unknown(client):
    c, _ = client
    assert c.get("/api/templates/does_not_exist").status_code == 404


def test_get_template_rejects_path_traversal(client):
    c, _ = client
    # `..` is not in the allowed regex, so we get 404 not 5xx and don't
    # touch parent directories.
    assert c.get("/api/templates/..%2Fpasswd").status_code == 404
    assert c.get("/api/templates/with-dash").status_code == 404


def test_list_templates_skips_unreadable_files(client):
    c, tdir = client
    _write(tdir, "good", {"document": {"document_type": "G", "fields": []}})
    (tdir / "bad.json").write_text("{not json")
    body = c.get("/api/templates").json()
    assert {i["id"] for i in body["items"]} == {"good"}


def test_built_in_templates_load(monkeypatch):
    """The shipped templates directory should parse cleanly — every file
    must have document.document_type and a list of fields."""
    real = Path(__file__).resolve().parent.parent / "templates"
    monkeypatch.setattr(main, "TEMPLATES_DIR", real)
    client = TestClient(main.app)
    listing = client.get("/api/templates").json()
    assert listing["total"] >= 1
    for entry in listing["items"]:
        full = client.get(f"/api/templates/{entry['id']}").json()
        assert "document" in full
        assert isinstance(full["document"].get("fields"), list)
