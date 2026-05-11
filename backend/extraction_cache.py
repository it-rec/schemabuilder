"""SQLite-backed cache for /extract results.

Each /extract call today recomputes the matcher from scratch, even though
the result is fully determined by (document bytes, definition that drives
matching). Caching keyed on (doc_signature, definition_hash) eliminates
the second-and-Nth call cost entirely. The text/render LRU caches in
main.py already cover the Docling-text path; this layer caches the *full*
response — including match_reason and rejected_candidate — so the cached
hit is a true zero-work return.

Why SQLite (vs an in-process dict):
- Survives restarts. The cache is most useful exactly when users
  re-explore yesterday's docs; an in-process dict throws all that away.
- Bounded on disk via SCHEMABUILDER_EXTRACTION_CACHE_MAX (LRU on
  created_at). No multi-GB drift over weeks.
- Single-file, no extra dependency.

Why not Redis: this is a single-process FastAPI app; adding a network hop
+ a sidecar isn't worth it for a small JSON blob cache.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

_DEFAULT_PATH = Path(__file__).parent / "extraction_cache.sqlite"
_DEFAULT_MAX_ENTRIES = 500


# Fields on a definition that actually affect matching. Anything else
# (target_tables, source_candidates, free-form extras) is irrelevant to the
# /extract output, so we strip it before hashing so an edit to target_tables
# doesn't invalidate every cached extraction.
_MATCHING_KEYS = {
    "name",
    "type",
    "description",
    "extraction_instructions",
    "examples",
    "available_options",
    "affix",
    "min_confidence",
    "pattern",
    "fields",
}


def _normalize_field(field: dict) -> dict:
    out = {}
    for k, v in field.items():
        if k not in _MATCHING_KEYS:
            continue
        if k == "fields" and isinstance(v, list):
            out["fields"] = [_normalize_field(sub) for sub in v if isinstance(sub, dict)]
        else:
            out[k] = v
    return out


def definition_hash(definition: dict) -> str:
    """Stable digest of the parts of a definition that drive matching.

    Sorted JSON encoding so dict key insertion order can't perturb the hash.
    Returns the hex digest of SHA-256 — short enough to print, long enough
    to never collide in practice.
    """
    doc = definition.get("document") or {}
    normalized = {
        "document_type": doc.get("document_type", ""),
        "fields": [
            _normalize_field(f)
            for f in (doc.get("fields") or [])
            if isinstance(f, dict)
        ],
    }
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_key(doc_signature: tuple, definition: dict) -> str:
    """Combined cache key: hash of the doc signature + the matching subset
    of the definition. The doc signature is `(name, mtime_ns, size)` from
    main._file_signature; baking it in invalidates the cache automatically
    when a document is re-uploaded under the same filename.
    """
    parts = json.dumps(
        [list(doc_signature), definition_hash(definition)],
        sort_keys=True,
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


class ExtractionCache:
    """SQLite-backed key/value cache with LRU-by-created_at eviction.

    Single-process safe via a thread-local connection; the SQLite WAL
    journal mode + a process-level write lock keep concurrent writers from
    stepping on each other.
    """

    def __init__(self, path: Optional[Path] = None, max_entries: Optional[int] = None):
        self.path = Path(path or os.getenv("SCHEMABUILDER_EXTRACTION_CACHE_PATH") or _DEFAULT_PATH)
        self.max_entries = (
            max_entries
            if max_entries is not None
            else max(
                10, int(os.getenv("SCHEMABUILDER_EXTRACTION_CACHE_MAX") or _DEFAULT_MAX_ENTRIES)
            )
        )
        self._write_lock = threading.Lock()
        self._tls = threading.local()
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path),
                isolation_level=None,  # autocommit; we wrap mutations explicitly
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._tls.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        conn = self._conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS extractions (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_extractions_created_at "
            "ON extractions(created_at)"
        )

    def get(self, key: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT value FROM extractions WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (TypeError, ValueError):
            # Corrupted row — drop it so the next put can overwrite cleanly.
            with self._write_lock:
                self._conn().execute("DELETE FROM extractions WHERE key = ?", (key,))
            return None

    def put(self, key: str, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        now = time.time()
        with self._write_lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO extractions(key, value, created_at) VALUES (?, ?, ?)",
                (key, payload, now),
            )
            self._evict_locked(conn)

    def invalidate(self, key: str) -> None:
        with self._write_lock:
            self._conn().execute("DELETE FROM extractions WHERE key = ?", (key,))

    def invalidate_by_doc_signature(self, doc_signature: tuple) -> int:
        """Drop every entry whose key includes this doc signature. Used
        when a document is deleted or re-uploaded under the same name.

        Returns the row count removed (mostly for tests / observability)."""
        # We can't reverse the hash, so we scan keys. The cache stays small
        # (<= max_entries), so an O(N) scan is fine.
        # Rebuild every existing key's prefix to compare.
        with self._write_lock:
            conn = self._conn()
            # Two-step: list all keys, recompute the doc-signature-prefix hash
            # for each via a callback. Faster: store doc_signature_hash as a
            # column. Simpler: just clear all keys whose value contains the
            # doc_id. We use the document_id field in the cached value as a
            # proxy.
            cur = conn.execute("SELECT key, value FROM extractions")
            to_delete = []
            sig_repr = json.dumps(list(doc_signature), sort_keys=True)
            for k, v in cur.fetchall():
                try:
                    obj = json.loads(v)
                except (TypeError, ValueError):
                    to_delete.append(k)
                    continue
                # Cached responses include `_doc_signature` (set by main.py
                # when populating the cache); compare against that. Keys
                # without the field (older cache rows) get conservatively
                # dropped so we don't serve stale data after a re-upload.
                if obj.get("_doc_signature") == list(doc_signature) or "_doc_signature" not in obj:
                    to_delete.append(k)
                    continue
                # Belt + suspenders: also drop if the signature literal
                # appears as a substring (defensive against legacy entries).
                if sig_repr in v:
                    to_delete.append(k)
            for k in to_delete:
                conn.execute("DELETE FROM extractions WHERE key = ?", (k,))
            return len(to_delete)

    def clear(self) -> None:
        with self._write_lock:
            self._conn().execute("DELETE FROM extractions")

    def size(self) -> int:
        row = self._conn().execute("SELECT COUNT(*) FROM extractions").fetchone()
        return int(row[0]) if row else 0

    def _evict_locked(self, conn: sqlite3.Connection) -> None:
        """Trim to max_entries by dropping oldest-created rows. Caller holds
        _write_lock."""
        n = conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0]
        if n <= self.max_entries:
            return
        excess = n - self.max_entries
        conn.execute(
            "DELETE FROM extractions WHERE key IN ("
            "  SELECT key FROM extractions ORDER BY created_at ASC LIMIT ?"
            ")",
            (excess,),
        )

    def close(self) -> None:
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._tls.conn = None


def get_default_cache() -> ExtractionCache:
    """Lazy module-level singleton. Tests can construct their own instance."""
    global _default_cache
    if _default_cache is None:
        _default_cache = ExtractionCache()
    return _default_cache


_default_cache: Optional[ExtractionCache] = None


def reset_default_cache() -> None:
    """Drop the singleton so the next get_default_cache() rebuilds it.
    Used by tests that override SCHEMABUILDER_EXTRACTION_CACHE_PATH."""
    global _default_cache
    if _default_cache is not None:
        _default_cache.close()
    _default_cache = None
