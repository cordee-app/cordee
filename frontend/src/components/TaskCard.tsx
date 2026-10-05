import { useState, useEffect, useCallback, useMemo } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { cn } from '../utils/cn';
import { fmtTokens } from '../utils/tokens';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { CSSProperties } from 'react';
import type { Task, GateQuestion } from '../types';
import { SLOT_LABELS, allowedSlotsForModel } from './providers';
import { Spinner } from './Spinner';
import { TaskOverflowMenu } from './TaskOverflowMenu';
import { ArrowDown, ArrowUp, TriangleAlert, Paperclip } from 'lucide-react';

interface Props {
  task: Task;
  variant?: 'board' | 'full';
}

const STATUS_COLORS: Record<string, string> = {
  pending: '#645c50',
  confirmed: '#7a6420',
  running: '#3f628f',
  done: '#56633f',
  failed: '#a3402f',
};

export const TaskCard = ({ task, variant = 'full' }: Props) => {
  const perms = useProjectPermissions(task.project_id);
  const [doneCollapsed, setDoneCollapsed] = useState(true);
  const [depUp, setDepUp] = useState(0);
  const [depDown, setDepDown] = useState(0);
  const [slotApplying, setSlotApplying] = useState<number | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submittingAnswers, setSubmittingAnswers] = useState(false);

  const selectionMode = useStore((s) => s.selectionMode);
  const selectedTasks = useStore((s) => s.selectedTasks);
  const toggleTaskSelect = useStore((s) => s.toggleTaskSelect);
  const setDepsModalTaskId = useStore((s) => s.setDepsModalTaskId);
  const setModModalTaskId = useStore((s) => s.setModModalTaskId);
  const setShowAddModal = useStore((s) => s.setShowAddModal);
  const models = useStore((s) => s.models);
  const roles = useStore((s) => s.roles);
  const setTasks = useStore((s) => s.setTasks);
  const setExecutions = useStore((s) => s.setExecutions);
  const setWorkSessions = useStore((s) => s.setWorkSessions);
  const setMemPanelOpen = useStore((s) => s.setMemPanelOpen);
  const setMemActiveTab = useStore((s) => s.setMemActiveTab);

  const isSelected = selectedTasks.has(task.id);
  const statusColor = STATUS_COLORS[task.status] || '#645c50';
  const isRunning = task.status === 'running';
  const isDone = task.status === 'done';
  const isArchived = Boolean(task.archived);

  // H2a hold: the task is under AIngel's clarifying-questions gate — either
  // still being reviewed (no questions produced yet) or awaiting answers to
  // questions that have landed. The card stays faded and locked (not editable)
  // for the whole hold, and only unlocks once the user answers or overrides
  // (gate → open).
  const h2aHeld = task.gate_state === 'hold' && task.gate_source === 'H2a';
  // True review window: held but no questions yet — badge reads "reviewing…".
  const h2aReviewing = h2aHeld && !task.gate_report_h2_json;

  const executions = useStore((s) => s.executions);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!isRunning) return;
    const id = setInterval(() => setTick((t) => t + 1), 60000);
    return () => clearInterval(id);
  }, [isRunning]);
  void tick;
  const runningExec = useMemo(() => {
    if (!isRunning) return null;
    const cands = executions.filter((e) => e.task_id === task.id && e.status === 'running');
    if (!cands.length) return null;
    cands.sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''));
    return cands[0];
  }, [executions, task.id, isRunning]);
  const taskElapsedMins = (() => {
    const iso = runningExec?.started_at || (task as unknown as { updated_at?: string }).updated_at || (task as unknown as { created_at?: string }).created_at;
    if (!iso) return 0;
    const t = new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime();
    if (isNaN(t)) return 0;
    return Math.floor((Date.now() - t) / 60000);
  })();
  const taskIsStuck = isRunning && taskElapsedMins > 15 && ((runningExec?.tokens_output || 0) === 0) && ((runningExec?.cost_usd || 0) * 0 === 0 || !runningExec || (runningExec.tokens_output || 0) === 0);

  const model = models.find((m) => m.id === task.model);
  const modelColor = model?.color || '#645c50';
  const modelLabel = model?.label || task.model;

  const awaitingHf = Boolean(task.awaiting_model && task.hf_repo_id);
  const modelBadge = awaitingHf ? (
    <>
      <span
        className="task-model text-sm font-semibold"
        style={{
          background: 'rgba(198,113,57,0.15)',
          color: '#c67139',
          border: '1px solid rgba(198,113,57,0.4)',
          padding: '2px 8px',
        }}
        title={`Hugging Face model: ${task.hf_repo_id}`}
      >
        HF
      </span>
      <span
        className="text-2xs px-1.5 py-px rounded-full font-semibold whitespace-nowrap"
        style={{
          background: 'rgba(198,113,57,0.15)',
          color: '#c67139',
          border: '1px solid rgba(198,113,57,0.4)',
        }}
      >
        Waiting
      </span>
    </>
  ) : (
    <span
      className="task-model text-sm font-semibold"
      style={{
        background: `${modelColor}22`,
        color: modelColor,
        border: `1px solid ${modelColor}55`,
        padding: '2px 8px',
      }}
    >
      {modelLabel}
    </span>
  );

  const roleName = useMemo(() => {
    if (task.role_id == null) return null;
    const role = roles.find((r) => r.id === task.role_id);
    return role?.name || null;
  }, [task.role_id, roles]);

  useEffect(() => {
    api.tasks.dependencies.list(task.id)
      .then((deps) => {
        setDepUp(deps.depended_on_by?.length || 0);
        setDepDown(deps.depends_on?.length || 0);
      })
      .catch(() => {});
  }, [task.id]);

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

  // Parse clarifying questions from the H2a gate payload (gate_report_h2_json).
  const gateQuestions = useMemo<GateQuestion[]>(() => {
    if (task.gate_source !== 'H2a' || !task.gate_report_h2_json) return [];
    try {
      const parsed = JSON.parse(task.gate_report_h2_json);
      const qs = Array.isArray(parsed?.questions) ? parsed.questions : [];
      return qs
        .filter((q: unknown) => q && typeof q === 'object' && (q as GateQuestion).question)
        .map((q: GateQuestion) => ({
          id: q.id || `q${Math.random().toString(36).slice(2, 6)}`,
          question: q.question,
          why: q.why,
          options: Array.isArray(q.options) ? q.options : undefined,
        }));
    } catch {
      return [];
    }
  }, [task.gate_source, task.gate_report_h2_json]);

  const handleSubmitAnswers = useCallback(async () => {
    if (!gateQuestions.length) return;
    const filled: Record<string, string> = {};
    for (const q of gateQuestions) {
      const v = (answers[q.id] || '').trim();
      if (v) filled[q.id] = v;
    }
    if (!Object.keys(filled).length) return;
    setSubmittingAnswers(true);
    try {
      await api.tasks.gate(task.id, 'answer', filled);
      setAnswers({});
      await reload();
    } catch { /* ignore */ } finally {
      setSubmittingAnswers(false);
    }
  }, [gateQuestions, answers, task.id, reload]);

  const handleCardClick = () => {
    if (selectionMode) {
      toggleTaskSelect(task.id);
    }
  };

  const handleEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (h2aHeld) return; // locked while AIngel holds the task for review/questions
    if (!perms.canEdit) return;
    setShowAddModal(true);
    setModModalTaskId(task.id);
  };

  const handleSlot = async (e: React.MouseEvent, slot: number | null) => {
    e.stopPropagation();
    setSlotApplying(slot);
    try {
      const slotVal = slot as never as number | undefined;
      const res = await api.tasks.update(task.id, { work_session_slot: slotVal }) as Record<string, unknown> | undefined;
      // Optimistic local update: avoid the heavy reload() that iterates all 14
      // project DBs. The card moves to its new bucket immediately. If the
      // backend auto-promoted the task to a different slot (slot-1 overflow →
      // PAYG), fall back to a full reload for correctness.
      if (res && res.auto_promoted) {
        await reload();
      } else {
        // Optimistic local update: mirror the backend's synchronous gate change
        // so the UI reacts instantly instead of waiting for the H2a-complete SSE.
        // Confirming → optimistic H2a hold (card fades, "reviewing…" badge, Run
        // buttons disable). Un-confirming → clear gate. For non-autopilot
        // projects the backend won't actually hold, but its task_changed SSE
        // corrects the state within ~1s — so applying the optimistic hold always
        // is safe and makes the fade instant.
        const confirming = slotVal != null;
        const gatePatch = confirming
          ? {
              gate_state: 'hold',
              gate_source: 'H2a',
              gate_reason: 'Guide is reviewing this task…',
              gate_report_h2_json: undefined,
            }
          : {
              gate_state: 'open',
              gate_source: '',
              gate_reason: '',
              gate_report_h2_json: undefined,
            };
        setTasks((prev) => prev.map((t) =>
          t.id === task.id
            ? {
                ...t,
                work_session_slot: slotVal ?? undefined,
                status: slotVal == null
                  ? (t.status === 'confirmed' ? 'pending' : t.status)
                  : 'confirmed',
                ...gatePatch,
              }
            : t
        ));
      }
    } catch (err: unknown) {
      console.error('Slot update failed:', err);
    } finally {
      setSlotApplying(null);
    }
  };

  const handleDoneToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    setDoneCollapsed((v) => !v);
  };

  const currentSlot = task.work_session_slot == null ? null : task.work_session_slot;
  const slotLabel = currentSlot == null ? 'Unassigned' : (SLOT_LABELS[String(currentSlot)] || `Slot ${currentSlot}`);
  const validSlots = task.description?.trim() ? allowedSlotsForModel(task.model) : [null];
  const isReadOnly = isRunning || isDone || task.status === 'failed' || h2aHeld;

  const hasDeps = depUp > 0 || depDown > 0;

  const estTokens = task.estimated_tokens >= 1000
    ? `~${Math.round(task.estimated_tokens / 1000)}k tok`
    : task.estimated_tokens > 0
      ? `~${task.estimated_tokens} tok`
      : '— tok';

  const estCost = task.estimated_cost != null && task.estimated_cost > 0
    ? `est. $${task.estimated_cost.toFixed(3)}`
    : null;

  const descPreview = (task.description || '').length > 80
    ? task.description!.slice(0, 80) + '\u2026'
    : (task.description || '');

  if (isDone && doneCollapsed) {
    const shortTitle = (task.title || '').length > 50
      ? task.title.slice(0, 50) + '\u2026'
      : task.title;
    return (
      <div
        className={cn('task-card', isSelected && 'selected')}
        style={{
          borderLeftColor: statusColor,
          opacity: isArchived ? 0.5 : 0.7,
        }}
        onClick={handleCardClick}
      >
        {selectionMode && (
          <input
            type="checkbox"
            data-tip="Select this task"
            className="task-check mr-1.5"
            checked={isSelected}
            readOnly
            onClick={(e) => { e.stopPropagation(); toggleTaskSelect(task.id); }}
          />
        )}
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-semibold text-sm+" style={{ color: modelColor }}>#{task.id}</span>
          <span className="text-sm+">{shortTitle}</span>
          {task.estimated_tokens > 0 && (
            <span className="text-xs text-faint">{estTokens}</span>
          )}
          {estCost && <span className="text-xs text-faint">{estCost}</span>}
          <span className="text-xs font-semibold ml-auto text-status-done">Done</span>
          <button
            data-tip="Expand task details"
            className="btn-xs ml-1"
            onClick={handleDoneToggle}
          >
            +
          </button>
        </div>
      </div>
    );
  }

  if (variant === 'board') {
    return (
      <div
        className={cn('task-card', `status-${task.status}`, isSelected && 'selected')}
        style={{
          borderLeft: `3px solid ${statusColor}`,
          '--task-status-color': statusColor,
          ...(isRunning ? { borderLeftColor: `${statusColor}88` } : {}),
          ...(isArchived ? { opacity: 0.65 } : {}),
          ...(h2aReviewing ? { opacity: 0.55 } : {}),
          ...(gateQuestions.length > 0 ? { animation: 'pulse-border 2s ease-in-out infinite' } : {}),
        } as CSSProperties}
        onClick={handleCardClick}
      >
        <div className="flex items-center gap-1.5">
          {selectionMode && (
            <input
              type="checkbox"
              data-tip="Select this task"
              checked={isSelected}
              readOnly
              onClick={(e) => { e.stopPropagation(); toggleTaskSelect(task.id); }}
              className="mr-0.5"
            />
          )}
          <span className="task-id font-semibold text-muted text-sm">#{task.id}</span>
          {modelBadge}
          {task.status === 'skip' && (
            <span
              className="text-2xs px-1.5 py-px rounded-full font-semibold whitespace-nowrap"
              style={{
                background: 'rgba(138,109,59,0.15)',
                color: '#8a6d3b',
                border: '1px solid rgba(138,109,59,0.4)',
              }}
              title="Deliberately skipped — assign a provider to revive, or use the ⋯ menu → Redo to reset"
            >
              Skipped
            </span>
          )}
          {isRunning && <Spinner size={12} color={statusColor} title="Running" />}
          {isArchived && (
            <span
              className="text-2xs px-1.5 py-px rounded-full font-semibold whitespace-nowrap"
              style={{
                background: 'rgba(100,92,80,0.12)',
                color: '#645c50',
                border: '1px solid rgba(100,92,80,0.3)',
              }}
            >
              Archived
            </span>
          )}
          {task.gate_state === 'hold' && (
            <span
              className="text-2xs px-1.5 py-px rounded-full font-semibold cursor-pointer inline-flex items-center gap-1"
              style={{
                background: 'rgba(198,113,57,0.15)',
                color: '#c67139',
                border: '1px solid rgba(198,113,57,0.4)',
                ...(h2aReviewing ? { animation: 'pulse-border 2s ease-in-out infinite' } : {}),
              }}
              onClick={handleEdit}
                title={h2aReviewing ? 'Guide is reviewing this task…' : gateQuestions.length > 0 ? 'Guide — your input needed (answer the questions below)' : (task.gate_reason || 'Guide review required')}
            >
              <TriangleAlert size={12} className="shrink-0" /> {h2aReviewing ? 'reviewing…' : gateQuestions.length > 0 ? 'answer needed' : 'review'}
            </span>
          )}
          {task.gate_state === 'closed' && (
            <span className="text-2xs text-faint italic" title={task.gate_reason || ''}>ack</span>
          )}
          <span className="text-xs text-faint ml-auto">{estTokens}</span>
          <div style={{ position: 'relative', display: 'inline-flex' }}>
            <TaskOverflowMenu task={task} isArchived={isArchived} held={h2aHeld} />
          </div>
        </div>
        {isRunning && (
          <div className="flex items-center gap-1.5 mt-1 flex-wrap">
            <span className="text-2xs font-semibold text-status-running">{taskElapsedMins}m</span>
            {runningExec && (
              <span className="text-2xs font-mono text-faint">{fmtTokens(runningExec.tokens_input)}↑{fmtTokens(runningExec.tokens_output)}↓</span>
            )}
            {taskIsStuck && (
              <span className="text-2xs font-semibold rounded px-1 py-px" style={{ background: '#fff2eb', color: '#b2622d', border: '1px solid #b2622d' }} title={`Running for ${taskElapsedMins} min with 0 output tokens. Consider cancelling and re-running.`}>⚠ may be stuck</span>
            )}
          </div>
        )}
        <div
          className={cn('task-title text-sm font-medium mt-0.5 px-0', perms.canEdit && 'cursor-pointer')}
          onClick={perms.canEdit ? handleEdit : undefined}
        >
          {task.title}
        </div>
        {perms.canEdit && !isReadOnly && !isArchived && !awaitingHf && (task.status === 'pending' || task.status === 'confirmed' || task.status === 'skip' || task.status === 'cancelled') && (() => {
          const slots = allowedSlotsForModel(task.model);
          return (
            <div className="flex gap-1 mt-1 flex-wrap">
              <span className="text-2xs text-faint self-center">Assign:</span>
              {slots.map((s) => {
                const sKey = String(s ?? 0);
                const active = currentSlot === s;
                const blocked = s !== null && !task.description?.trim();
                return (
                  <button
                    key={sKey}
                    data-tip={blocked ? 'Add a description first' : (SLOT_LABELS[sKey] || 'Unassigned')}
                    className="btn-xs text-2xs"
                    style={{
                      background: active ? `${modelColor}22` : 'white',
                      borderColor: active ? modelColor : '#dcd3c4',
                      borderWidth: active ? 2 : 1,
                      color: active ? modelColor : '#645c50',
                      fontWeight: active ? 700 : 400,
                      opacity: slotApplying === s ? 0.6 : blocked ? 0.5 : 1,
                      padding: '1px 6px',
                    }}
                    disabled={slotApplying !== null || blocked}
                    onClick={(e) => handleSlot(e, s)}
                  >
                    {slotApplying === s ? '...' : (SLOT_LABELS[sKey] || 'Unassigned')}
                  </button>
                );
              })}
            </div>
          );
        })()}
        {gateQuestions.length > 0 && (
          <div className="mt-1.5 px-0">
            {gateQuestions.map((q) => (
              <div key={q.id} className="mb-1.5">
                <div className="text-xs font-medium text-ink mb-0.5 dark:text-text-dark">{q.question}</div>
                {q.why ? <div className="text-faint text-2xs mb-0.5">{q.why}</div> : null}
                {q.options && q.options.length > 0 ? (
                  <select
                    data-tip="Choose an answer for this question"
                    className="w-full text-xs rounded border border-default bg-surface px-1.5 py-1 dark:bg-surface-dark dark:border-border-dark-default dark:text-text-dark"
                    value={answers[q.id] || ''}
                    onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
                  >
                    <option value="">{'Select…'}</option>
                    {q.options.map((opt) => (
                      <option key={opt} value={opt}>{opt}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    data-tip="Type your answer for this question"
                    className="w-full text-xs rounded border border-default bg-surface px-1.5 py-1 dark:bg-surface-dark dark:border-border-dark-default dark:text-text-dark"
                    placeholder="Your answer…"
                    value={answers[q.id] || ''}
                    onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
                  />
                )}
              </div>
            ))}
            {perms.canEdit && (
              <button
                data-tip="Submit your answers to unblock this task"
                className="btn-xs font-semibold"
                style={{
                  background: 'rgba(63,98,143,0.15)',
                  border: '1px solid rgba(63,98,143,0.4)',
                  color: '#3f628f',
                }}
                disabled={submittingAnswers}
                onClick={async (e) => {
                  e.stopPropagation();
                  await handleSubmitAnswers();
                }}
              >
                {submittingAnswers ? 'Submitting…' : 'Submit answers'}
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div
      className={cn('task-card', `status-${task.status}`, isSelected && 'selected')}
      style={{
        borderLeft: `3px solid ${statusColor}`,
        '--task-status-color': statusColor,
        ...(isRunning || gateQuestions.length > 0 ? { animation: 'pulse-border 2s ease-in-out infinite' } : {}),
        ...(isArchived ? { opacity: 0.65 } : {}),
        ...(h2aReviewing ? { opacity: 0.55 } : {}),
      } as CSSProperties}
      onClick={handleCardClick}
    >
      <div className="flex items-center gap-1.5 mb-1">
        {selectionMode && (
          <input
            data-tip="Select this task"
            type="checkbox"
            checked={isSelected}
            readOnly
            onClick={(e) => { e.stopPropagation(); toggleTaskSelect(task.id); }}
            className="mr-0.5"
          />
        )}
        <span className="task-id font-semibold text-muted text-sm">#{task.id}</span>
        {modelBadge}
        {task.status === 'skip' && (
          <span
            className="text-2xs px-1.5 py-px rounded-full font-semibold whitespace-nowrap"
            style={{
              background: 'rgba(138,109,59,0.15)',
              color: '#8a6d3b',
              border: '1px solid rgba(138,109,59,0.4)',
            }}
            title="Deliberately skipped — assign a provider to revive, or use the ⋯ menu → Redo to reset"
          >
            Skipped
          </span>
        )}
        {task.role_id != null && (
          <span
            className="text-2xs px-[7px] py-px rounded-full font-semibold whitespace-nowrap"
            style={{
              background: 'rgba(122,90,166,0.15)',
              color: '#7a5aa6',
              border: '1px solid rgba(122,90,166,0.35)',
            }}
          >
            {roleName || `Role #${task.role_id}`}
          </span>
        )}
        {isArchived && (
          <span
            className="text-2xs px-[7px] py-px rounded-full font-semibold whitespace-nowrap"
            style={{
              background: 'rgba(100,92,80,0.12)',
              color: '#645c50',
              border: '1px solid rgba(100,92,80,0.3)',
            }}
          >
            Archived
          </span>
        )}
        {isDone && (
          <button data-tip="Collapse task details" className="btn-xs ml-auto" onClick={handleDoneToggle}>
            -
          </button>
        )}
      </div>

      {task.handoff_context && (
        <div
          className="text-sm+ px-1 pb-1 overflow-hidden max-h-[36px]"
          style={{ color: '#3f628f' }}
          title={task.handoff_context}
        >
          Handoff: {task.handoff_context.length > 140
            ? task.handoff_context.slice(0, 140) + '\u2026'
            : task.handoff_context}
        </div>
      )}

      <div className={cn('task-title text-base font-medium mb-1 leading-[1.3] px-1 pb-[3px]', perms.canEdit && 'cursor-pointer')} onClick={perms.canEdit ? handleEdit : undefined}>
        {task.title}
      </div>

      {task.gate_state === 'hold' && (
        <div
          className="mx-1 mb-1.5 px-2 py-1.5 rounded text-xs"
          style={{
            background: 'rgba(198,113,57,0.12)',
            border: '1px solid rgba(198,113,57,0.35)',
          }}
        >
          <div className="text-status-pending font-semibold mb-1 flex items-center gap-1">
            <TriangleAlert size={12} className="shrink-0" /> Guide review required
            {task.gate_source ? ` (${task.gate_source})` : ''}
          </div>
          {task.gate_reason && (
            <div className="text-faint mb-1 max-h-[36px] overflow-hidden">
              {task.gate_reason.length > 140
                ? task.gate_reason.slice(0, 140) + '\u2026'
                : task.gate_reason}
            </div>
          )}
          {gateQuestions.length > 0 && (
            <div className="mb-1.5">
              {gateQuestions.map((q) => (
                <div key={q.id} className="mb-1.5">
                  <div className="text-ink font-medium mb-0.5">{q.question}</div>
                  {q.why ? <div className="text-faint text-2xs mb-0.5">{q.why}</div> : null}
                  {q.options && q.options.length > 0 ? (
                    <select
                      data-tip="Choose an answer for this question"
                      className="w-full text-xs rounded border border-default bg-surface px-1.5 py-1 dark:bg-surface-dark dark:border-border-dark-default dark:text-text-dark"
                      value={answers[q.id] || ''}
                      onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
                    >
                      <option value="">{'Select…'}</option>
                      {q.options.map((opt) => (
                        <option key={opt} value={opt}>{opt}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      data-tip="Type your answer for this question"
                      className="w-full text-xs rounded border border-default bg-surface px-1.5 py-1 dark:bg-surface-dark dark:border-border-dark-default dark:text-text-dark"
                      placeholder="Your answer…"
                      value={answers[q.id] || ''}
                      onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
                    />
                  )}
                </div>
              ))}
              {perms.canEdit && (
                <button
                  data-tip="Submit your answers to unblock this task"
                  className="btn-xs font-semibold"
                  style={{
                    background: 'rgba(63,98,143,0.15)',
                    border: '1px solid rgba(63,98,143,0.4)',
                    color: '#3f628f',
                  }}
                  disabled={submittingAnswers}
                  onClick={async (e) => {
                    e.stopPropagation();
                    await handleSubmitAnswers();
                  }}
                >
                  {submittingAnswers ? 'Submitting\u2026' : 'Submit answers'}
                </button>
              )}
            </div>
          )}
          {perms.canEdit && (
            <div className="flex gap-1">
              <button
                data-tip="Open a chat to discuss this task with your Guide"
                className="btn-xs font-semibold"
                style={{
                  background: 'rgba(198,113,57,0.15)',
                  border: '1px solid rgba(198,113,57,0.4)',
                  color: '#c67139',
                }}
                onClick={async (e) => {
                  e.stopPropagation();
                  try {
                    const res = await api.tasks.gate(task.id, 'discuss');
                    if ((res as Record<string, unknown>).chat_id) {
                      reload();
                    } else {
                      reload();
                    }
                  } catch { /* ignore */ }
                }}
              >
                Discuss
              </button>
              <button
                data-tip="Acknowledge the review and close the gate"
                className="btn-xs font-semibold"
                style={{
                  background: 'rgba(63,98,143,0.1)',
                  border: '1px solid rgba(63,98,143,0.3)',
                  color: '#3f628f',
                }}
                onClick={async (e) => {
                  e.stopPropagation();
                  try {
                    await api.tasks.gate(task.id, 'acknowledge');
                    reload();
                  } catch { /* ignore */ }
                }}
              >
                Acknowledge
              </button>
              {(task.status === 'pending' || task.status === 'confirmed') && (
                <button
                  data-tip="Override the gate and run this task now"
                  className="btn-xs font-semibold"
                  style={{
                    background: 'rgba(163,64,47,0.08)',
                    border: '1px solid rgba(163,64,47,0.3)',
                    color: '#a3402f',
                  }}
                  onClick={async (e) => {
                    e.stopPropagation();
                    try {
                      await api.tasks.gate(task.id, 'override');
                      const execRes = await api.execute(task.id);
                      reload();
                      if (execRes && execRes.status !== 'done') {
                        alert('Run failed: ' + (execRes.error || 'unknown error'));
                      }
                    } catch { /* ignore */ }
                  }}
                >
                  Override
                </button>
              )}
            </div>
          )}
        </div>
      )}
      {task.gate_state === 'closed' && (
        <div
          className="mx-1 mb-1.5 text-2xs text-muted italic px-1"
          title={task.gate_reason || ''}
        >
          Gate acknowledged
        </div>
      )}

      {descPreview && (
        <div
          onClick={perms.canEdit ? handleEdit : undefined}
          className={cn('text-xs text-faint px-1 pb-[5px] leading-[1.4] whitespace-pre-wrap break-words', perms.canEdit && 'cursor-pointer')}
        >
          {descPreview}
        </div>
      )}

      {task.phase_name && <div className="task-phase text-sm+ text-soft mb-1">{task.phase_name}</div>}

      {hasDeps && (
        <div className="flex gap-1 mb-1 px-1">
          <button
            data-tip="Manage dependencies"
            className="btn-xs text-2xs font-semibold"
            style={{
              background: 'rgba(63,98,143,0.1)',
              color: '#3f628f',
              border: '1px solid rgba(63,98,143,0.3)',
              borderRadius: 999,
              padding: '1px 7px',
            }}
            onClick={(e) => { e.stopPropagation(); setDepsModalTaskId(task.id); }}
          >
            {depUp > 0 && (
              <span className="inline-flex items-center gap-0.5">
                {depUp}<ArrowUp size={10} className="shrink-0" />
              </span>
            )}
            {depUp > 0 && depDown > 0 && ' '}
            {depDown > 0 && (
              <span className="inline-flex items-center gap-0.5">
                {depDown}<ArrowDown size={10} className="shrink-0" />
              </span>
            )}
          </button>
        </div>
      )}

      <div className="task-meta flex flex-col gap-[3px] px-1 mb-1.5">
        <div className="text-xs text-muted font-semibold">
          {slotLabel}
        </div>
        <div className="flex gap-3 flex-wrap">
          <span className="text-sm+ text-faint">
            <span className="font-semibold">Tokens:</span> {estTokens}
          </span>
          {estCost && (
            <span className="text-sm+ text-faint">
              <span className="font-semibold">Cost:</span> {estCost}
            </span>
          )}
        </div>
        {isRunning && (
          <span className="inline-flex items-center gap-1.5 flex-wrap">
            <span className="text-status-running font-semibold text-xs">Running... {taskElapsedMins}m</span>
            {runningExec && (
              <span className="text-xs font-mono text-faint" title={`Tokens: ${runningExec.tokens_input || 0} input / ${runningExec.tokens_output || 0} output`}>
                {fmtTokens(runningExec.tokens_input)}↑{fmtTokens(runningExec.tokens_output)}↓
              </span>
            )}
            {!runningExec && taskElapsedMins > 0 && (
              <span className="text-xs font-mono text-faint">{taskElapsedMins}m</span>
            )}
            {taskIsStuck && (
              <span
                className="text-xs font-semibold rounded px-1.5 py-px"
                style={{ background: '#fff2eb', color: '#b2622d', border: '1px solid #b2622d' }}
                title={`Running for ${taskElapsedMins} min with 0 output tokens. Consider cancelling and re-running.`}
              >
                ⚠ may be stuck
              </span>
            )}
          </span>
        )}
      </div>

      {perms.canEdit && !isReadOnly && !awaitingHf && validSlots.length > 1 && (
        <div className="flex gap-1 px-1 pb-1.5 flex-wrap">
          <span className="text-2xs text-faint self-center">Slot:</span>
          {validSlots.map((s) => {
            const sKey = String(s ?? 0);
            const active = currentSlot === s;
            return (
              <button
                key={sKey}
                data-tip={SLOT_LABELS[sKey] || 'Unassigned'}
                className="btn-xs text-xs"
                style={{
                  background: active ? `${modelColor}22` : 'white',
                  borderColor: active ? modelColor : '#dcd3c4',
                  borderWidth: active ? 2 : 1,
                  color: active ? modelColor : '#645c50',
                  fontWeight: active ? 700 : 400,
                  opacity: slotApplying === s ? 0.6 : 1,
                  padding: '2px 8px',
                }}
                disabled={slotApplying !== null}
                onClick={(e) => handleSlot(e, s)}
              >
                {slotApplying === s ? '...' : (SLOT_LABELS[sKey] || 'Unassigned')}
              </button>
            );
          })}
        </div>
      )}

      {isReadOnly && (
        <div className="text-xs text-faint px-1 pb-1">
          {slotLabel}
        </div>
      )}

      <div className="task-actions flex gap-2 px-1 pb-1 items-center">
        {perms.canEdit && (
          <button data-tip={h2aHeld ? 'Answer the Guide review questions first' : 'Edit task'} className="btn-xs" onClick={handleEdit} disabled={isArchived || h2aHeld}>
            Edit
          </button>
        )}
        {isDone && (
          <button data-tip="Collapse task details" className="btn-xs" onClick={handleDoneToggle}>
            Collapse
          </button>
        )}
        {isDone && (
          <button
            data-tip="View files this task produced in the Files browser"
            className="btn-xs inline-flex items-center gap-1"
            onClick={() => { setMemPanelOpen(true); setMemActiveTab('files'); }}
          >
            <Paperclip size={11} /> Files
          </button>
        )}
        <div style={{ marginLeft: 'auto', position: 'relative' }}>
          <TaskOverflowMenu task={task} isArchived={isArchived} held={h2aHeld} />
        </div>
      </div>
    </div>
  );
};

export default TaskCard;