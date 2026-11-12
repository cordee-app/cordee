import { useEffect } from 'react';
import { useStore } from '../store';

/**
 * Subscribes to SSE events for a project and dispatches them to the store.
 * Native EventSource auto-reconnects on error - no manual reconnection logic needed.
 * The 30s loadAll poll remains as a safety net for missed events.
 */
export function useProjectSSE(projectId: number | null) {
  const handleSSEEvent = useStore((s) => s.handleSSEEvent);
  const termsRequired = useStore((s) => s.termsRequired);

  useEffect(() => {
    if (projectId === null) return;
    // A gated SSE endpoint would 403 and EventSource would reconnect in a
    // loop; stay closed until the T&C are accepted (flag cleared by loadAll).
    if (termsRequired) return;

    const es = new EventSource(`/api/projects/${projectId}/events`);

    es.onmessage = (event) => {
      // Skip heartbeat comments (lines starting with ':')
      if (event.data.startsWith(':')) return;
      try {
        const evt = JSON.parse(event.data);
        handleSSEEvent(evt);
      } catch {
        // Ignore malformed events - poll will catch up
      }
    };

    // EventSource auto-reconnects on error by default
    // No onerror handler needed - just let it reconnect silently

    return () => {
      es.close();
    };
  }, [projectId, handleSSEEvent, termsRequired]);
}
