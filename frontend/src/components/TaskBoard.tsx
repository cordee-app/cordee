import { useState, useRef, useEffect, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { TaskCard } from './TaskCard';
import { Spinner } from './Spinner';
import { BoardTools } from './BoardTools';
import { PROVIDER_BUCKETS } from './providers';
import { useIsMobile } from '../hooks/useIsMobile';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import { cn } from '../utils/cn';
import type { WorkSession, Task } from '../types';

const STATUSES = ['pending', 'confirmed', 'done', 'failed'] as const;

const STATUS_COLORS: Record<string, string> = {
  pending: '#645c50',
  confirmed: '#7a6420',
  running: '#3f628f',
  done: '#56633f',
  failed: '#a3402f',
  skip: '#645c50',
  cancelled: '#645c50',
};

const displayStatus = (s: string) => (s === 'cancelled' || s === 'skip') ? 'pending' : s;

const ASSIGNED_BUCKETS = PROVIDER_BUCKETS.filter((b) => b.slot !== null);

const SUB_SESSION_BUDGET = 150000;

function fmtHHMM(iso: string): string {
  if (!iso) return '\u2014';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function tokensForGrouping(t: Task): number {
  if (t.status === 'done') return t.actual_tokens || 0;
  return t.estimated_tokens || 0;
}

function groupBySubSession(tasksInOrder: Task[], budget: number): { tasks: Task[]; tokens: number }[] {
  const groups: { tasks: Task[]; tokens: number }[] = [];
  let current: { tasks: Task[]; tokens: number } = { tasks: [], tokens: 0 };
  for (const t of tasksInOrder) {
    const tok = tokensForGrouping(t);
    if (current.tasks.length && (current.tokens + tok) > budget) {
      groups.push(current);
      current = { tasks: [], tokens: 0 };
    }
    current.tasks.push(t);
    current.tokens += tok;
  }
  if (current.tasks.length) groups.push(current);
  return groups;
}

function tokLabelFor(tasks: Task[]): string {
  const total = tasks.reduce((sum, t) => sum + (t.estimated_tokens || 0), 0);
  return total >= 1000 ? `${(total / 1000).toFixed(1)}k tok` : `${total} tok`;
}

const WorkSessionBar = ({ ws, slot, canEdit }: { ws: WorkSession | undefined; slot: number | null; canEdit: boolean }) => {
  const setWorkSessions = useStore((s) => s.setWorkSessions);
  const setTasks = useStore((s) => s.setTasks);
  const setExecutions = useStore((s) => s.setExecutions);

  if (!ws || slot !== 1) return null;

  const secondsRemaining = ws.seconds_remaining;
  const expired = ws.expired;
  const nextRunAt = ws.next_run_at;
  const pauseReason = ws.pause_reason;
  const tokenBudget = ws.token_budget || 150000;
  const tokensUsed = ws.tokens_used || 0;

  if (secondsRemaining == null && !nextRunAt && !tokensUsed) return null;

  const totalSec = ws.duration_seconds || 18000;
  let barPct = 0;
  let barColor = '#56633f';
  let timerStr = '';
  if (secondsRemaining != null && secondsRemaining > 0) {
    barPct = Math.min(100, Math.round((1 - secondsRemaining / totalSec) * 100));
    if (barPct >= 90) barColor = '#a3402f';
    else if (barPct >= 70) barColor = '#b2622d';
    const h = Math.floor(secondsRemaining / 3600);
    const m = Math.floor((secondsRemaining % 3600) / 60);
    const s = secondsRemaining % 60;
    timerStr = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  } else if (expired) {
    timerStr = '00:00:00';
    barPct = 100;
    barColor = '#a3402f';
  }

  const isQuotaPaused = pauseReason === 'quota';
  const isWindowFull = pauseReason === 'window_full' || (nextRunAt && !isQuotaPaused);

  const tokPct = tokenBudget > 0 ? Math.round((tokensUsed / tokenBudget) * 100) : 0;

  const handleResume = async () => {
    try {
      await api.workSessions.start(1, true);
      const [t, e, ws2] = await Promise.all([
        api.tasks.list(),
        api.executions.list(100),
        api.workSessions.status(),
      ]);
      setTasks(t);
      setExecutions(e);
      setWorkSessions(ws2);
    } catch { /* ignore */ }
  };

  return (
    <div
      className="px-2 py-1.5 mb-1.5 rounded-md text-sm+"
      style={{
        background: 'rgba(198,113,57,0.08)',
        border: '1px solid rgba(198,113,57,0.2)',
      }}
    >
      <div className="flex items-center gap-2 mb-1">
        <span
          className="font-bold font-mono text-md-"
          style={{ color: '#c67139' }}
        >
          {timerStr || '\u2014'}
        </span>
        <div className="flex-1 bg-border-muted rounded h-1.5 overflow-hidden">
          <div
            className="h-full rounded transition-[width] duration-300"
            style={{ width: `${barPct}%`, background: barColor }}
          />
        </div>
        {isQuotaPaused ? (
          <>
            <span className="text-xs font-semibold" style={{ color: '#b2622d' }}>
              Quota paused {nextRunAt ? `\u2014 resumes at ${fmtHHMM(nextRunAt)}` : ''}
            </span>
            {canEdit && (
              <button
                data-tip="Resume the paused column"
                onClick={handleResume}
                className="px-2 py-0.5 rounded-sm border text-xs font-semibold cursor-pointer"
                style={{
                  border: '1px solid #c67139',
                  background: 'rgba(198,113,57,0.15)',
                  color: '#c67139',
                }}
              >
                Resume
              </button>
            )}
          </>
        ) : isWindowFull ? (
          <>
            <span style={{ color: '#645c50', fontSize: 10 }}>
              Next at {nextRunAt ? fmtHHMM(nextRunAt) : '\u2014'}
            </span>
            {canEdit && (
              <button
                data-tip="Force-resume the column now"
                onClick={handleResume}
                className="px-2 py-0.5 rounded-sm border text-xs font-semibold cursor-pointer"
                style={{
                  border: '1px solid #c67139',
                  background: 'rgba(198,113,57,0.15)',
                  color: '#c67139',
                }}
              >
                Resume
              </button>
            )}
          </>
        ) : (
          <span className="text-xs text-faint">Window</span>
        )}
      </div>
      {tokenBudget > 0 && (
        <div className="flex items-center gap-1.5 text-xs text-muted">
          <div className="flex-1 bg-border-muted rounded-sm h-1 overflow-hidden">
            <div
              className="h-full rounded-sm transition-[width] duration-300"
              style={{ width: `${tokPct}%`, background: '#3f628f' }}
            />
          </div>
          <span className="font-mono whitespace-nowrap">
            {(tokensUsed / 1000).toFixed(1)}k / {Math.round(tokenBudget / 1000)}k tok ({tokPct}%)
          </span>
        </div>
      )}
    </div>
  );
};

function LockIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"
        fill="currentColor"
      />
    </svg>
  );
}

