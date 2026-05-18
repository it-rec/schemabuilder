import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  ComposedModal,
  InlineNotification,
  ModalBody,
  ModalFooter,
  ModalHeader,
  StructuredListBody,
  StructuredListCell,
  StructuredListHead,
  StructuredListRow,
  StructuredListWrapper,
  Tag,
} from "@carbon/react";
import {
  fetchDefinition,
  fetchDefinitionVersion,
  fetchDefinitionVersions,
  updateDefinition,
} from "../services/api";

// Tiny LCS-based line diff. Returns rows of {kind, leftLine, rightLine}
// where kind is "same" | "added" | "removed". Suitable for tiny JSON-doc
// diffs (a few hundred lines max); a real algorithm pays off only at
// kilobyte scale and isn't worth the dep.
function diffLines(leftText, rightText) {
  const a = leftText.split("\n");
  const b = rightText.split("\n");
  const n = a.length;
  const m = b.length;
  // Build the LCS length table.
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] =
        a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ kind: "same", leftLine: a[i], rightLine: b[j] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ kind: "removed", leftLine: a[i], rightLine: null });
      i++;
    } else {
      rows.push({ kind: "added", leftLine: null, rightLine: b[j] });
      j++;
    }
  }
  while (i < n) rows.push({ kind: "removed", leftLine: a[i++], rightLine: null });
  while (j < m) rows.push({ kind: "added", leftLine: null, rightLine: b[j++] });
  return rows;
}

function formatTimestamp(ms) {
  try {
    return new Date(ms).toLocaleString();
  } catch (_) {
    return String(ms);
  }
}

