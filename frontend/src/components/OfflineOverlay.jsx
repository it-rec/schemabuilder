import React from "react";
import { InlineLoading } from "@carbon/react";
import { WifiOff } from "@carbon/react/icons";

// Full-viewport blocker that takes over the UI whenever the backend is
// unreachable. Two visual states keyed off `online`:
//   null  → first probe still in flight → minimal "Connecting…" message.
//   false → confirmed offline → headline + auto-reconnect indicator.
// The polling loop in useConnectionStatus keeps retrying in the background
// so the overlay disappears the moment the backend comes back. A single,
// steady InlineLoading conveys "we're working on it" without flipping
// between in-flight / waiting states every few seconds.
export default function OfflineOverlay({ online }) {
  const isChecking = online === null;
  return (
    <div
      className="offline-overlay"
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="offline-overlay-title"
      aria-describedby="offline-overlay-description"
      data-testid="offline-overlay"
    >
      <div className="offline-overlay__card">
        <div className="offline-overlay__icon" aria-hidden="true">
          <WifiOff size={32} />
        </div>
        <h2 id="offline-overlay-title" className="offline-overlay__title">
          {isChecking ? "Connecting…" : "You're offline"}
        </h2>
        <p id="offline-overlay-description" className="offline-overlay__body">
          {isChecking
            ? "Checking the connection to the Schema Builder backend."
            : "Schema Builder can't reach its backend. The interface is paused and will resume automatically as soon as the connection is restored."}
        </p>
        <div className="offline-overlay__status">
          <InlineLoading
            description={
              isChecking ? "Connecting…" : "Reconnecting automatically…"
            }
            status="active"
            data-testid="offline-overlay-loading"
          />
        </div>
      </div>
    </div>
  );
}
