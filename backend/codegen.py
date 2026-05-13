"""Render a Schema Builder definition as a downstream artifact.

Turning the JSON definition into JSON Schema, SQL DDL, or TypeScript types
lets a data pipeline consume the same schema the extractor uses, without a
hand-maintained glue layer. The functions here are pure (no FastAPI / disk
access) so the endpoint, tests, and any future codegen target can compose
them.

Field-type inference is intentionally conservative — we look at `normalizer`
and `available_options` to upgrade an otherwise-string field to a typed one,
because the live `FieldSpec.type` is only meaningful for `"array"`. Anything
the matcher would extract as raw text stays a string downstream.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from normalizers import parse_spec

SqlDialect = Literal["postgres", "bigquery"]


SUPPORTED_FORMATS = frozenset(
    {"json-schema", "sql-postgres", "sql-bigquery", "typescript"}
)

_IDENT_SAFE_RE = re.compile(r"[^A-Za-z0-9_]")
_TS_BARE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _document_root(definition: dict[str, Any]) -> dict[str, Any]:
    doc = definition.get("document")
    return doc if isinstance(doc, dict) else {}


def _fields_list(maybe_fields: Any) -> list[dict[str, Any]]:
    if not isinstance(maybe_fields, list):
        return []
    return [
        f for f in maybe_fields
        if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]
    ]


def _is_array(field: dict[str, Any]) -> bool:
    return (field.get("type") or "").lower() == "array"


def _scalar_kind(field: dict[str, Any]) -> str:
    """Return one of: string, number, date, boolean.

    `normalizer` drives the upgrade; without one (or with a string-shaped
    normalizer like `trim`/`lowercase`/`uppercase`) the field stays a
    string so we don't pretend to know more than the definition does.
    """
    parsed = parse_spec(field.get("normalizer"))
    if parsed:
        name, _arg = parsed
        if name in ("number", "currency", "percent"):
            return "number"
        if name == "date":
            return "date"
        if name == "boolean":
            return "boolean"
    return "string"


# ---------- JSON Schema ----------


def _json_schema_for_field(field: dict[str, Any]) -> dict[str, Any]:
    if _is_array(field):
        sub = _fields_list(field.get("fields"))
        item: dict[str, Any] = {"type": "object"}
        if sub:
            item["properties"] = {
                f["name"]: _json_schema_for_field(f) for f in sub
            }
            item["additionalProperties"] = False
        schema: dict[str, Any] = {"type": "array", "items": item}
        if isinstance(field.get("description"), str) and field["description"]:
            schema["description"] = field["description"]
        return schema

    kind = _scalar_kind(field)
    schema: dict[str, Any]
    if kind == "number":
        schema = {"type": "number"}
    elif kind == "boolean":
        schema = {"type": "boolean"}
    elif kind == "date":
        schema = {"type": "string", "format": "date"}
    else:
        schema = {"type": "string"}

    if kind == "string":
        opts = field.get("available_options")
        if isinstance(opts, list) and opts:
            schema["enum"] = [str(o) for o in opts]
        pat = field.get("pattern")
        if isinstance(pat, str) and pat:
            schema["pattern"] = pat

    if isinstance(field.get("description"), str) and field["description"]:
        schema["description"] = field["description"]
    ex = field.get("examples")
    if isinstance(ex, list) and ex:
        schema["examples"] = [v for v in ex]
    return schema


def to_json_schema(definition: dict[str, Any]) -> dict[str, Any]:
    doc = _document_root(definition)
    fields = _fields_list(doc.get("fields"))
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": doc.get("document_type") or "Document",
        "type": "object",
        "properties": {f["name"]: _json_schema_for_field(f) for f in fields},
    }
    if isinstance(doc.get("document_description"), str) and doc["document_description"]:
        schema["description"] = doc["document_description"]
    return schema


# ---------- SQL DDL ----------

_POSTGRES_SCALAR = {
    "string": "TEXT",
    "number": "NUMERIC",
    "date": "DATE",
    "boolean": "BOOLEAN",
}
_BIGQUERY_SCALAR = {
    "string": "STRING",
    "number": "NUMERIC",
    "date": "DATE",
    "boolean": "BOOL",
}


def _sql_safe(name: str, fallback: str) -> str:
    cleaned = _IDENT_SAFE_RE.sub("_", name).strip("_")
    return cleaned or fallback


def _sql_quote(name: str, dialect: SqlDialect) -> str:
    if dialect == "postgres":
        return f'"{name}"'
    return f"`{name}`"


def _sql_comment_safe(text: str) -> str:
    # Used for inline `--` comments only — strip newlines so the comment
    # can't accidentally bleed into the next line of DDL.
    return text.replace("\n", " ").replace("\r", " ").strip()


def _sql_string_literal(text: str) -> str:
    return "'" + text.replace("'", "''").replace("\n", " ") + "'"


def to_sql_ddl(definition: dict[str, Any], dialect: SqlDialect) -> str:
    """Render the definition as a CREATE TABLE block (one parent table plus
    one child table per `type: array` field). Array fields become a side
    table keyed by `doc_id` because target warehouses are happier with flat
    rows than nested arrays, and the existing target-table export already
    follows the same convention.
    """
    if dialect not in ("postgres", "bigquery"):
        raise ValueError(f"Unsupported dialect: {dialect!r}")
    doc = _document_root(definition)
    doc_type = doc.get("document_type") or "document"
    main_table = _sql_safe(doc_type.lower(), "document")
    fields = _fields_list(doc.get("fields"))
    scalar_map = _POSTGRES_SCALAR if dialect == "postgres" else _BIGQUERY_SCALAR
    doc_id_type = "TEXT" if dialect == "postgres" else "STRING"

    out: list[str] = []
    desc = doc.get("document_description")
    if isinstance(desc, str) and desc:
        out.append(f"-- {_sql_comment_safe(desc)}")

    def _render_table(table_ident: str, columns: list[tuple[str, str, str | None]]) -> str:
        # columns: [(quoted_name, sql_type, optional description)]
        col_lines = []
        for quoted, sql_type, col_desc in columns:
            line = f"  {quoted} {sql_type}"
            if dialect == "bigquery" and col_desc:
                line += f" OPTIONS(description={_sql_string_literal(col_desc)})"
            col_lines.append(line)
        return f"CREATE TABLE {table_ident} (\n" + ",\n".join(col_lines) + "\n);"

    parent_cols: list[tuple[str, str, str | None]] = [
        (_sql_quote("doc_id", dialect), doc_id_type, "Extracted document identifier."),
    ]
    for f in fields:
        if _is_array(f):
            continue
        col_name = _sql_safe(f["name"], "col")
        sql_type = scalar_map[_scalar_kind(f)]
        col_desc = f.get("description") if isinstance(f.get("description"), str) else None
        parent_cols.append((_sql_quote(col_name, dialect), sql_type, col_desc))
    out.append(_render_table(_sql_quote(main_table, dialect), parent_cols))

    for arr in fields:
        if not _is_array(arr):
            continue
        sub_name = _sql_safe(arr["name"].lower(), "items")
        child_ident = _sql_quote(f"{main_table}_{sub_name}", dialect)
        arr_desc = arr.get("description")
        if isinstance(arr_desc, str) and arr_desc:
            out.append(f"-- {_sql_comment_safe(arr_desc)}")
        child_cols: list[tuple[str, str, str | None]] = [
            (_sql_quote("doc_id", dialect), doc_id_type, "Parent document identifier."),
        ]
        for sub_f in _fields_list(arr.get("fields")):
            col_name = _sql_safe(sub_f["name"], "col")
            sql_type = scalar_map[_scalar_kind(sub_f)]
            col_desc = (
                sub_f.get("description")
                if isinstance(sub_f.get("description"), str)
                else None
            )
            child_cols.append((_sql_quote(col_name, dialect), sql_type, col_desc))
        out.append(_render_table(child_ident, child_cols))

    return "\n\n".join(out) + "\n"


# ---------- TypeScript ----------


def _ts_pascal_case(name: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", name)
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    if not out:
        return "Document"
    if out[0].isdigit():
        out = "_" + out
    return out


def _ts_quote_key(name: str) -> str:
    if _TS_BARE_IDENT_RE.match(name):
        return name
    return json.dumps(name)


def _ts_scalar_type(field: dict[str, Any]) -> str:
    kind = _scalar_kind(field)
    if kind == "number":
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "string":
        opts = field.get("available_options")
        if isinstance(opts, list) and opts:
            return " | ".join(json.dumps(str(o)) for o in opts)
    return "string"


def _ts_field_type(field: dict[str, Any], indent: int) -> str:
    if _is_array(field):
        sub = _fields_list(field.get("fields"))
        if not sub:
            return "Array<Record<string, string | null>>"
        return "Array<" + _ts_object_body(sub, indent + 2) + ">"
    return _ts_scalar_type(field)


def _ts_object_body(fields: list[dict[str, Any]], indent: int) -> str:
    pad = " " * indent
    closing_pad = " " * max(0, indent - 2)
    lines = []
    for f in fields:
        key = _ts_quote_key(f["name"])
        # Every field is optional + nullable because the matcher can miss; a
        # consumer that wants `Required<…>` can wrap it on their side.
        lines.append(f"{pad}{key}?: {_ts_field_type(f, indent)} | null;")
    return "{\n" + "\n".join(lines) + "\n" + closing_pad + "}"


def to_typescript(definition: dict[str, Any]) -> str:
    doc = _document_root(definition)
    name = _ts_pascal_case(doc.get("document_type") or "Document")
    fields = _fields_list(doc.get("fields"))
    body = _ts_object_body(fields, indent=2)
    header = ""
    desc = doc.get("document_description")
    if isinstance(desc, str) and desc.strip():
        header = f"/** {desc.strip()} */\n"
    return f"{header}export interface {name} {body}\n"


# ---------- Dispatch ----------


def render(definition: dict[str, Any], fmt: str) -> tuple[bytes, str, str]:
    """Return (body_bytes, media_type, filename_stem) for `fmt`.

    Kept here so the endpoint stays a thin wrapper and tests can exercise
    the full pipeline without going through HTTP.
    """
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"Unsupported format: {fmt!r}")
    doc_type = _document_root(definition).get("document_type") or "document"
    stem = _IDENT_SAFE_RE.sub("_", doc_type.lower()).strip("_") or "document"
    if fmt == "json-schema":
        body = json.dumps(to_json_schema(definition), indent=2).encode("utf-8")
        return body, "application/json", f"{stem}.schema.json"
    if fmt == "sql-postgres":
        body = to_sql_ddl(definition, "postgres").encode("utf-8")
        return body, "text/plain; charset=utf-8", f"{stem}.postgres.sql"
    if fmt == "sql-bigquery":
        body = to_sql_ddl(definition, "bigquery").encode("utf-8")
        return body, "text/plain; charset=utf-8", f"{stem}.bigquery.sql"
    # typescript
    body = to_typescript(definition).encode("utf-8")
    return body, "text/plain; charset=utf-8", f"{stem}.ts"