export default function DefinitionHistory({
  open,
  definitionId,
  onClose,
  onRestored, // (definitionId) => void — caller refetches + closes
}) {
  const [versions, setVersions] = useState([]);
  // Start in the loading state — the parent only mounts this modal when
  // there's a definitionId to fetch for, so we'll always kick off a
  // request on first render and the spinner should be visible until it
  // resolves. Avoids a synchronous setLoading(true) inside the effect.
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [archived, setArchived] = useState(null);
  const [current, setCurrent] = useState(null);
  const [restoring, setRestoring] = useState(false);

  // Fetch on mount. The parent unmounts this modal when `historyOpen` flips
  // back to false (and a `key={definitionId}` forces a clean remount when
  // the user switches definitions while it's open), so we don't need an
  // open-prop guard or in-body state resets — every mount starts from the
  // useState defaults above.
  useEffect(() => {
    if (!definitionId) return undefined;
    const ctrl = new AbortController();
    Promise.all([
      fetchDefinitionVersions(definitionId, { signal: ctrl.signal }),
      fetchDefinition(definitionId, { signal: ctrl.signal }),
    ])
      .then(([vs, cur]) => {
        if (ctrl.signal.aborted) return;
        setVersions(vs?.items || []);
        setCurrent(cur || null);
      })
      .catch((err) => {
        if (err?.name === "AbortError") return;
        setError(err.message || "Failed to load history");
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [definitionId]);

  const handleSelect = useCallback(
    async (id) => {
      setSelectedId(id);
      setArchived(null);
      try {
        const data = await fetchDefinitionVersion(definitionId, id);
        setArchived(data);
      } catch (err) {
        setError(err.message || "Failed to load version");
      }
    },
    [definitionId],
  );

  const handleRestore = useCallback(async () => {
    if (!archived || !definitionId) return;
    if (
      // eslint-disable-next-line no-alert
      !window.confirm(
        "Restore this version? The current definition will be archived as a new version.",
      )
    ) {
      return;
    }
    setRestoring(true);
    setError(null);
    try {
      await updateDefinition(definitionId, archived);
      onRestored?.(definitionId);
    } catch (err) {
      setError(err.message || "Restore failed");
    } finally {
      setRestoring(false);
    }
  }, [archived, definitionId, onRestored]);

  // Stringify both for diff rendering. Stable key order so a definition
  // that round-tripped through Python (which inserts keys arbitrarily)
  // doesn't show noisy diff churn. JSON.stringify's replacer-as-array
  // form only orders top-level keys and drops nested keys it doesn't
  // know about — so we recurse manually.
  const diffRows = useMemo(() => {
    if (!archived || !current) return null;
    const sorted = (v) => {
      if (Array.isArray(v)) return v.map(sorted);
      if (v && typeof v === "object") {
        return Object.keys(v)
          .sort()
          .reduce((acc, k) => {
            acc[k] = sorted(v[k]);
            return acc;
          }, {});
      }
      return v;
    };
    const leftText = JSON.stringify(sorted(archived), null, 2);
    const rightText = JSON.stringify(sorted(current), null, 2);
    return diffLines(leftText, rightText);
  }, [archived, current]);

  return (
    <ComposedModal
      open={open}
      onClose={() => (restoring ? false : onClose?.())}
      size="lg"
      aria-label="Definition history"
    >
      <ModalHeader title="Definition history" />
      <ModalBody hasScrollingContent>
        {error && (
          <InlineNotification
            kind="error"
            title="Error"
            subtitle={error}
            onCloseButtonClick={() => setError(null)}
            lowContrast
          />
        )}
        {loading ? (
          <p className="definition-history__empty" role="status">
            Loading…
          </p>
        ) : versions.length === 0 ? (
          <p className="definition-history__empty">
            No archived versions. Every edit, overwrite, or delete on this
            definition is archived; the list starts populating after the
            first such mutation.
          </p>
        ) : (
          <div className="definition-history__layout">
            <StructuredListWrapper
              className="definition-history__list"
              aria-label="Version list"
            >
              <StructuredListHead>
                <StructuredListRow head>
                  <StructuredListCell head>When</StructuredListCell>
                  <StructuredListCell head>Action</StructuredListCell>
                </StructuredListRow>
              </StructuredListHead>
              <StructuredListBody>
                {versions.map((v) => (
                  <StructuredListRow
                    key={v.id}
                    onClick={() => handleSelect(v.id)}
                    role="button"
                    tabIndex={0}
                    aria-pressed={selectedId === v.id}
                    className={
                      "definition-history__row" +
                      (selectedId === v.id ? " definition-history__row--selected" : "")
                    }
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        handleSelect(v.id);
                      }
                    }}
                    data-testid={`def-version-${v.id}`}
                  >
                    <StructuredListCell>
                      {formatTimestamp(v.timestamp_ms)}
                    </StructuredListCell>
                    <StructuredListCell>
                      <Tag size="sm" type="cool-gray">
                        {v.action}
                      </Tag>
                    </StructuredListCell>
                  </StructuredListRow>
                ))}
              </StructuredListBody>
            </StructuredListWrapper>

            <div className="definition-history__diff">
              {!archived && (
                <p className="definition-history__empty">
                  Select a version to view the diff against the current
                  definition.
                </p>
              )}
              {archived && diffRows && (
                <pre
                  className="definition-history__diff-pre"
                  data-testid="def-history-diff"
                >
                  {diffRows.map((row, idx) => {
                    if (row.kind === "same") {
                      return (
                        <span key={idx} className="definition-history__diff-same">
                          {`  ${row.leftLine}`}
                          {"\n"}
                        </span>
                      );
                    }
                    if (row.kind === "removed") {
                      return (
                        <span key={idx} className="definition-history__diff-removed">
                          {`- ${row.leftLine}`}
                          {"\n"}
                        </span>
                      );
                    }
                    return (
                      <span key={idx} className="definition-history__diff-added">
                        {`+ ${row.rightLine}`}
                        {"\n"}
                      </span>
                    );
                  })}
                </pre>
              )}
            </div>
          </div>
        )}
      </ModalBody>
      <ModalFooter>
        <Button kind="secondary" onClick={onClose} disabled={restoring}>
          Close
        </Button>
        <Button
          kind="primary"
          onClick={handleRestore}
          disabled={!archived || restoring}
          data-testid="def-restore-button"
        >
          {restoring ? "Restoring…" : "Restore this version"}
        </Button>
      </ModalFooter>
    </ComposedModal>
  );
}
