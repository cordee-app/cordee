// "May be stuck" detection for running executions.
//
// Token counts are only written when a run finishes (Vibe and batch paths
// write everything at the end), so "N min with 0 output tokens" flags every
// healthy long run. The executor heartbeat (touched every 30s while the worker
// is alive) is the real liveness signal; tokens/elapsed is only a fallback for
// rows that have no heartbeat.

const HEARTBEAT_STALE_MINS = 2;
const NO_HEARTBEAT_STUCK_MINS = 15;

const minsSince = (iso?: string | null): number | null => {
  if (!iso) return null;
  const t = new Date(iso.replace(' ', 'T') + (iso.endsWith('Z') ? '' : 'Z')).getTime();
  if (isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 60000);
};

interface StuckInput {
  status?: string;
  started_at?: string;
  last_heartbeat_at?: string | null;
  tokens_output?: number;
}

/** Returns a tooltip reason when the execution looks stuck, else null. */
export function stuckReason(e: StuckInput | null | undefined): string | null {
  if (!e || e.status !== 'running') return null;
  const hb = minsSince(e.last_heartbeat_at);
  if (hb !== null) {
    return hb > HEARTBEAT_STALE_MINS
      ? `No worker heartbeat for ${hb} min. Consider cancelling and re-running.`
      : null;
  }
  const elapsed = minsSince(e.started_at) ?? 0;
  return elapsed > NO_HEARTBEAT_STUCK_MINS && (e.tokens_output || 0) === 0
    ? `Running for ${elapsed} min with 0 output tokens and no heartbeat. Consider cancelling and re-running.`
    : null;
}