export const TaskBoard = () => {
  const tasks = useStore((s) => s.tasks);
  const projects = useStore((s) => s.projects);
  const activeProject = useStore((s) => s.activeProject);
  const setTasks = useStore((s) => s.setTasks);
  const setExecutions = useStore((s) => s.setExecutions);
  const setWorkSessions = useStore((s) => s.setWorkSessions);
  const setActiveStatus = useStore((s) => s.setActiveStatus);
  const workSessions = useStore((s) => s.workSessions);
  const kanbanTokenBudget = useStore((s) => s.kanbanTokenBudget) || SUB_SESSION_BUDGET;
  const showArchived = useStore((s) => s.showArchived);
  const activePhase = useStore((s) => s.activePhase);
  const selectedTaskId = useStore((s) => s.selectedTaskId);

  const runError = useStore((s) => s.runError);
  const clearRunError = useStore((s) => s.clearRunError);
  const [runningSlots, setRunningSlots] = useState<Set<number | null>>(new Set());
  const boardRef = useRef<HTMLDivElement>(null);
  const [runAllRunning, setRunAllRunning] = useState<'claude' | 'mistral' | 'scaleway' | null>(null);
  const isMobile = useIsMobile();
  const perms = useProjectPermissions(activeProject);

  // Reset the "Run all (project)" button back to idle once the slot it kicked
  // off has no running tasks left. Without this the button stays stuck on
  // "Running..." forever after the batch finishes. We only reset after the
  // slot has actually been observed running (not while it's still 'confirmed'
  // in the brief window before the run thread flips it to 'running').
  const runAllSlot = runAllRunning === 'claude' ? 1 : runAllRunning === 'mistral' ? 2 : runAllRunning === 'scaleway' ? 4 : null;
  const runAllSeenRunning = useRef<Set<number>>(new Set());
  useEffect(() => {
    if (runAllSlot === null) return;
    const slotTasks = tasks.filter(
      (t) => t.project_id === activeProject && (t.work_session_slot ?? null) === runAllSlot
    );
    const anyRunning = slotTasks.some((t) => t.status === 'running');
    if (anyRunning) runAllSeenRunning.current.add(runAllSlot);
    if (runAllSeenRunning.current.has(runAllSlot) && !anyRunning) {
      setRunAllRunning(null);
      runAllSeenRunning.current.delete(runAllSlot);
    }
  }, [tasks, activeProject, runAllSlot]);
  const [mobileCol, setMobileCol] = useState<'pending' | 'confirmed' | 'done' | 'failed'>('pending');
  const [toast, setToast] = useState<{ msg: string; level: 'info' | 'warn' | 'error' } | null>(null);
  const showToast = useCallback((msg: string, level: 'info' | 'warn' | 'error' = 'error') => {
    setToast({ msg, level });
    setTimeout(() => setToast(null), 5000);
  }, []);
  // Surface background run failures (e.g. free-tier quota blocks): the kick
  // returns ok:true and the failure happens in the run thread, so without
  // this the board shows "Running" forever with no explanation. Also clears
  // the stuck Running indicators for the affected slot.
  useEffect(() => {
    if (!runError) return;
    showToast(runError.msg, 'error');
    const slot = runError.slot;
    if (slot !== null) {
      setRunningSlots((prev) => {
        if (!prev.has(slot)) return prev;
        const next = new Set(prev);
        next.delete(slot);
        return next;
      });
      runAllSeenRunning.current.delete(slot);
      setRunAllRunning((cur) => {
        const curSlot = cur === 'claude' ? 1 : cur === 'mistral' ? 2 : cur === 'scaleway' ? 4 : null;
        return curSlot === slot ? null : cur;
      });
    }
    clearRunError();
  }, [runError, showToast, clearRunError]);
  // Clear the column "Running" pill once its tasks stop running. Slots are
  // only removed after they were actually observed running (not while tasks
  // are still 'confirmed' in the window before the run thread flips them).
  // Quota-refused batches never flip anything, so those rely on the
  // run_failed handler above instead.
  const runningSlotsSeenRunning = useRef<Set<number>>(new Set());
  useEffect(() => {
    if (runningSlots.size === 0) return;
    const stillRunning = new Set<number>();
    for (const t of tasks) {
      const s = t.work_session_slot ?? null;
      if (t.project_id === activeProject && t.status === 'running' && s !== null && runningSlots.has(s)) {
        stillRunning.add(s);
        runningSlotsSeenRunning.current.add(s);
      }
    }
    let changed = false;
    const next = new Set(runningSlots);
    for (const s of Array.from(runningSlotsSeenRunning.current)) {
      if (!next.has(s)) {
        runningSlotsSeenRunning.current.delete(s);
        continue;
      }
      if (!stillRunning.has(s)) {
        next.delete(s);
        runningSlotsSeenRunning.current.delete(s);
        changed = true;
      }
    }
    if (changed) setRunningSlots(next);
  }, [tasks, activeProject, runningSlots]);

  const activeProjectData = projects.find((p) => p.id === activeProject);
  const scwLocked = Boolean(
    activeProjectData &&
      activeProjectData.scw_session_enabled &&
      activeProjectData.scw_session_bucket &&
      activeProjectData.scw_kms_key_id
  );

  const filtered = activeProject
    ? tasks.filter((t) => t.project_id === activeProject && (!t.archived || showArchived))
    : tasks.filter((t) => (!t.archived || showArchived));

  const phaseFiltered = activePhase
    ? filtered.filter((t) => t.phase_name === activePhase.phaseName)
    : filtered;

  const taskFiltered = selectedTaskId != null
    ? phaseFiltered.filter((t) => t.id === selectedTaskId)
    : phaseFiltered;

  const pendingTasks = taskFiltered.filter((t) => displayStatus(t.status) === 'pending');
  const confirmedTasks = taskFiltered.filter((t) => t.status === 'confirmed' || t.status === 'running');
  const doneTasks = taskFiltered.filter((t) => displayStatus(t.status) === 'done');
  const failedTasks = taskFiltered.filter((t) => displayStatus(t.status) === 'failed');

  // Project-wide held tasks per slot (gate_state === 'hold'). Used to disable
  // the "Run all (project)" buttons when every task in a slot is awaiting
  // AIngel review — running would be a silent no-op.
  const heldCountForSlot = (slot: number): number =>
    confirmedTasks.filter((t) => (t.work_session_slot ?? null) === slot && t.gate_state === 'hold').length;

  const reload = useCallback(async () => {
    try {
      const [t, e, ws] = await Promise.all([
        api.tasks.list(),
        api.executions.list(100),
        api.workSessions.status(),
      ]);
      setTasks(t);
      setExecutions(e);
      setWorkSessions(ws);
    } catch { /* ignore */ }
  }, [setTasks, setExecutions, setWorkSessions]);

  useEffect(() => {
    const board = boardRef.current;
    if (!board) return;
    const cols = board.querySelectorAll('.board-col');
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const status = (entry.target as HTMLElement).dataset.status;
            if (status && (STATUSES as readonly string[]).includes(status)) {
              setActiveStatus(status);
            }
          }
        }
      },
      { root: board.parentElement, threshold: 0.5 }
    );
    cols.forEach((col) => observer.observe(col));
    return () => observer.disconnect();
  }, [setActiveStatus]);

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (!detail?.boardScrollTo) return;
      const board = boardRef.current;
      if (!board) return;
      const col = board.querySelector(`[data-status="${detail.boardScrollTo}"]`);
      if (col) col.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    };
    window.addEventListener('aingel:board-scroll', handler);
    return () => window.removeEventListener('aingel:board-scroll', handler);
  }, []);

  const handleRunColumn = useCallback(
    async (slot: number | null) => {
      if (slot === null) return;
      const slotTasks = filtered.filter((t) => (t.work_session_slot ?? null) === slot);
      // Match legacy /old/ behaviour: column Run runs only CONFIRMED tasks.
      // Slot 1 (Claude Pro) further restricts to the FIRST sub-session's confirmed tasks.
      let confirmedIds: number[];
      if (slot === 1) {
        const groups = groupBySubSession(slotTasks, kanbanTokenBudget);
        const firstGroup = groups[0];
        confirmedIds = (firstGroup ? firstGroup.tasks : [])
          .filter((t) => t.status === 'confirmed')
          .map((t) => t.id);
      } else {
        confirmedIds = slotTasks
          .filter((t) => t.status === 'confirmed')
          .map((t) => t.id);
      }
      if (!confirmedIds.length) return;
      const slotNum = slot;
      try {
        await api.columns.run(slotNum, confirmedIds);
        await reload();
        setRunningSlots((prev) => new Set(prev).add(slotNum));
        // SSE events will update the UI when tasks complete - no polling needed
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : 'Failed to run column';
        console.error('Failed to run column:', e);
        showToast(msg);
      }
    },
    [filtered, reload, kanbanTokenBudget, showToast]
  );

  const handleRunAll = useCallback(
    async (provider: 'claude' | 'mistral' | 'scaleway') => {
      if (!activeProject) return;
      try {
        const res = provider === 'claude'
          ? await api.projects.runAllClaude(activeProject)
          : provider === 'mistral'
            ? await api.projects.runAllMistral(activeProject)
            : await api.projects.runAllScaleway(activeProject);
        await reload();

        // Surface dependency skips so the click isn't a silent no-op. The
        // runtime gate in _run_seq skips tasks whose deps aren't 'done' and
        // leaves them 'confirmed' — without this feedback the user sees
        // "Run all" do nothing with no explanation.
        const skipped = res.skipped_by_deps;
        if (skipped && skipped.length > 0) {
          const summary = skipped
            .map((s) => `#${s.task_id} depends on pending task #${s.unmet.map((u) => `#${u.id} (${u.status})`).join(', ')}`)
            .join('; ');
          if (res.queued === 0) {
            showToast(`Nothing ran — unmet dependency discovered: ${summary}. A task cannot run before the task it depends on is done.`, 'error');
          } else {
            showToast(`Queued ${res.queued}. ${skipped.length} task(s) skipped — unmet dependency: ${summary}`, 'info');
          }
        } else if (res.queued === 0 && res.message) {
          showToast(res.message, 'info');
        } else {
          const slot = provider === 'claude' ? 1 : provider === 'mistral' ? 2 : 4;
          runAllSeenRunning.current.delete(slot);
          setRunAllRunning(provider);
        }
        // SSE events will update the UI when tasks complete - no polling needed
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : `Failed to run all ${provider}`;
        console.error(`Failed to run all ${provider}:`, e);
        showToast(msg);
      }
    },
    [activeProject, reload, showToast]
  );

  // Count confirmed runnable tasks for a slot (slot-1 sub-session rule).
  // Tasks held by an AIngel gate (H2a clarifying questions or H2/H3 review) are
  // excluded — the runner skips them, so running now would be a no-op.
  const runnableCountFor = (slot: number | null, bucketTasks: Task[]): number => {
    if (slot === null) return 0;
    const runnable = bucketTasks.filter((t) => t.status === 'confirmed' && t.gate_state !== 'hold');
    if (slot === 1) {
      const groups = groupBySubSession(runnable, kanbanTokenBudget);
      const firstGroup = groups[0];
      return firstGroup ? firstGroup.tasks.length : 0;
    }
    return runnable.length;
  };

  // Confirmed tasks in a slot currently held by an AIngel gate (awaiting review
  // or clarification). Used to disable the Run button with a tooltip.
  const heldCountFor = (slot: number | null, bucketTasks: Task[]): number => {
    if (slot === null) return 0;
    return bucketTasks.filter((t) => t.status === 'confirmed' && t.gate_state === 'hold').length;
  };

  return (
    <div className="project-board p-2 h-full flex flex-col relative" ref={boardRef}>
      {scwLocked && (
        <div
          className="absolute bottom-2 right-3 pointer-events-none text-amber-400/60"
          title="KMS-encrypted Scaleway session active"
        >
          <LockIcon className="w-7 h-7" />
        </div>
      )}
      {isMobile && (
        <div className="flex items-center justify-between mb-3">
          <BoardTools variant="mobile" />
        </div>
      )}
      {isMobile && (
        <div className="mobile-board-seg flex gap-1 mb-2">
          {(['pending', 'confirmed', 'done', 'failed'] as const).map((s) => (
            <button
              key={s}
              data-tip={`Show the ${s} column`}
              className={cn(
                'flex-1 py-2 px-1 rounded-md border text-xs font-semibold capitalize cursor-pointer',
                mobileCol === s
                  ? 'bg-accent dark:bg-accent-dark-DEFAULT text-white border-accent dark:border-accent-dark-DEFAULT'
                  : 'bg-surface-raised dark:bg-surface-dark-raised text-text-muted dark:text-text-dark-muted border-border-muted dark:border-border-dark-muted',
              )}
              onClick={() => setMobileCol(s)}
            >
              {s}
            </button>
          ))}
        </div>
      )}
      <div className={cn('board-grid flex-1 overflow-hidden', isMobile ? 'flex flex-col' : 'grid grid-cols-4 gap-2.5')}>

        {/* Pending column */}
        <div className={cn('board-col bg-surface-panel rounded-md p-2 flex flex-col overflow-hidden', isMobile && mobileCol !== 'pending' && 'hidden')} data-status="pending">
          <h4 className="text-sm+ m-0 mb-2 capitalize text-soft" style={{ color: STATUS_COLORS['pending'] }}>
            Pending ({pendingTasks.length})
          </h4>
          <div className="flex-1 overflow-y-auto flex flex-col gap-2">
            {/* Unassigned sub-row */}
            {(() => {
              const unassigned = pendingTasks.filter((t) => (t.work_session_slot ?? null) === null);
              if (unassigned.length === 0) return null;
              return (
                <div>
                  <div
                    className="text-xs font-semibold mb-1 pb-0.5 flex items-center gap-1.5"
                    style={{ color: '#645c50', borderTop: '2px solid #645c50', paddingTop: 4 }}
                  >
                    <span>Unassigned</span>
                    <span className="font-mono text-faint">({unassigned.length})</span>
                  </div>
                  {unassigned.map((t) => (
                    <TaskCard key={t.id} task={t} variant="board" />
                  ))}
                </div>
              );
            })()}

            {/* Per-bucket sub-rows */}
            {ASSIGNED_BUCKETS.map((bucket) => {
              const slot = bucket.slot as number;
              const bucketPending = pendingTasks.filter((t) => (t.work_session_slot ?? null) === slot);
              if (bucketPending.length === 0) return null;
              return (
                <div key={bucket.key}>
                  <div
                    className="text-xs font-semibold mb-1 pb-0.5 flex items-center gap-1.5 flex-wrap"
                    style={{ color: bucket.color, borderTop: `2px solid ${bucket.color}`, paddingTop: 4 }}
                  >
                    <span>{bucket.label}</span>
                    <span className="font-mono text-faint">({bucketPending.length})</span>
                    <span className="text-faint font-mono">{'\u00B7'} {tokLabelFor(bucketPending)}</span>
                  </div>
                  {bucketPending.map((t) => (
                    <TaskCard key={t.id} task={t} variant="board" />
                  ))}
                </div>
              );
            })}

            {pendingTasks.length === 0 && (
              <p className="text-sm+ text-faint italic m-0">No tasks</p>
            )}
          </div>
        </div>

        {/* Confirmed column */}
        <div className={cn('board-col bg-surface-panel rounded-md p-2 flex flex-col overflow-hidden', isMobile && mobileCol !== 'confirmed' && 'hidden')} data-status="confirmed">
          <h4 className="text-sm+ m-0 mb-2 capitalize text-soft" style={{ color: STATUS_COLORS['confirmed'] }}>
            Confirmed ({confirmedTasks.length})
          </h4>
          <div className="flex-1 overflow-y-auto flex flex-col gap-2">
            {ASSIGNED_BUCKETS.map((bucket) => {
              const slot = bucket.slot as number;
              const bucketTasks = confirmedTasks.filter((t) => (t.work_session_slot ?? null) === slot);
              if (bucketTasks.length === 0) return null;
              const runningCount = bucketTasks.filter((t) => t.status === 'running').length;
              const runnableCount = runnableCountFor(slot, bucketTasks);
              const heldCount = heldCountFor(slot, bucketTasks);
              const hasRunning = runningSlots.has(slot) || runningCount > 0;

              return (
                <div key={bucket.key}>
                  <div
                    className="text-xs font-semibold mb-1 pb-0.5 flex items-center gap-1.5 flex-wrap"
                    style={{ color: bucket.color, borderTop: `2px solid ${bucket.color}`, paddingTop: 4 }}
                  >
                    <span>{bucket.label}</span>
                    <span className="font-mono text-faint">({bucketTasks.length})</span>
                    <span className="text-faint font-mono">{'\u00B7'} {tokLabelFor(bucketTasks)}</span>
                    {hasRunning && (
                      <>
                        <Spinner size={12} color={bucket.color} title="Running" />
                        <span className="text-xs font-semibold" style={{ color: bucket.color }}>
                          Running ({runningCount})
                        </span>
                      </>
                    )}
                    {runnableCount > 0 && perms.canEdit && (
                      <button
                        data-tip={slot === 1
                          ? `Run the first sub-session (${runnableCount} task${runnableCount === 1 ? '' : 's'})`
                          : `Run all ${runnableCount} confirmed task${runnableCount === 1 ? '' : 's'}`}
                        className="text-xs px-2 py-0.5 rounded-sm border cursor-pointer font-bold ml-auto"
                        style={{
                          border: '1px solid #56633f',
                          background: 'rgba(86,99,63,0.15)',
                          color: '#56633f',
                        }}
                        onClick={() => handleRunColumn(slot)}
                      >
                        {slot === 1 ? `Run (${runnableCount})` : `Run all (${runnableCount})`}
                      </button>
                    )}
                    {runnableCount === 0 && heldCount > 0 && (
                      <button
                        data-tip={`${heldCount} task${heldCount === 1 ? '' : 's'} awaiting Guide review — answer the questions on the card to run`}
                        className="text-xs px-2 py-0.5 rounded-sm border font-bold ml-auto"
                        style={{
                          border: '1px solid #645c50',
                          background: 'rgba(150,150,150,0.12)',
                          color: '#645c50',
                          cursor: 'not-allowed',
                        }}
                        disabled
                      >
                        {heldCount === 1 ? 'Awaiting review' : `Awaiting review (${heldCount})`}
                      </button>
                    )}
                    {activeProject && slot === 1 && perms.canEdit && (
                      <button
                        data-tip={heldCountForSlot(1) > 0 && runnableCount === 0
                          ? 'All Claude tasks are awaiting Guide review — answer the questions on the cards to run'
                          : "Run every Claude task already in slot 1 for the active project; auto-resumes across 5h windows. Drag tasks into Claude Pro first."}
                        disabled={runAllRunning !== null || (heldCountForSlot(1) > 0 && runnableCount === 0)}
                        onClick={() => handleRunAll('claude')}
                        className="text-xs px-2 py-0.5 rounded-sm border font-bold"
                        style={{
                          border: '1px solid #c67139',
                          background: 'rgba(198,113,57,1)',
                          color: '#fff',
                          cursor: runAllRunning !== null || (heldCountForSlot(1) > 0 && runnableCount === 0) ? 'not-allowed' : 'pointer',
                          opacity: runAllRunning !== null || (heldCountForSlot(1) > 0 && runnableCount === 0) ? 0.6 : 1,
                        }}
                      >
                        {runAllRunning === 'claude' ? 'Running...' : (heldCountForSlot(1) > 0 && runnableCount === 0) ? 'Awaiting review' : 'Run all (project)'}
                      </button>
                    )}
                    {activeProject && slot === 2 && perms.canEdit && (
                      <button
                        data-tip={heldCountForSlot(2) > 0 && runnableCount === 0
                          ? 'All Mistral tasks are awaiting Guide review — answer the questions on the cards to run'
                          : "Run every Mistral task already in slot 2 for the active project. Drag Mistral tasks into Mistral Pro first."}
                        disabled={runAllRunning !== null || (heldCountForSlot(2) > 0 && runnableCount === 0)}
                        onClick={() => handleRunAll('mistral')}
                        className="text-xs px-2 py-0.5 rounded-sm border font-bold"
                        style={{
                          border: '1px solid #b8742f',
                          background: 'rgba(184,116,47,1)',
                          color: '#fff',
                          cursor: runAllRunning !== null || (heldCountForSlot(2) > 0 && runnableCount === 0) ? 'not-allowed' : 'pointer',
                          opacity: runAllRunning !== null || (heldCountForSlot(2) > 0 && runnableCount === 0) ? 0.6 : 1,
                        }}
                      >
                        {runAllRunning === 'mistral' ? 'Running...' : (heldCountForSlot(2) > 0 && runnableCount === 0) ? 'Awaiting review' : 'Run all (project)'}
                      </button>
                    )}
                    {activeProject && slot === 4 && perms.canEdit && (
                      <button
                        data-tip={heldCountForSlot(4) > 0 && runnableCount === 0
                          ? 'All Scaleway tasks are awaiting Guide review — answer the questions on the cards to run'
                          : "Run every Scaleway task already in slot 4 for the active project. Drag scw-* tasks into EU Scaleway first."}
                        disabled={runAllRunning !== null || (heldCountForSlot(4) > 0 && runnableCount === 0)}
                        onClick={() => handleRunAll('scaleway')}
                        className="text-xs px-2 py-0.5 rounded-sm border font-bold"
                        style={{
                          border: '1px solid #7a5aa6',
                          background: 'rgba(122,90,166,1)',
                          color: '#1a1a2e',
                          cursor: runAllRunning !== null || (heldCountForSlot(4) > 0 && runnableCount === 0) ? 'not-allowed' : 'pointer',
                          opacity: runAllRunning !== null || (heldCountForSlot(4) > 0 && runnableCount === 0) ? 0.6 : 1,
                        }}
                      >
                        {runAllRunning === 'scaleway' ? 'Running...' : (heldCountForSlot(4) > 0 && runnableCount === 0) ? 'Awaiting review' : 'Run all (project)'}
                      </button>
                    )}
                  </div>

                  <WorkSessionBar
                    ws={workSessions.find((ws) => ws.slot === slot)}
                    slot={slot}
                    canEdit={perms.canEdit}
                  />

                  {slot === 1
                    ? (() => {
                        const groups = groupBySubSession(bucketTasks, kanbanTokenBudget);
                        return groups.length
                          ? groups.map((g, i, arr) => {
                              const alpha = 'ABCDEFGHIJKLMNOP';
                              const label = arr.length === 1 ? 'Sub-session A' : `Sub-session ${alpha[i] || i + 1}`;
                              const k = Math.round(g.tokens / 1000);
                              const cap = Math.round(kanbanTokenBudget / 1000);
                              return (
                                <div
                                  key={i}
                                  className="rounded-md p-1.5"
                                  style={{
                                    border: '1px solid rgba(198,113,57,0.25)',
                                    marginBottom: i < arr.length - 1 ? 10 : 0,
                                  }}
                                >
                                  <div
                                    className="text-xs font-semibold flex justify-between"
                                    style={{ color: '#c67139' }}
                                  >
                                    <span>{label}</span>
                                    <span className="font-mono">{k}k / {cap}k tok</span>
                                  </div>
                                  {g.tasks.map((t) => (
                                    <TaskCard key={t.id} task={t} variant="board" />
                                  ))}
                                </div>
                              );
                            })
                          : null;
                      })()
                    : bucketTasks.map((t) => (
                        <TaskCard key={t.id} task={t} variant="board" />
                      ))}
                </div>
              );
            })}

            {confirmedTasks.length === 0 && (
              <p className="text-sm+ text-faint italic m-0">No tasks</p>
            )}
          </div>
        </div>

        {/* Done column */}
        <div className={cn('board-col bg-surface-panel rounded-md p-2 flex flex-col overflow-hidden', isMobile && mobileCol !== 'done' && 'hidden')} data-status="done">
          <h4 className="text-sm+ m-0 mb-2 capitalize text-soft" style={{ color: STATUS_COLORS['done'] }}>
            Done ({doneTasks.length})
          </h4>
          <div className="flex-1 overflow-y-auto flex flex-col gap-1.5">
            {doneTasks.length === 0 && (
              <p className="text-sm+ text-faint italic m-0">No tasks</p>
            )}
            {doneTasks.map((t) => (
              <TaskCard key={t.id} task={t} variant="board" />
            ))}
          </div>
        </div>

        {/* Failed column */}
        <div className={cn('board-col bg-surface-panel rounded-md p-2 flex flex-col overflow-hidden', isMobile && mobileCol !== 'failed' && 'hidden')} data-status="failed">
          <h4 className="text-sm+ m-0 mb-2 capitalize text-soft" style={{ color: STATUS_COLORS['failed'] }}>
            Failed ({failedTasks.length})
          </h4>
          <div className="flex-1 overflow-y-auto flex flex-col gap-1.5">
            {failedTasks.length === 0 && (
              <p className="text-sm+ text-faint italic m-0">No tasks</p>
            )}
            {failedTasks.map((t) => (
              <TaskCard key={t.id} task={t} variant="board" />
            ))}
          </div>
        </div>

      </div>
      {toast && (
        <div
          className="fixed bottom-4 left-1/2 -translate-x-1/2 z-[300] px-4 py-2 rounded-md text-sm+ text-white shadow-lg"
          style={{
            background:
              toast.level === 'error' ? 'rgba(163, 64, 47, 0.95)'
              : toast.level === 'warn' ? 'rgba(178, 98, 45, 0.95)'
              : 'rgba(63, 98, 143, 0.95)',
            maxWidth: '90vw',
          }}
          onClick={() => setToast(null)}
        >
          {toast.msg}
        </div>
      )}
    </div>
  );
};

export default TaskBoard;