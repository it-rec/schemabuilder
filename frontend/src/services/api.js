const API_BASE = process.env.REACT_APP_API_URL || "http://localhost:8000";

export async function fetchDocuments() {
  const res = await fetch(`${API_BASE}/api/documents`);
  if (!res.ok) throw new Error("Failed to fetch documents");
  return res.json();
}

export async function fetchDocument(docId, signal) {
  const res = await fetch(`${API_BASE}/api/documents/${docId}`, { signal });
  if (!res.ok) throw new Error("Failed to fetch document");
  return res.json();
}

export function getPageImageUrl(docId, pageNo) {
  return `${API_BASE}/api/documents/${docId}/pages/${pageNo}`;
}
