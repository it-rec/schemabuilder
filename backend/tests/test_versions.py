"""Tests for the definition version archive + revision endpoints.

Every POST-with-overwrite / PATCH / DELETE on a definition snapshots the
old content into a hidden subdir of DEFINITIONS_DIR before writing the
new content. Two endpoints expose the archive: list (metadata only,
newest first) and fetch (full content for one version).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    main._invalidate_definitions_cache()
    return TestClient(main.app)


def _def(document_type: str, fields: list) -> dict:
    return {
        "document": {
            "document_type": document_type,
            "fields": fields,
        }
    }


def test_create_does_not_archive_when_no_prior_version_exists(client):
    """Pure POST (no overwrite, no prior file) doesn't write a version
    file — there's nothing to archive yet."""
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    versions = client.get("/api/definitions/test/versions").json()
    assert versions["items"] == []


def test_patch_archives_the_previous_version(client):
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    client.patch(
        "/api/definitions/test",
        json=_def("Test", [{"name": "x", "examples": ["INV-001"]}]),
    )
    versions = client.get("/api/definitions/test/versions").json()
    assert len(versions["items"]) == 1
    v = versions["items"][0]
    assert v["action"] == "patch"
    assert v["timestamp_ms"] > 0

    # And the archived content is the PRE-PATCH version (no examples).
    archived = client.get(f"/api/definitions/test/versions/{v['id']}").json()
    assert archived["document"]["fields"][0].get("examples") is None


def test_overwrite_archives_the_previous_version(client):
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    client.post(
        "/api/definitions?overwrite=true",
        json=_def("Test", [{"name": "x", "examples": ["v2"]}]),
    )
    versions = client.get("/api/definitions/test/versions").json()
    assert any(v["action"] == "overwrite" for v in versions["items"])


def test_delete_archives_the_previous_version(client):
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    client.delete("/api/definitions/test")
    versions = client.get("/api/definitions/test/versions").json()
    assert any(v["action"] == "delete" for v in versions["items"])


def test_versions_list_orders_newest_first(client):
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    for i in range(3):
        client.patch(
            "/api/definitions/test",
            json=_def("Test", [{"name": "x", "examples": [f"v{i}"]}]),
        )
    versions = client.get("/api/definitions/test/versions").json()["items"]
    timestamps = [v["timestamp_ms"] for v in versions]
    assert timestamps == sorted(timestamps, reverse=True)


def test_get_unknown_version_returns_404(client):
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    resp = client.get("/api/definitions/test/versions/9999999-patch")
    assert resp.status_code == 404


def test_get_version_with_malformed_id_returns_404(client):
    """The version id has a strict shape — anything else is 404, not 500."""
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    for bad in ("not-an-id", "../../etc/passwd", "abc-patch"):
        resp = client.get(f"/api/definitions/test/versions/{bad}")
        assert resp.status_code == 404, bad


def test_versions_endpoint_returns_404_for_unknown_definition(client):
    """A definition that never existed (no live file, no archive) is a
    typo — return 404 so the caller can distinguish that from a live
    definition with empty history. A *deleted* definition still has
    archived versions and returns 200 (covered by the
    test_delete_archives_the_previous_version round-trip)."""
    resp = client.get("/api/definitions/never_existed/versions")
    assert resp.status_code == 404


def test_versions_endpoint_returns_200_for_deleted_def_with_history(client):
    """Deleted definitions are browsable via history (for resurrection)."""
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    client.delete("/api/definitions/test")
    resp = client.get("/api/definitions/test/versions")
    assert resp.status_code == 200
    assert any(v["action"] == "delete" for v in resp.json()["items"])


def test_restore_round_trip_via_patch(client):
    """Use case: user PATCHes back to an archived version's content to
    roll back. The roll-back itself archives the current state, so
    history grows linearly."""
    client.post(
        "/api/definitions",
        json=_def("Test", [{"name": "x", "examples": ["original"]}]),
    )
    # Make a bad edit.
    client.patch(
        "/api/definitions/test",
        json=_def("Test", [{"name": "x", "examples": ["BROKEN"]}]),
    )
    versions = client.get("/api/definitions/test/versions").json()["items"]
    archived = client.get(
        f"/api/definitions/test/versions/{versions[0]['id']}"
    ).json()

    # Roll back by PATCHing with the archived content.
    client.patch("/api/definitions/test", json=archived)

    current = client.get("/api/definitions/test").json()
    assert current["document"]["fields"][0]["examples"] == ["original"]
    # The bad edit is now itself in history, plus the original archive.
    versions2 = client.get("/api/definitions/test/versions").json()["items"]
    assert len(versions2) == 2


def test_versions_dir_invisible_to_listing(client):
    """The hidden .versions/ subdir mustn't pollute the definition
    listing or break the loader."""
    client.post("/api/definitions", json=_def("Test", [{"name": "x"}]))
    client.patch("/api/definitions/test", json=_def("Test", [{"name": "y"}]))

    listing = client.get("/api/definitions").json()
    ids = {d["id"] for d in listing["items"]}
    assert ids == {"test"}  # NOT {"test", ".versions"} or anything weird
