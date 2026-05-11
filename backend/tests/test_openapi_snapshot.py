"""Test that the committed OpenAPI snapshot matches the live schema.

This catches API drift the same way CI does, but runs in <50 ms on every
test run so a developer who forgets to regenerate the snapshot finds
out before they push. When the test fails the assertion message says
exactly how to refresh.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
SNAPSHOT = BACKEND_DIR / "openapi-snapshot.json"


def test_openapi_snapshot_is_up_to_date():
    """If this fails, regenerate the snapshot:

        cd backend && python export_openapi.py SNAPSHOT

    Then commit the updated openapi-snapshot.json. The snapshot is a
    drift detector — a non-empty diff means the API surface changed and
    the change should be deliberate (with a corresponding frontend or
    contract update).
    """
    assert SNAPSHOT.exists(), (
        "openapi-snapshot.json is missing. "
        "Run: cd backend && python export_openapi.py SNAPSHOT"
    )
    committed = json.loads(SNAPSHOT.read_text())

    # Re-run the export script in a subprocess so it runs in a fresh
    # interpreter and can't be polluted by other tests' monkeypatches
    # (notably the conftest patches that swap DEFINITIONS_DIR).
    result = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "export_openapi.py")],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_DIR),
        check=True,
    )
    live = json.loads(result.stdout)

    assert committed == live, (
        "OpenAPI snapshot is stale. Refresh with:\n"
        "    cd backend && python export_openapi.py SNAPSHOT\n"
        "Then review and commit the updated openapi-snapshot.json."
    )
