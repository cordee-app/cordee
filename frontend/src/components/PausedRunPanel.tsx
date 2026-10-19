import { useEffect, useState } from 'react';
import { api } from '../api';
import { useStore } from '../store';
import type { Execution } from '../types';

// A run paused at its turn/time limit (TODO item 16): the report Cordée wrote
// (facts from code + the Guide's read) and the user's three choices. Shown on
// the task card; the execution log has the same buttons.

interface PauseFacts {
  limit?: string;
  part?: number;
  turns_this_part?: number;
  minutes_this_part?: number;
  continues_left?: number;
  cost_usd_so_far?: number;
  next_block_cost_usd_estimate?: number;
  deliverables_written?: string[];
  deliverables_missing?: string[];
  files_changed_count?: number;
  repeated_actions?: { action: string; times: number }[];
  model_status?: string;
}

interface PauseGuide {
  progress?: string;
  recommendation?: string;
  suggested_note?: string;
  summary?: string;
}

const parseReport = (raw?: string | null): { facts: PauseFacts; guide: PauseGuide | null } => {
  try {
    const r = raw ? JSON.parse(raw) : {};
    return { facts: r.facts || {}, guide: r.guide || null };
  } catch {
    return { facts: {}, guide: null };
  }
};

const PROGRESS_STYLE: Record<string, { label: string; color: string }> = {
  progressing: { label: 'progressing', color: '#56633f' },
  looping: { label: 'looping', color: '#a3402f' },
  blocked: { label: 'blocked', color: '#a3402f' },
};

const REC_LABEL: Record<string, string> = {
  continue: 'continue',
  continue_with_note: 'continue with a note',
  stop: 'stop',
};

