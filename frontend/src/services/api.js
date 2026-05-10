const API_BASE = process.env.REACT_APP_API_URL || "http://localhost:8000";

// Backend list endpoints return `{items, total, limit, offset}` so paginated
// scrolling and "N of M" displays are possible without a second round-trip.
// Callers historically expected a bare array, so unwrap here and let the rest
// of the app keep working with `docs.map(...)` / `defs.find(...)`.
function unwrapList(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.items)) return payload.items;
  return [];
}

async function readError(res, fallback) {
  // FastAPI's HTTPException body is `{detail: "..."}` — surface it so a
  // 409/404/422 ends up as an actionable message rather than "Failed to ...".
  try {
    const body = await res.json();
    if (body && typeof body.detail === "string") return body.detail;
  } catch (_) {
    /* not JSON */
  }
  return fallback;
}

export async function fetchDocuments() {
  const res = await fetch(`${API_BASE}/api/documents`);
  if (!res.ok) throw new Error(await readError(res, "Failed to fetch documents"));
  return unwrapList(await res.json());
}

export async function fetchDocument(docId) {
  const res = await fetch(`${API_BASE}/api/documents/${docId}`);
  if (!res.ok) throw new Error(await readError(res, "Failed to fetch document"));
  return res.json();
}

export function getPageImageUrl(docId, pageNo) {
  return `${API_BASE}/api/documents/${docId}/pages/${pageNo}`;
}

export async function fetchDefinitions() {
  const res = await fetch(`${API_BASE}/api/definitions`);
  if (!res.ok) throw new Error(await readError(res, "Failed to fetch definitions"));
  return unwrapList(await res.json());
}

export async function fetchDefinition(defId) {
  const res = await fetch(`${API_BASE}/api/definitions/${defId}`);
  if (!res.ok) throw new Error(await readError(res, "Failed to fetch definition"));
  return res.json();
}

export async function extractFields(docId, definitionId) {
  const res = await fetch(`${API_BASE}/api/documents/${docId}/extract`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ definition_id: definitionId }),
  });
  if (!res.ok) throw new Error(await readError(res, "Failed to extract fields"));
  return res.json();
}

export async function uploadDefinition(definition, { overwrite = false } = {}) {
  const qs = overwrite ? "?overwrite=true" : "";
  const res = await fetch(`${API_BASE}/api/definitions${qs}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(definition),
  });
  if (!res.ok) throw new Error(await readError(res, "Failed to upload definition"));
  return res.json();
}

export async function updateDefinition(defId, definition) {
  const res = await fetch(`${API_BASE}/api/definitions/${defId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(definition),
  });
  if (!res.ok) throw new Error(await readError(res, "Failed to update definition"));
  return res.json();
}

export async function deleteDefinition(defId) {
  const res = await fetch(`${API_BASE}/api/definitions/${defId}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await readError(res, "Failed to delete definition"));
  return res.json();
}
