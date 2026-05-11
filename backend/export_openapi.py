"""Dump the FastAPI app's OpenAPI schema to stdout (or to a file).

Usage:
    python export_openapi.py            # prints JSON to stdout
    python export_openapi.py SNAPSHOT   # writes JSON to ./openapi-snapshot.json

CI uses this to detect drift: regenerate the schema on every run and diff
it against the committed snapshot. A non-empty diff is a fatal mistake
unless the developer also refreshed `openapi-snapshot.json`.

This script imports `main` only to grab `main.app.openapi()`. The Docling
pipeline isn't started — it only runs inside the lifespan hook, which is
not invoked here. We do, however, force-shut the batch / prefetch thread
pools at exit so the script can terminate cleanly without leaking threads
in a CI runner.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Importing `main` triggers FastAPI's route registration. SCHEMABUILDER_DISABLE_LIFESPAN
# would be nice, but the lifespan body only runs when uvicorn starts the
# server — direct attribute access on `app.openapi()` is safe.
import main  # noqa: E402

SNAPSHOT_PATH = Path(__file__).parent / "openapi-snapshot.json"


def _emit_schema() -> dict:
    """Render and post-process the OpenAPI document.

    FastAPI bakes its current version into `info.version`. That string
    changes across FastAPI upgrades and would cause spurious CI diffs, so
    we normalize it. Same for FastAPI's auto-generated `info.title`,
    which we don't depend on staying stable byte-for-byte.
    """
    schema = main.app.openapi()
    schema.setdefault("info", {})["version"] = "snapshot"
    schema["info"]["title"] = main.app.title or "schemabuilder"
    return schema


def main_cli() -> int:
    schema = _emit_schema()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if len(sys.argv) > 1 and sys.argv[1] == "SNAPSHOT":
        SNAPSHOT_PATH.write_text(text)
        sys.stderr.write(f"wrote {SNAPSHOT_PATH}\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    # Shut down the module-level thread pools so the process can exit
    # without dangling non-daemon threads. atexit takes care of this in
    # practice, but being explicit makes CI diagnostics cleaner.
    try:
        sys.exit(main_cli())
    finally:
        for attr in ("_batch_pool", "_bg_executor"):
            pool = getattr(main, attr, None)
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
        # os._exit skips stdio flushing, so on Python 3.14 the schema written
        # via sys.stdout.write never reaches the parent process and the
        # OpenAPI-drift test reads an empty subprocess stdout. Flush before
        # the hard exit. (3.11/CI happened to flush anyway under the old
        # implementation; explicit is safer than relying on that.)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
