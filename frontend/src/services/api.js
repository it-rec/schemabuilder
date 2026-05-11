const API_BASE = process.env.REACT_APP_API_URL || "http://localhost:8000";

// Default per-request timeout. Beyond this we abort and let the caller retry
// or surface the failure — better than letting a stuck server pin the UI.
const DEFAULT_TIMEOUT_MS = Number(process.env.REACT_APP_API_TIMEOUT_MS) || 30_000;

// Retry budget for idempotent GETs. Mutations (POST/PATCH/DELETE) don't retry
// because we can't tell from the client whether a 5xx happened before or
// after the server-side write.
const DEFAULT_RETRIES = 2;
const RETRY_BASE_MS = 200;

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

// Combine a caller-provided AbortSignal with our internal timeout signal so
// either source can cancel the in-flight fetch. Returns an AbortSignal plus a
// cleanup that detaches the timer / listener so we don't leak handles.
function withTimeoutSignal(externalSignal, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new DOMException("timeout", "AbortError")), timeoutMs);
  let detach = () => {};
  if (externalSignal) {
    if (externalSignal.aborted) {
      ctrl.abort(externalSignal.reason);
    } else {
      const onAbort = () => ctrl.abort(externalSignal.reason);
      externalSignal.addEventListener("abort", onAbort, { once: true });
      detach = () => externalSignal.removeEventListener("abort", onAbort);
    }
  }
  return {
    signal: ctrl.signal,
    cleanup: () => {
      clearTimeout(timer);
      detach();
    },
  };
}

// Core fetch wrapper. Adds:
//   - per-request timeout via AbortController
//   - retry-with-backoff for transient failures on idempotent requests
//   - parsed FastAPI error messages on 4xx/5xx
// Returns the raw Response so callers can call .json() / .blob() / etc.
async function request(path, {
  method = "GET",
  body,
  headers,
  signal,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  retries,
  errorFallback = `Request to ${path} failed`,
} = {}) {
  const isIdempotent = method === "GET" || method === "HEAD";
  // A caller can opt non-idempotent methods into retries by passing `retries`
  // explicitly — e.g. /extract POSTs that want to absorb a 503 from the
  // server-side concurrency limiter. Without an explicit value, only
  // idempotent verbs retry by default.
  const retriesExplicit = retries !== undefined;
  const canRetry = isIdempotent || retriesExplicit;
  const attempts = (retries ?? (isIdempotent ? DEFAULT_RETRIES : 0)) + 1;

  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const { signal: combinedSignal, cleanup } = withTimeoutSignal(signal, timeoutMs);
    let res;
    try {
      res = await fetch(`${API_BASE}${path}`, {
        method,
        headers,
        body,
        signal: combinedSignal,
      });
    } catch (err) {
      cleanup();
      // Propagate caller-initiated aborts immediately. A timeout is also an
      // AbortError but we surface it as a retryable network error below.
      if (signal && signal.aborted) throw err;
      lastError = err;
      // Retry network errors (incl. our own timeout) on idempotent requests,
      // or when the caller explicitly opted in via `retries`.
      if (canRetry && attempt < attempts - 1) {
        await new Promise((r) => setTimeout(r, RETRY_BASE_MS * 2 ** attempt));
        continue;
      }
      throw err;
    }
    cleanup();

    // Retry transient server errors (5xx) and 503 specifically. 429 is also
    // worth a retry but with the Retry-After header honored if present.
    if (res.status >= 500 || res.status === 429) {
      if (canRetry && attempt < attempts - 1) {
        const ra = Number(res.headers.get("retry-after"));
        const wait = Number.isFinite(ra) && ra > 0
          ? ra * 1000
          : RETRY_BASE_MS * 2 ** attempt;
        await new Promise((r) => setTimeout(r, wait));
        continue;
      }
    }

    if (!res.ok) {
      throw new Error(await readError(res, errorFallback));
    }
    return res;
  }
  throw lastError || new Error(errorFallback);
}

export async function fetchDocuments({ signal } = {}) {
  const res = await request("/api/documents", {
    signal,
    errorFallback: "Failed to fetch documents",
  });
  return unwrapList(await res.json());
}

export async function fetchDocument(docId, { signal } = {}) {
  const res = await request(`/api/documents/${docId}`, {
    signal,
    errorFallback: "Failed to fetch document",
  });
  return res.json();
}

export function getPageImageUrl(docId, pageNo) {
  return `${API_BASE}/api/documents/${docId}/pages/${pageNo}`;
}

export async function fetchDefinitions({ signal } = {}) {
  const res = await request("/api/definitions", {
    signal,
    errorFallback: "Failed to fetch definitions",
  });
  return unwrapList(await res.json());
}

export async function fetchDefinition(defId, { signal } = {}) {
  const res = await request(`/api/definitions/${defId}`, {
    signal,
    errorFallback: "Failed to fetch definition",
  });
  return res.json();
}

export async function extractFields(docId, definitionId, { signal } = {}) {
  // Extraction is a long-running POST. Allow more time than the default GET
  // budget (Docling can take 10-30s on a cold cache for large PDFs) and
  // tolerate a 503 from the concurrency limiter by retrying once after the
  // server's Retry-After.
  const res = await request(`/api/documents/${docId}/extract`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ definition_id: definitionId }),
    signal,
    timeoutMs: 120_000,
    retries: 1,
    errorFallback: "Failed to extract fields",
  });
  return res.json();
}

export async function uploadDefinition(definition, { overwrite = false, signal } = {}) {
  const qs = overwrite ? "?overwrite=true" : "";
  const res = await request(`/api/definitions${qs}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(definition),
    signal,
    errorFallback: "Failed to upload definition",
  });
  return res.json();
}

export async function updateDefinition(defId, definition, { signal } = {}) {
  const res = await request(`/api/definitions/${defId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(definition),
    signal,
    errorFallback: "Failed to update definition",
  });
  return res.json();
}

export async function deleteDefinition(defId, { signal } = {}) {
  const res = await request(`/api/definitions/${defId}`, {
    method: "DELETE",
    signal,
    errorFallback: "Failed to delete definition",
  });
  return res.json();
}

