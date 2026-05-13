"""Tests for the codegen module + GET /api/definitions/{id}/codegen.

Covers the pure rendering functions (JSON Schema, Postgres DDL, BigQuery DDL,
TypeScript) and the HTTP wrapper around them. Type inference is driven by
`normalizer` / `available_options`, and array fields fan out into one child
table (SQL) / nested array (JSON Schema, TS) per definition.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import codegen
import main

SAMPLE_DEFINITION = {
    "document": {
        "document_type": "Invoice",
        "document_description": "Sample invoice schema.",
        "fields": [
            {
                "name": "invoice_number",
                "description": "Unique identifier.",
                "examples": ["INV-001"],
                "pattern": r"INV-\d+",
            },
            {
                "name": "invoice_date",
                "description": "Issue date.",
                "normalizer": "date",
            },
            {
                "name": "total_amount",
                "normalizer": "currency",
                "description": "Total due.",
            },
            {
                "name": "currency",
                "available_options": ["USD", "EUR"],
            },
            {
                "name": "paid",
                "normalizer": "boolean",
            },
            {
                "name": "line_items",
                "type": "array",
                "description": "Billed line items.",
                "fields": [
                    {"name": "description"},
                    {"name": "quantity", "normalizer": "number"},
                    {"name": "amount", "normalizer": "currency"},
                ],
            },
        ],
    }
}


# ---------- pure functions ----------


def test_json_schema_infers_types_from_normalizer():
    schema = codegen.to_json_schema(SAMPLE_DEFINITION)
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["title"] == "Invoice"
    assert schema["description"] == "Sample invoice schema."
    props = schema["properties"]
    assert props["invoice_number"]["type"] == "string"
    assert props["invoice_number"]["pattern"] == r"INV-\d+"
    assert props["invoice_number"]["examples"] == ["INV-001"]
    assert props["invoice_date"] == {
        "type": "string",
        "format": "date",
        "description": "Issue date.",
    }
    assert props["total_amount"]["type"] == "number"
    assert props["currency"]["enum"] == ["USD", "EUR"]
    assert props["paid"]["type"] == "boolean"
    line_items = props["line_items"]
    assert line_items["type"] == "array"
    assert line_items["items"]["type"] == "object"
    assert line_items["items"]["additionalProperties"] is False
    assert line_items["items"]["properties"]["quantity"]["type"] == "number"


def test_json_schema_empty_definition_returns_minimal_skeleton():
    schema = codegen.to_json_schema({"document": {"document_type": "Blank"}})
    assert schema["properties"] == {}
    assert "description" not in schema


def test_sql_postgres_emits_parent_and_child_tables():
    ddl = codegen.to_sql_ddl(SAMPLE_DEFINITION, "postgres")
    # Parent table includes scalar columns only.
    assert 'CREATE TABLE "invoice"' in ddl
    assert '"invoice_number" TEXT' in ddl
    assert '"invoice_date" DATE' in ddl
    assert '"total_amount" NUMERIC' in ddl
    assert '"paid" BOOLEAN' in ddl
    # Array fields become a side table keyed on doc_id.
    assert 'CREATE TABLE "invoice_line_items"' in ddl
    assert '"quantity" NUMERIC' in ddl
    # doc_id present on both.
    assert ddl.count('"doc_id" TEXT') == 2


def test_sql_bigquery_uses_bool_string_and_options_descriptions():
    ddl = codegen.to_sql_ddl(SAMPLE_DEFINITION, "bigquery")
    assert "CREATE TABLE `invoice`" in ddl
    assert "`paid` BOOL" in ddl
    assert "`doc_id` STRING" in ddl
    # BigQuery descriptions land in OPTIONS(description=…); single quotes
    # inside the description get doubled.
    assert "OPTIONS(description='Unique identifier.')" in ddl
    assert "OPTIONS(description='Total due.')" in ddl


def test_sql_dialect_rejects_unknown_value():
    with pytest.raises(ValueError):
        codegen.to_sql_ddl(SAMPLE_DEFINITION, "oracle")  # type: ignore[arg-type]


def test_typescript_emits_interface_with_unions_and_arrays():
    ts = codegen.to_typescript(SAMPLE_DEFINITION)
    assert ts.startswith("/** Sample invoice schema. */\n")
    assert "export interface Invoice {" in ts
    assert 'invoice_number?: string | null;' in ts
    assert 'total_amount?: number | null;' in ts
    assert 'currency?: "USD" | "EUR" | null;' in ts
    assert 'paid?: boolean | null;' in ts
    # Nested array of struct: Array<{ ... }>
    assert "line_items?: Array<{" in ts
    assert "quantity?: number | null;" in ts


def test_typescript_pascal_cases_document_type():
    ts = codegen.to_typescript({"document": {"document_type": "purchase order"}})
    assert "export interface PurchaseOrder " in ts


def test_typescript_quotes_non_identifier_keys():
    defn = {"document": {"document_type": "X", "fields": [{"name": "1st-line"}]}}
    ts = codegen.to_typescript(defn)
    assert '"1st-line"?:' in ts


def test_render_dispatches_to_format_and_returns_metadata():
    body, media, filename = codegen.render(SAMPLE_DEFINITION, "json-schema")
    parsed = json.loads(body)
    assert parsed["title"] == "Invoice"
    assert media == "application/json"
    assert filename == "invoice.schema.json"

    body, media, filename = codegen.render(SAMPLE_DEFINITION, "sql-postgres")
    assert b'CREATE TABLE "invoice"' in body
    assert filename == "invoice.postgres.sql"
    assert media.startswith("text/plain")

    body, media, filename = codegen.render(SAMPLE_DEFINITION, "sql-bigquery")
    assert b"CREATE TABLE `invoice`" in body
    assert filename == "invoice.bigquery.sql"

    body, media, filename = codegen.render(SAMPLE_DEFINITION, "typescript")
    assert b"export interface Invoice " in body
    assert filename == "invoice.ts"


def test_render_rejects_unknown_format():
    with pytest.raises(ValueError):
        codegen.render(SAMPLE_DEFINITION, "yaml")


# ---------- HTTP endpoint ----------


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    main._invalidate_definitions_cache()
    c = TestClient(main.app)
    c.post("/api/definitions", json=SAMPLE_DEFINITION)
    return c


def test_codegen_endpoint_returns_json_schema(client):
    resp = client.get("/api/definitions/invoice/codegen?format=json-schema")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    cd = resp.headers["content-disposition"]
    assert 'filename="invoice.schema.json"' in cd
    body = resp.json()
    assert body["title"] == "Invoice"
    assert body["properties"]["invoice_date"]["format"] == "date"


def test_codegen_endpoint_returns_postgres_ddl(client):
    resp = client.get("/api/definitions/invoice/codegen?format=sql-postgres")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'filename="invoice.postgres.sql"' in resp.headers["content-disposition"]
    assert 'CREATE TABLE "invoice"' in resp.text
    assert 'CREATE TABLE "invoice_line_items"' in resp.text


def test_codegen_endpoint_returns_bigquery_ddl(client):
    resp = client.get("/api/definitions/invoice/codegen?format=sql-bigquery")
    assert resp.status_code == 200
    assert "CREATE TABLE `invoice`" in resp.text
    assert 'filename="invoice.bigquery.sql"' in resp.headers["content-disposition"]


def test_codegen_endpoint_returns_typescript(client):
    resp = client.get("/api/definitions/invoice/codegen?format=typescript")
    assert resp.status_code == 200
    assert "export interface Invoice " in resp.text
    assert 'filename="invoice.ts"' in resp.headers["content-disposition"]


def test_codegen_endpoint_404_for_missing_definition(client):
    resp = client.get("/api/definitions/no_such_def/codegen?format=typescript")
    assert resp.status_code == 404


def test_codegen_endpoint_400_for_unknown_format(client):
    resp = client.get("/api/definitions/invoice/codegen?format=yaml")
    assert resp.status_code == 400
    assert "format must be one of" in resp.json()["detail"]


def test_codegen_endpoint_422_when_format_missing(client):
    # FastAPI rejects the missing required query param before we even reach
    # the handler — the validator surfaces a 422 with a query.format hint.
    resp = client.get("/api/definitions/invoice/codegen")
    assert resp.status_code == 422


def test_codegen_endpoint_validates_def_id_shape(client):
    # Same shape guard as the versions endpoint — `..` / dashes etc never
    # touch the filesystem.
    resp = client.get("/api/definitions/..%2Fpasswd/codegen?format=typescript")
    assert resp.status_code == 404