export const PausedRunPanel = ({ exec, canEdit }: { exec: Execution; canEdit: boolean }) => {
  const { facts, guide } = parseReport(exec.pause_report_json);
  const suggested = guide?.suggested_note || '';
  const [note, setNote] = useState<string>(suggested);
  const [busy, setBusy] = useState(false);
  const [showStatus, setShowStatus] = useState(false);
  const setExecutions = useStore((s) => s.setExecutions);
  const guideChatId = useStore((s) => s.projects.find((p) => p.id === exec.project_id)?.aingel_chat_id);
  // The Guide's read arrives a few seconds after the pause: fill the note box
  // then, unless the user has started typing (RP-094 part 2: the box was empty
  // when the user looked, and the suggestion never reached the model).
  useEffect(() => {
    if (suggested) setNote((cur) => (cur.trim() ? cur : suggested));
  }, [suggested]);

  const refresh = async () => {
    try {
      setExecutions(await api.executions.list(100));
    } catch {
      /* SSE will catch up */
    }
  };

  const act = async (fn: () => Promise<unknown>, failMsg: string) => {
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (err: unknown) {
      alert(failMsg + ': ' + (err instanceof Error ? err.message : 'unknown error'));
    } finally {
      setBusy(false);
    }
  };

  const limit = facts.limit === 'time' ? 'time' : 'turn';
  const prog = guide?.progress ? PROGRESS_STYLE[guide.progress] : undefined;
  const missing = facts.deliverables_missing || [];
  const written = facts.deliverables_written || [];
  const noContinue = facts.continues_left === 0;

  return (
    <div
      className="mx-1 mb-1.5 px-2 py-1.5 rounded text-xs"
      style={{ background: 'rgba(138,90,18,0.08)', border: '1px solid rgba(138,90,18,0.35)' }}
      onClick={(ev) => ev.stopPropagation()}
    >
      <div className="font-semibold mb-1" style={{ color: '#8a5a12' }}>
        ⏸ Paused at its {limit} limit{(facts.part || 1) > 1 ? ` · part ${facts.part}` : ''}
      </div>
      <div className="text-soft mb-1">
        {facts.turns_this_part ?? '?'} turns, {facts.minutes_this_part ?? '?'} min in this part ·
        ${(facts.cost_usd_so_far ?? exec.cost_usd ?? 0).toFixed(2)} so far · another block ≈
        ${(facts.next_block_cost_usd_estimate ?? 0).toFixed(2)}
      </div>
      {(missing.length > 0 || written.length > 0) && (
        <div className="mb-1">
          {written.length > 0 && <span className="text-status-done">Written: {written.join(', ')}. </span>}
          {missing.length > 0 && <span className="text-danger">Not written yet: {missing.join(', ')}.</span>}
        </div>
      )}
      {(facts.repeated_actions || []).length > 0 && (
        <div className="mb-1 text-danger">
          Repeated: {(facts.repeated_actions || []).map((r) => `${r.action} ×${r.times}`).join('; ')}
        </div>
      )}
      {guide?.summary ? (
        <div className="mb-1">
          <span className="font-semibold">Guide</span>
          {prog && <span style={{ color: prog.color }}> ({prog.label})</span>}
          {guide.recommendation && <span className="text-faint"> · suggests: {REC_LABEL[guide.recommendation] || guide.recommendation}</span>}
          <div className="whitespace-pre-wrap">{guide.summary}</div>
        </div>
      ) : (
        <div className="mb-1 text-faint italic">The Guide's read is on its way (or no Guide is set).</div>
      )}
      {facts.model_status && (
        <div className="mb-1">
          <button className="btn-xs" onClick={() => setShowStatus((v) => !v)}>
            {showStatus ? 'Hide' : 'Show'} the model's status
          </button>
          {showStatus && <div className="whitespace-pre-wrap mt-1 text-soft">{facts.model_status}</div>}
        </div>
      )}
      {guideChatId != null && (
        <div className="mb-1">
          <button
            className="btn-xs"
            data-tip="The pause notice is also in the Guide chat; open it to ask the Guide about this run"
            onClick={() => window.dispatchEvent(new CustomEvent('aingel:open-chat', { detail: { chatId: guideChatId } }))}
          >
            Open the Guide chat
          </button>
        </div>
      )}
      {canEdit && (
        <>
          {suggested && note.trim() !== suggested.trim() && (
            <div className="mb-1 text-faint">
              Guide's suggested note: <span className="italic">{suggested}</span>{' '}
              <button className="btn-xs" disabled={busy} onClick={() => setNote(suggested)}>Use it</button>{' '}
              <button className="btn-xs" disabled={busy}
                      onClick={() => setNote((cur) => (cur.trim() ? `${cur.trim()}\n${suggested}` : suggested))}>
                Add it
              </button>
            </div>
          )}
          <textarea
            className="w-full text-xs rounded border border-default p-1 mb-1 dark:bg-surface-dark-raised dark:border-border-dark-default"
            rows={2}
            placeholder="Optional note to the model when it continues (e.g. write the table now)"
            value={note}
            onChange={(ev) => setNote(ev.target.value)}
            disabled={busy || noContinue}
          />
          <div className="flex gap-1 flex-wrap">
            <button
              className="btn-xs text-status-done"
              disabled={busy || noContinue}
              data-tip={noContinue ? 'No continues left — stop it and review, or split the task' : 'Run another block of turns from where it stopped'}
              onClick={() => act(() => api.executions.continue(exec.id, note), 'Continue failed')}
            >
              Continue{facts.continues_left != null ? ` (${facts.continues_left} left)` : ''}
            </button>
            <button
              className="btn-xs"
              disabled={busy}
              data-tip="Finish now with what it produced; the Guide reviews it"
              onClick={() => {
                if (window.confirm('Stop this run and keep what it produced? It finishes now and the Guide reviews it.')) {
                  act(() => api.executions.stop(exec.id, false), 'Stop failed');
                }
              }}
            >
              Stop &amp; review
            </button>
            <button
              className="btn-xs text-danger border-danger"
              disabled={busy}
              data-tip="Stop and discard its changes; the task returns to pending"
              onClick={() => {
                if (window.confirm('Stop this run and DISCARD its changes? Its task branch is deleted and the task returns to pending.')) {
                  act(() => api.executions.stop(exec.id, true), 'Discard failed');
                }
              }}
            >
              Discard
            </button>
          </div>
        </>
      )}
    </div>
  );
};
