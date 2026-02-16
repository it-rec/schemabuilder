const API_BASE = process.env.REACT_APP_API_URL || "http://localhost:8000";

export async function fetchDocuments() {
  const res = await fetch(`${API_BASE}/api/documents`);
  if (!res.ok) throw new Error("Failed to fetch documents");
  return res.json();
}

export async function fetchDocument(docId) {
  const res = await fetch(`${API_BASE}/api/documents/${docId}`);
  if (!res.ok) throw new Error("Failed to fetch document");
  return res.json();
}

export function getPageImageUrl(docId, pageNo) {
  return `${API_BASE}/api/documents/${docId}/pages/${pageNo}`;
}

export async function fetchDefinitions() {
  const res = await fetch(`${API_BASE}/api/definitions`);
  if (!res.ok) throw new Error("Failed to fetch definitions");
  return res.json();
}

export async function fetchDefinition(defId) {
  const res = await fetch(`${API_BASE}/api/definitions/${defId}`);
  if (!res.ok) throw new Error("Failed to fetch definition");
  return res.json();
}

export async function extractFields(docId, definitionId) {
  const res = await fetch(`${API_BASE}/api/documents/${docId}/extract`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ definition_id: definitionId }),
  });
  if (!res.ok) throw new Error("Failed to extract fields");
  return res.json();
}

export async function uploadDefinition(definition) {
  const res = await fetch(`${API_BASE}/api/definitions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(definition),
  });
  if (!res.ok) throw new Error("Failed to upload definition");
  return res.json();
}
