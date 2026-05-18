import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  Button,
  ComposedModal,
  InlineNotification,
  ModalBody,
  ModalFooter,
  ModalHeader,
  ProgressBar,
} from "@carbon/react";
import { cancelBatch, getBatchStatus, startBatchExtract } from "../services/api";

// Poll interval for batch status. 500 ms feels live without thrashing the
// API; the worker emits one update per document so faster polling wouldn't
// surface anything new on a typical doc.
const POLL_INTERVAL_MS = 500;

export default function BatchExtractModal({
  open,
  documents, // [{id, filename}, ...] — every doc we want to enqueue
  definitionId,
  definitionLabel,
  onClose,
}) {
  const [status, setStatus] = useState("idle"); // idle | running | done | cancelled | failed
  const [jobId, setJobId] = useState(null);
  const [progress, setProgress] = useState({ completed: 0, total: documents.length });
  const [errors, setErrors] = useState({});
  const [results, setResults] = useState({});
  const [startError, setStartError] = useState(null);
  const pollTimerRef = useRef(null);

  // Reset state whenever the modal opens with a new run. The closed-state
  // branch in start() also clears, but resetting on open avoids briefly
  // flashing the previous run's progress on re-open.
  useEffect(() => {
    if (!open) return;
    setStatus("idle");
    setJobId(null);
    setProgress({ completed: 0, total: documents.length });
    setErrors({});
    setResults({});
    setStartError(null);
  }, [open, documents.length]);

  // Clear any in-flight poll timer on unmount / close.
  useEffect(() => {
    return () => {
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, []);

  // Recursive setTimeout chain — the rescheduled tick has to dispatch the
  // next poll. Reaching back through the `pollOnce` binding inside its own
  // useCallback would TDZ-capture the const while it's still being
  // initialized (works in practice because the timer fires later, but the
  // linter rightly flags it as an unstable self-reference). Stash the
  // latest callable on a ref so the timer indirectly resolves to whatever
  // `pollOnce` is at the moment the timer fires.
  const pollOnceRef = useRef(null);
  const pollOnce = useCallback(async (id) => {
    try {
      const s = await getBatchStatus(id);
      setProgress({ completed: s.completed, total: s.total });
      setErrors(s.errors || {});
      setResults(s.results || {});
      setStatus(s.status);
      if (s.status === "running") {
        pollTimerRef.current = setTimeout(
          () => pollOnceRef.current?.(id),
          POLL_INTERVAL_MS,
        );
      }
    } catch (err) {
      console.error(err);
      setStatus("failed");
    }
  }, []);
  useEffect(() => {
    pollOnceRef.current = pollOnce;
  }, [pollOnce]);

  const handleStart = useCallback(async () => {
    if (!documents.length || !definitionId) return;
    setStatus("running");
    setStartError(null);
    try {
      const { job_id } = await startBatchExtract(
        documents.map((d) => d.id),
        definitionId,
      );
      setJobId(job_id);
      // Kick the first poll immediately so the bar moves off zero ASAP
      // rather than waiting POLL_INTERVAL_MS.
      pollOnce(job_id);
    } catch (err) {
      setStartError(err.message || "Failed to start batch.");
      setStatus("idle");
    }
  }, [documents, definitionId, pollOnce]);

  const handleCancel = useCallback(async () => {
    if (!jobId) return;
    // Stop polling immediately so the UI doesn't keep firing /status
    // requests against a worker that is winding down. The server-side
    // cancel is async (the worker checks the flag between docs), so
    // the user might wait minutes for the actual `cancelled` status
    // otherwise; flipping local state right away avoids that confusion.
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setStatus("cancelled");
    try {
      await cancelBatch(jobId);
    } catch (err) {
      console.error(err);
    }
  }, [jobId]);

  const handleDownload = useCallback(() => {
    const payload = {
      job_id: jobId,
      definition_id: definitionId,
      results,
      errors,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `batch-${jobId}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }, [jobId, definitionId, results, errors]);

  const isRunning = status === "running";
  const isTerminal = status === "done" || status === "cancelled" || status === "failed";
  const errorCount = Object.keys(errors).length;
  const successCount = Object.keys(results).length;
  const fraction = progress.total > 0 ? progress.completed / progress.total : 0;

  return (
    <ComposedModal
      open={open}
      onClose={() => (isRunning ? false : onClose?.())}
      size="sm"
      aria-label="Batch extraction"
    >
      <ModalHeader title="Batch extraction" />
      <ModalBody>
        <p className="batch-modal__intro">
          Run <strong>{definitionLabel}</strong> over{" "}
          <strong>{documents.length}</strong> document
          {documents.length === 1 ? "" : "s"}.
        </p>

        {startError && (
          <InlineNotification
            kind="error"
            title="Couldn't start"
            subtitle={startError}
            lowContrast
            onCloseButtonClick={() => setStartError(null)}
          />
        )}

        {status !== "idle" && (
          <>
            <ProgressBar
              label={`${progress.completed} / ${progress.total} done`}
              helperText={
                status === "cancelled"
                  ? "Cancelled"
                  : status === "done"
                    ? `${successCount} succeeded, ${errorCount} errored`
                    : status === "failed"
                      ? "Polling failed — see console"
                      : "Working…"
              }
              max={progress.total || 1}
              value={progress.completed}
              status={
                status === "failed"
                  ? "error"
                  : status === "done"
                    ? "finished"
                    : status === "cancelled"
                      ? "error"
                      : "active"
              }
              data-testid="batch-progress"
            />
            {errorCount > 0 && isTerminal && (
              <div
                className="batch-modal__errors"
                data-testid="batch-errors"
                role="note"
              >
                <strong>{errorCount} failed:</strong>
                <ul>
                  {Object.entries(errors)
                    .slice(0, 5)
                    .map(([docId, msg]) => (
                      <li key={docId}>
                        <code>{docId.slice(0, 12)}…</code>: {msg}
                      </li>
                    ))}
                  {errorCount > 5 && <li>(+{errorCount - 5} more, see JSON)</li>}
                </ul>
              </div>
            )}
          </>
        )}
        {/* fraction is read by tests via the aria-valuenow on the bar */}
        <span className="cds--visually-hidden">{Math.round(fraction * 100)}%</span>
      </ModalBody>
      <ModalFooter>
        {status === "idle" && (
          <>
            <Button kind="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button
              kind="primary"
              onClick={handleStart}
              disabled={!documents.length}
              data-testid="batch-start"
            >
              Start
            </Button>
          </>
        )}
        {isRunning && (
          <Button kind="danger" onClick={handleCancel} data-testid="batch-cancel">
            Cancel run
          </Button>
        )}
        {isTerminal && (
          <>
            <Button
              kind="secondary"
              onClick={handleDownload}
              disabled={successCount === 0 && errorCount === 0}
              data-testid="batch-download"
            >
              Download JSON
            </Button>
            <Button kind="primary" onClick={onClose}>
              Close
            </Button>
          </>
        )}
      </ModalFooter>
    </ComposedModal>
  );
}
