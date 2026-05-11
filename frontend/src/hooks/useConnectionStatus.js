import { useCallback, useEffect, useRef, useState } from "react";
import { checkHealth } from "../services/api";

// While offline we probe aggressively so the UI un-pauses quickly once the
// backend recovers. Once online, we drop to a lazy heartbeat that catches a
// drop within ~30s without hammering the server.
const OFFLINE_POLL_MS = 3000;
const ONLINE_POLL_MS = 30000;

// Tracks reachability of the backend's /health endpoint. Returns:
//   online    – null while the first probe is in flight, then true / false.
//   retrying  – true while a probe is in flight; drives the spinner in the
//               offline overlay so the user sees we're actively reconnecting.
//   reloadKey – monotonically increments on every transition into "online".
//               Consumers put it in a useEffect dep list to force data loads
//               to re-run once the connection comes back.
//   retry     – fire an immediate probe (wired to the "Retry now" button).
export function useConnectionStatus() {
  const [online, setOnline] = useState(null);
  const [retrying, setRetrying] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  // Read inside the polling loop without re-creating the timer on every flip;
  // the effect that owns the loop runs once for the lifetime of the hook.
  const onlineRef = useRef(null);

  const ping = useCallback(async () => {
    setRetrying(true);
    try {
      await checkHealth();
      if (onlineRef.current !== true) {
        onlineRef.current = true;
        setOnline(true);
        // Bump on null→true (first probe) and on false→true (reconnect) so
        // callers can re-fetch in both cases.
        setReloadKey((k) => k + 1);
      }
    } catch (_err) {
      onlineRef.current = false;
      setOnline(false);
    } finally {
      setRetrying(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    async function loop() {
      if (cancelled) return;
      await ping();
      if (cancelled) return;
      const next = onlineRef.current ? ONLINE_POLL_MS : OFFLINE_POLL_MS;
      timer = setTimeout(loop, next);
    }
    loop();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [ping]);

  // Browser-level online/offline events are best-effort hints — they fire on
  // OS network changes but not on backend-only outages. Use them to react
  // instantly when we have the signal; the polling loop covers the rest.
  useEffect(() => {
    function onBrowserOnline() {
      if (onlineRef.current !== true) ping();
    }
    function onBrowserOffline() {
      onlineRef.current = false;
      setOnline(false);
    }
    window.addEventListener("online", onBrowserOnline);
    window.addEventListener("offline", onBrowserOffline);
    return () => {
      window.removeEventListener("online", onBrowserOnline);
      window.removeEventListener("offline", onBrowserOffline);
    };
  }, [ping]);

  return { online, retrying, reloadKey, retry: ping };
}
