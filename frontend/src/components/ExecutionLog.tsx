import { useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { cn } from '../utils/cn';
import { fmtTokens } from '../utils/tokens';
import { fmtUsd } from '../utils/currency';
import { useIsMobile } from '../hooks/useIsMobile';
import { useProjectPermissions, roleAtLeast } from '../hooks/useProjectPermissions';
import type { ProjectRole } from '../hooks/useProjectPermissions';
import type { Execution, MemoryLevel } from '../types';
import { CircleCheck, X, LoaderCircle, GitCommitHorizontal, GitBranch, GitMerge, Sparkles, RefreshCw, CircleSlash, ChevronRight, ChevronDown } from 'lucide-react';
import { relUrl } from '../utils/file';
import { stuckReason } from '../utils/stuck';

const LOG_HEIGHT_KEY = 'log_panel_height';
const COL_WIDTHS_KEY = 'superagent_exec_col_widths';

const DEFAULT_COL_WIDTHS: number[] = [130, 50, 60, 260, 100, 80, 70, 120, 280];

const fmtTimeFull = (iso?: string): string => {
  if (!iso) return '—';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

const truncate = (s: string | undefined, n: number): string => {
  if (!s) return '';
  return s.length > n ? s.slice(0, n) + '\u2026' : s;
};

const getModelColor = (model: string): string => {
  const hash = model.split('').reduce((a, c) => a + c.charCodeAt(0), 0);
  const hue = hash % 360;
  return `hsl(${hue}, 60%, 45%)`;
};

const getModelLabel = (e: Execution): string => {
  const label = e.model_label;
  if (label) return label;
  const parts = e.model.split('/');
  return parts[parts.length - 1] || e.model;
};

const statusStyle = (status: string, memoryStatus?: string): { color: string; bg: string; label: string } => {
  // If the execution was rejected, show red "Rejected" regardless of done status
  if (memoryStatus === 'rejected' && status === 'done') {
    return { color: '#a3402f', bg: '#f7e2dc', label: 'Rejected' };
  }
  switch (status) {
    case 'done':
      return { color: '#56633f', bg: '#e1eecc', label: 'Done' };
    case 'failed':
      return { color: '#a3402f', bg: '#f7e2dc', label: 'Failed' };
    case 'running':
      return { color: '#3f628f', bg: '#e3ebf5', label: 'Running' };
    case 'pending':
      return { color: '#b2622d', bg: '#fff2eb', label: 'Pending' };
    case 'skipped':
      return { color: '#645c50', bg: '#eee7db', label: 'Skipped' };
    case 'rejected':
      return { color: '#a3402f', bg: '#f7e2dc', label: 'Rejected' };
    default:
      return { color: '#645c50', bg: '#eee7db', label: status };
  }
};

const gitExecutionState = (e: Execution) => {
  const gitBranch = (e.git_branch || '').trim();
  const gitCommit = (e.git_commit || '').trim();
  const gitMerge = (e.git_merge_commit || '').trim();
  if (gitMerge) {
    return {
      label: `Merged${gitCommit ? ` \u00b7 ${gitCommit}` : ''}`,
      title: `Git branch merged into main${gitBranch ? `\nBranch: ${gitBranch}` : ''}${gitMerge ? `\nMerge commit: ${gitMerge}` : ''}`,
      kind: 'merged',
    };
  }
  if (gitCommit) {
    return {
      label: `Committed${gitCommit ? ` \u00b7 ${gitCommit}` : ''}`,
      title: `Git branch committed${gitBranch ? `\nBranch: ${gitBranch}` : ''}${gitCommit ? `\nCommit: ${gitCommit}` : ''}`,
      kind: 'committed',
    };
  }
  if (gitBranch) {
    return {
      label: `Branch ${gitBranch}`,
      title: `Git branch ready${gitBranch ? `\nBranch: ${gitBranch}` : ''}`,
      kind: 'branch',
    };
  }
  return null;
};

const elapsedMinutes = (iso?: string): number => {
  if (!iso) return 0;
  const t = new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).getTime();
  if (isNaN(t)) return 0;
  return Math.floor((Date.now() - t) / 60000);
};

// RAG provenance badge — makes it crystal-clear whether an execution grounded
// its answer in the shared legal library (RAG) vs. the model's generic knowledge.
const ragBadgeStyle = (label?: string): { label: string; bg: string; fg: string; title: string } => {
  switch (label) {
    case 'RAG-PREFETCHED':
      return { label: 'RAG', bg: '#e3ebf5', fg: '#3f628f', title: 'Answer grounded in retrieved legal library (pre-fetched citations inlined into the prompt)' };
    case 'RAG-TOOL':
      return { label: 'RAG', bg: '#e1eecc', fg: '#56633f', title: 'Answer grounded in retrieved legal library (model called rag_query mid-run)' };
    case 'RAG-REQUIRED':
      return { label: 'RAG?', bg: '#fff2eb', fg: '#b2622d', title: 'RAG was required but no citations were retrieved (library empty/down)' };
    case 'RAG-OFF':
      return { label: 'RAG off', bg: '#eee7db', fg: '#645c50', title: 'RAG not used for this execution' };
    default:
      return { label: '', bg: '', fg: '', title: '' };
  }
};

const RagBadge = ({ exec }: { exec: Execution }) => {
  const s = ragBadgeStyle(exec.rag_label);
  if (!s.label) return null;
  return (
    <span
      className="inline-flex items-center rounded text-[10px] font-semibold px-1.5 py-px"
      style={{ background: s.bg, color: s.fg }}
      title={s.title}
    >
      {s.label}
    </span>
  );
};

const FilesBadge = ({ exec }: { exec: Execution }) => {
  const used = exec.context_used ?? [];
  const binary = exec.context_binary ?? [];
  const review = exec.context_review_needed ?? false;
  if (used.length === 0 && binary.length === 0 && !review) return null;
  const lines = [used.join('\n')];
  if (binary.length > 0) lines.push(`Not inlined (binary): ${binary.join(', ')}`);
  if (review) lines.push('Fallback capped index was used.');
  const title = lines.filter(Boolean).join('\n');
  return (
    <span
      className="inline-flex items-center rounded text-[10px] font-semibold px-1.5 py-px"
      style={{ background: '#eee7db', color: '#645c50' }}
      title={title}
    >
      📎 {used.length} file{used.length === 1 ? '' : 's'}
    </span>
  );
};

export const ExecutionLog = () => {
  const { executions, setExecutions, activeProject, tasks } = useStore();
  const isMobile = useIsMobile();
  const perms = useProjectPermissions(activeProject);
  const user = useStore((s) => s.user);
  const projects = useStore((s) => s.projects);
  const canEditExec = useCallback((projectId?: number | null): boolean => {
    if (projectId == null || projectId === activeProject) return perms.canEdit;
    if (user == null || user.role === 'admin') return true;
    const p = projects.find((x) => x.id === projectId);
    return roleAtLeast((p?.current_user_role ?? null) as ProjectRole | null, 'member');
  }, [activeProject, perms.canEdit, user, projects]);
  const [height, setHeight] = useState(() => {
    const saved = localStorage.getItem(LOG_HEIGHT_KEY);
    return saved ? parseInt(saved, 10) : 200;
  });
  const [rejectFormId, setRejectFormId] = useState<number | null>(null);
  const [rejectFeedback, setRejectFeedback] = useState('');
  const [forkFormId, setForkFormId] = useState<number | null>(null);
  const [forkFeedback, setForkFeedback] = useState('');
  const [reviewPopover, setReviewPopover] = useState<{ execId: number; review: string } | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [diffModal, setDiffModal] = useState<{ open: boolean; title: string; content: string; truncated?: boolean; baseBranch?: string; taskBranch?: string }>({
    open: false, title: '', content: '',
  });
  const [errorModal, setErrorModal] = useState<{ open: boolean; title: string; content: string }>({
    open: false, title: '', content: '',
  });
  const [outputModal, setOutputModal] = useState<{ open: boolean; title: string; content: string; loading: boolean }>({
    open: false, title: '', content: '', loading: false,
  });
  const [colWidths, setColWidths] = useState<number[]>(() => {
    try {
      const saved = localStorage.getItem(COL_WIDTHS_KEY);
      if (saved) return JSON.parse(saved) as number[];
    } catch { /* ignore */ }
    return DEFAULT_COL_WIDTHS;
  });
  // tick every 60s so running elapsed timers stay fresh (ChatPanel.tsx:231 pattern)
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 60000);
    return () => clearInterval(id);
  }, []);
  // reference tick so the component re-renders when it changes
  void tick;
  const panelRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const colDragIdx = useRef<number | null>(null);
  const colDragStartX = useRef(0);
  const colDragStartW = useRef(0);

  const refreshExecutions = useCallback(async () => {
    try {
      const list = await api.executions.list(100);
      setExecutions(list);
    } catch { /* ignore */ }
  }, [setExecutions]);

  const clampedHeight = Math.min(Math.max(height, 100), window.innerHeight * 0.7);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    dragging.current = true;
    const startY = e.clientY;
    const startH = panelRef.current?.offsetHeight || height;
    document.body.style.userSelect = 'none';
    const onMove = (ev: MouseEvent) => {
      if (!dragging.current) return;
      const delta = startY - ev.clientY;
      const newH = startH + delta;
      setHeight(newH);
    };
    const onUp = () => {
      dragging.current = false;
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      localStorage.setItem(LOG_HEIGHT_KEY, String(panelRef.current?.offsetHeight || height));
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [height]);

  const handleColResizeStart = useCallback((e: React.MouseEvent, idx: number) => {
    e.preventDefault();
    e.stopPropagation();
    colDragIdx.current = idx;
    colDragStartX.current = e.clientX;
    colDragStartW.current = colWidths[idx] || DEFAULT_COL_WIDTHS[idx] || 100;
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';
    const onMove = (ev: MouseEvent) => {
      if (colDragIdx.current == null) return;
      const delta = ev.clientX - colDragStartX.current;
      const newW = Math.max(40, colDragStartW.current + delta);
      setColWidths((prev) => {
        const next = [...prev];
        next[colDragIdx.current!] = newW;
        return next;
      });
    };
    const onUp = () => {
      if (colDragIdx.current != null) {
        setColWidths((prev) => {
          localStorage.setItem(COL_WIDTHS_KEY, JSON.stringify(prev));
          return prev;
        });
      }
      colDragIdx.current = null;
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }, [colWidths]);

  useEffect(() => {
    return () => {
      document.body.style.userSelect = '';
    };
  }, []);

  const filtered = executions.filter((e) => {
    if (!activeProject) return true;
    return e.project_id === activeProject;
  });

  const handleApprove = async (execId: number, level: MemoryLevel) => {
    try {
      await api.memory.approve({ exec_id: execId, level });
      await refreshExecutions();
    } catch (err: unknown) {
      alert('Approve failed: ' + (err instanceof Error ? err.message : 'unknown error'));
    }
  };

  const handleReject = async (execId: number) => {
    try {
      await api.memory.reject({ exec_id: execId, feedback: rejectFeedback });
      setRejectFormId(null);
      setRejectFeedback('');
      await refreshExecutions();
    } catch (err: unknown) {
      alert('Reject failed: ' + (err instanceof Error ? err.message : 'unknown error'));
    }
  };

  const handleRevert = async (execId: number) => {
    if (!window.confirm('Revert this approval? This will remove the memory entry and make the execution approvable again.')) return;
    try {
      await api.memory.revert(execId);
      await refreshExecutions();
    } catch (err: unknown) {
      alert('Revert failed: ' + (err instanceof Error ? err.message : 'unknown error'));
    }
  };

  const handleSkip = async (execId: number) => {
    if (!window.confirm('Skip this execution? The task will be marked as cancelled.')) return;
    try {
      await api.memory.skip({ exec_id: execId });
      await refreshExecutions();
    } catch (err: unknown) {
      alert('Skip failed: ' + (err instanceof Error ? err.message : 'unknown error'));
    }
  };

  const handleCancel = async (execId: number) => {
    if (!window.confirm('Cancel this execution? The child process will be killed and the task returns to pending.')) return;
    try {
      await api.executions.cancel(execId);
      await refreshExecutions();
    } catch (err: unknown) {
      alert('Cancel failed: ' + (err instanceof Error ? err.message : 'unknown error'));
    }
  };

  const handleFork = async (execId: number) => {
    const topic = forkFeedback.trim();
    if (!topic) return;
    try {
      const result = await api.memory.fork({ exec_id: execId, topic });
      setForkFormId(null);
      setForkFeedback('');
      showToast(`Forked \u2014 new task #${result.new_task_id}, chat #${result.new_chat_id}`);
      await refreshExecutions();
    } catch (err: unknown) {
      alert('Fork failed: ' + (err instanceof Error ? err.message : 'unknown error'));
    }
  };

  const showToast = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(null), 4000);
  };

  const handleView = async (execId: number) => {
    const exec = executions.find((x) => x.id === execId);
    const title = `Output — ${exec?.task_title || 'Execution #' + execId}`;
    setOutputModal({ open: true, title, content: '', loading: true });
    try {
      const d = await api.executions.output(execId);
      setOutputModal({ open: true, title, content: d.content || '(empty output)', loading: false });
    } catch (err: unknown) {
      setOutputModal({
        open: true, title, loading: false,
        content: 'Failed to load output: ' + (err instanceof Error ? err.message : 'unknown'),
      });
    }
  };

  const handleError = (e: Execution) => {
    setErrorModal({
      open: true,
      title: `Error — ${e.task_title || 'Execution #' + e.id}`,
      content: e.error_message || 'No error details available.',
    });
  };

  const handleGitDiff = async (execId: number) => {
    try {
      const d = await api.executions.diff(execId);
      if ('error' in d && typeof (d as unknown as { error: string }).error === 'string') {
        alert((d as unknown as { error: string }).error);
        return;
      }
      const exec = executions.find((x) => x.id === execId);
      const title = exec
        ? `Git Diff — ${exec.task_title || 'Execution #' + execId}`
        : `Git Diff — Execution #${execId}`;
      setDiffModal({ open: true, title, content: d.diff || '', baseBranch: d.base_branch, taskBranch: d.task_branch });
    } catch (err: unknown) {
      alert('Failed to load diff: ' + (err instanceof Error ? err.message : 'unknown'));
    }
  };

  const newChatForTask = (taskId: number) => {
    const project = tasks.find((t) => t.id === taskId);
    if (project) {
      window.dispatchEvent(new CustomEvent('aingel:new-chat-task', { detail: { taskId } }));
    }
  };

  const thBase = "text-left px-2 py-1 text-soft font-medium relative dark:text-text-dark-soft";
  const tdBase = "px-2 py-[3px] border-b border-default/60 dark:border-border-dark-muted/60";

  return (
    <>
      <div
        className="execution-log border-t border-default flex-shrink-0 bg-surface-muted overflow-y-auto"
        ref={panelRef}
        style={{ height: clampedHeight, position: 'relative' }}
      >
        <div
          className="absolute -top-0.5 left-0 right-0 z-10 bg-border-strong dark:bg-border-dark-strong"
          style={{
            height: 4,
            cursor: 'ns-resize',
          }}
          onMouseDown={handleMouseDown}
        />
        <div className="log-header flex justify-between items-center px-4 py-1.5 text-sm+ text-soft border-b border-default dark:text-text-dark-soft dark:border-border-dark-muted">
          <h4 className="m-0 text-sm">Execution Log</h4>
          <span id="log-count">({filtered.length})</span>
          <span id="log-project-label" className="ml-2 text-sm+ opacity-70" />
        </div>
        <div className={cn('log-table', isMobile && 'hidden')}>
          <table className="w-full border-collapse text-sm+">
            <thead>
              <tr>
                <th className={thBase} style={{ width: colWidths[0] || DEFAULT_COL_WIDTHS[0] }}>Time<div onMouseDown={(e) => handleColResizeStart(e, 0)} className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize bg-transparent" /></th>
                <th className={cn(thBase, 'text-center')} style={{ width: colWidths[1] || DEFAULT_COL_WIDTHS[1] }}>Chat<div onMouseDown={(e) => handleColResizeStart(e, 1)} className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize bg-transparent" /></th>
                <th className={thBase} style={{ width: colWidths[2] || DEFAULT_COL_WIDTHS[2] }}>Task<div onMouseDown={(e) => handleColResizeStart(e, 2)} className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize bg-transparent" /></th>
                <th className={thBase} style={{ width: colWidths[3] || DEFAULT_COL_WIDTHS[3] }}>Instructions<div onMouseDown={(e) => handleColResizeStart(e, 3)} className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize bg-transparent" /></th>
                <th className={thBase} style={{ width: colWidths[4] || DEFAULT_COL_WIDTHS[4] }}>Model<div onMouseDown={(e) => handleColResizeStart(e, 4)} className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize bg-transparent" /></th>
                <th className={thBase} style={{ width: colWidths[5] || DEFAULT_COL_WIDTHS[5] }}>Status<div onMouseDown={(e) => handleColResizeStart(e, 5)} className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize bg-transparent" /></th>
                <th className={thBase} style={{ width: colWidths[6] || DEFAULT_COL_WIDTHS[6] }}>Cost<div onMouseDown={(e) => handleColResizeStart(e, 6)} className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize bg-transparent" /></th>
                <th className={thBase} style={{ width: colWidths[7] || DEFAULT_COL_WIDTHS[7] }}>File<div onMouseDown={(e) => handleColResizeStart(e, 7)} className="absolute right-0 top-0 bottom-0 w-[5px] cursor-col-resize bg-transparent" /></th>
                <th className={thBase} style={{ width: colWidths[8] || DEFAULT_COL_WIDTHS[8], background: '#eee7db' }}>Actions</th>
              </tr>
            </thead>
            <tbody id="exec-log-tbody">
              {!filtered.length ? (
                <tr>
                  <td colSpan={10} className="text-center p-3.5 text-faint">
                    No executions yet — confirm a task and run it.
                  </td>
                </tr>
              ) : (
                filtered.map((e) => {
                  const st = statusStyle(e.status, e.memory_status);
                  const modelColor = getModelColor(e.model);
                  const gitState = gitExecutionState(e);
                  const instructions = truncate(e.instructions || e.task_title || '', 80);
                  const diffstatFirstLine = (e.git_diffstat || '').split('\n')[0];
                  const memStatus = e.memory_status || 'pending';
                  const hasOutput = e.output_summary && !e.output_summary.startsWith('[STUB');
                  const hasError = e.error_message && e.error_message.length > 0;
                  const elapsedMins = e.status === 'running' ? elapsedMinutes(e.started_at) : 0;
                  const stuck = stuckReason(e);
                  return (
                    <tr key={e.id} id={`log-${e.id}`} className={cn('log-row', `status-${e.status}`)}>
                      <td className={tdBase}>
                        {fmtTimeFull(e.started_at)}
                      </td>
                      <td className={cn(tdBase, 'text-center')}>
                        {e.task_id ? (
                          <button
                            data-tip={`Open new chat for task #${e.task_id}`}
                            className="btn-xs"
                            style={{ color: '#7a5aa6', borderColor: 'rgba(122,90,166,.4)', background: 'rgba(122,90,166,.15)' }}
                            onClick={() => newChatForTask(e.task_id)}
                          >
                            Chat
                          </button>
                        ) : e.chat_id ? (
                          <button
                            data-tip="Open this execution's chat"
                            className="btn-xs"
                            style={{ color: '#7a5aa6', borderColor: 'rgba(122,90,166,.4)', background: 'rgba(122,90,166,.15)' }}
                            onClick={() => {
                              window.dispatchEvent(new CustomEvent('aingel:open-chat', { detail: { chatId: e.chat_id } }));
                            }}
                          >
                            Chat
                          </button>
                        ) : (
                          '\u2014'
                        )}
                      </td>
                      <td className={cn(tdBase, 'text-faint font-semibold')} style={{ fontVariantNumeric: 'tabular-nums' }}>
                        {e.task_id ? `#${e.task_id}` : '\u2014'}
                      </td>
                      <td className={tdBase} title={e.instructions || e.task_title || ''}>
                        {instructions}
                        {(e.git_commit || e.git_branch) ? (
                          <span
                            title={(e.git_diffstat || 'No code changes') + '\n(on branch ' + (e.git_branch || '') + ')\nClick to view full diff'}
                            className="font-mono text-2xs rounded-lg px-1.5 ml-1.5 cursor-pointer inline-flex items-center gap-1"
                            style={{ color: '#56633f', background: 'rgba(86,99,63,.12)' }}
                            onClick={(ev) => { ev.stopPropagation(); handleGitDiff(e.id); }}
                          >
                            <GitCommitHorizontal size={11} className="shrink-0" /> {e.git_commit || e.git_branch || ''}
                            {diffstatFirstLine ? (
                              <span className="text-faint max-w-[180px] overflow-hidden text-ellipsis whitespace-nowrap" style={{ fontSize: 8 }}>
                                {diffstatFirstLine}
                              </span>
                            ) : null}
                          </span>
                        ) : null}
                      </td>
                      <td className={tdBase}>
                        <span className="inline-flex items-center gap-1 flex-wrap">
                          <span className="rounded-[10px] text-xs" style={{ background: `${modelColor}22`, color: modelColor, padding: '1px 6px' }}>
                            {getModelLabel(e)}
                          </span>
                          <RagBadge exec={e} />
                          <FilesBadge exec={e} />
                        </span>
                      </td>
                      <td className={tdBase}>
                        <span className="inline-flex items-center gap-1 flex-wrap" style={{ color: st.color }}>
                          {e.status === 'done' ? <CircleCheck size={13} className="shrink-0 dark:text-status-done-dark" /> : e.status === 'failed' ? <X size={13} className="shrink-0 dark:text-status-failed-dark" /> : e.status === 'running' ? <LoaderCircle size={13} className="shrink-0 animate-spin dark:text-status-running-dark" /> : null}
                          <span className={cn('rounded-[10px] text-xs font-medium', e.status === 'running' && 'animate-pulse')} style={{
                            background: st.bg, color: st.color,
                            padding: '1px 6px',
                          }}>
                            {st.label}
                          </span>
                          {e.status === 'running' && (
                            <span className="text-xs font-mono text-faint" title={`Running for ${elapsedMins} min since ${fmtTimeFull(e.started_at)}`} style={{ fontVariantNumeric: 'tabular-nums' }}>
                              {elapsedMins}m
                            </span>
                          )}
                          {e.status === 'running' && (
                            <span className="text-xs font-mono text-faint" title={`Tokens: ${e.tokens_input || 0} input / ${e.tokens_output || 0} output`} style={{ fontVariantNumeric: 'tabular-nums' }}>
                              {fmtTokens(e.tokens_input)}↑{fmtTokens(e.tokens_output)}↓
                            </span>
                          )}
                          {stuck && (
                            <span
                              className="text-xs font-semibold rounded px-1.5 py-px"
                              style={{ background: '#fff2eb', color: '#b2622d', border: '1px solid #b2622d' }}
                              title={stuck}
                            >
                              ⚠ may be stuck
                            </span>
                          )}
                        </span>
                      </td>
                      <td className={cn(tdBase, 'text-status-done font-semibold')} style={{ fontVariantNumeric: 'tabular-nums' }}>
                        {fmtUsd(e.cost_usd)}
                      </td>
                      <td className={tdBase}>
                        <ExecFileCell exec={e} />
                      </td>
                      <td className={tdBase}>
                        {e.aingel_review ? (
                          <span
                            className="text-status-done cursor-pointer text-sm+ font-medium inline-flex items-center gap-1 dark:text-status-done-dark"
                            onClick={() => setReviewPopover(reviewPopover?.execId === e.id ? null : { execId: e.id, review: e.aingel_review! })}
                            title="Click to view review"
                          >
                            <CircleCheck size={13} className="shrink-0" /> Reviewed
                          </span>
                        ) : (
                          <span className="text-faint text-sm+">{'\u2014'}</span>
                        )}
                        {reviewPopover?.execId === e.id && (
                          <div
                            className="absolute bg-surface-raised border border-default rounded-md p-2 max-w-[300px] shadow-medium z-20 text-sm+ leading-[1.4] dark:bg-surface-dark-raised dark:border-border-dark-default"
                            onClick={(ev) => ev.stopPropagation()}
                          >
                            <div className="font-semibold mb-1 text-status-done inline-flex items-center gap-1 dark:text-status-done-dark"><Sparkles size={13} className="shrink-0" /> Guide review</div>
                            <div className="whitespace-pre-wrap">{reviewPopover.review}</div>
                            <button
                              data-tip="Close this review popover"
                              className="btn-xs mt-1"
                              onClick={() => setReviewPopover(null)}
                            >Close</button>
                          </div>
                        )}
                      </td>
                      <td className={tdBase}>
                        <div className="flex gap-[3px] flex-wrap items-center">
                          {e.status === 'running' && canEditExec(e.project_id) && (
                            <button
                              data-tip="Cancel execution and reset task to pending"
                              onClick={(ev) => { ev.stopPropagation(); handleCancel(e.id); }}
                              className="btn-xs text-danger border-danger"
                            >
                              Cancel
                            </button>
                          )}
                          {hasOutput ? (
                            <button data-tip="View execution output" className="btn-xs" onClick={() => handleView(e.id)}>View</button>
                          ) : hasError ? (
                            <button data-tip="View the error details" className="btn-xs text-danger border-danger" onClick={() => handleError(e)}>Error</button>
                          ) : null}
                          {e.status === 'done' && memStatus === 'pending' && gitState && gitState.kind !== 'branch' && hasOutput ? (
                            <span className="text-xs text-status-done inline-flex items-center gap-0.5 dark:text-status-done-dark" title={gitState.title}>
                              {gitState.kind === 'merged' ? <GitMerge size={11} className="shrink-0" /> : <GitCommitHorizontal size={11} className="shrink-0" />}
                              {gitState.label}
                            </span>
                          ) : e.status === 'done' && memStatus === 'pending' ? (
                            <>
                              {e.git_commit ? (
                                <div className="text-2xs w-full mb-0.5 inline-flex items-center gap-0.5" style={{ color: '#56633f' }}>
                                  <GitMerge size={10} className="shrink-0" /> approve = merge to main &middot; reject = discard
                                </div>
                              ) : null}
                              {canEditExec(e.project_id) && (
                                <>
                                  <button data-tip="Approve to project memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'project')}>
                                    Proj
                                  </button>
                                  <button data-tip="Approve to phase memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'phase')}>
                                    Phase
                                  </button>
                                  {e.session_id ? (
                                    <button data-tip="Approve to workflow memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'workflow')}>
                                      WF
                                    </button>
                                  ) : null}
                                  {rejectFormId === e.id ? (
                                    <span className="inline-flex gap-1 items-center">
                                      <input
                                        data-tip="Enter rejection feedback"
                                        type="text"
                                        placeholder="Feedback..."
                                        value={rejectFeedback}
                                        onChange={(ev) => setRejectFeedback(ev.target.value)}
                                        className="text-xs px-1 border border-default rounded-sm w-[120px]"
                                        style={{ padding: '1px 4px', borderWidth: 1 }}
                                      />
                                      <button data-tip="Submit rejection feedback" className="btn-xs text-danger" onClick={() => handleReject(e.id)}>
                                        Send
                                      </button>
                                      <button data-tip="Cancel rejection" className="btn-xs" onClick={() => { setRejectFormId(null); setRejectFeedback(''); }}>
                                        Cancel
                                      </button>
                                    </span>
                                  ) : (
                                    <button data-tip="Reject this execution" className="btn-xs text-danger" onClick={() => setRejectFormId(e.id)}>
                                      Reject
                                    </button>
                                  )}
                                  <button data-tip="Skip this execution" className="btn-xs text-faint" onClick={() => handleSkip(e.id)}>
                                    Skip
                                  </button>
                                </>
                              )}
                            </>
                          ) : e.status === 'done' && memStatus === 'rejected' && hasOutput ? (
                            <>
                              <span className="text-xs text-faint inline-flex items-center gap-0.5 dark:text-text-dark-faint"><RefreshCw size={11} className="shrink-0" /> re-queued</span>
                              {canEditExec(e.project_id) && (
                                <>
                                  <button data-tip="Approve to project memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'project')}>
                                    Proj
                                  </button>
                                  <button data-tip="Approve to phase memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'phase')}>
                                    Phase
                                  </button>
                                </>
                              )}
                            </>
                          ) : e.status === 'done' ? (
                            <>
                              <span className="text-xs inline-flex items-center gap-0.5">
                                {memStatus.startsWith('approved') ? (
                                  <><CircleCheck size={11} className="shrink-0 text-status-done dark:text-status-done-dark" /> {memStatus.split(':')[1] || 'saved'}</>
                                ) : memStatus === 'skipped' ? (
                                  <><CircleSlash size={11} className="shrink-0" /> skipped</>
                                ) : (
                                  <><RefreshCw size={11} className="shrink-0" /> re-queued</>
                                )}
                              </span>
                              {memStatus.startsWith('approved') && canEditExec(e.project_id) ? (
                                <button data-tip="Revert this approval" className="btn-xs text-faint" onClick={() => handleRevert(e.id)}>
                                  Revert
                                </button>
                              ) : null}
                            </>
                          ) : null}
                          {(e.status === 'failed' || e.status === 'rejected') && canEditExec(e.project_id) ? (
                            <>
                              {forkFormId === e.id ? (
                                <span className="inline-flex gap-1 items-center">
                                  <input
                                    data-tip="Enter fork topic or feedback"
                                    type="text"
                                    placeholder="Topic/feedback..."
                                    value={forkFeedback}
                                    onChange={(ev) => setForkFeedback(ev.target.value)}
                                    className="text-xs px-1 border border-default rounded-sm w-[120px]"
                                    style={{ padding: '1px 4px', borderWidth: 1 }}
                                  />
                                  <button data-tip="Fork into a new task" className="btn-xs" style={{ color: '#7a5aa6' }} onClick={() => handleFork(e.id)}>
                                    Fork
                                  </button>
                                  <button data-tip="Cancel fork" className="btn-xs" onClick={() => { setForkFormId(null); setForkFeedback(''); }}>
                                    Cancel
                                  </button>
                                </span>
                              ) : (
                                <button data-tip="Fork this execution" className="btn-xs" style={{ color: '#7a5aa6' }} onClick={() => setForkFormId(e.id)}>
                                  Fork
                                </button>
                              )}
                            </>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {isMobile && (
          <div className="log-cards flex flex-col gap-2 p-2">
            {!filtered.length ? (
              <div className="text-center p-3.5 text-faint">No executions yet — confirm a task and run it.</div>
            ) : (
              filtered.map((e) => {
                const st = statusStyle(e.status, e.memory_status);
                const modelColor = getModelColor(e.model);
                const gitState = gitExecutionState(e);
                const hasOutput = e.output_summary && !e.output_summary.startsWith('[STUB');
                const hasError = e.error_message && e.error_message.length > 0;
                const memStatus = e.memory_status || 'pending';
                const elapsedMins = e.status === 'running' ? elapsedMinutes(e.started_at) : 0;
                const stuck = stuckReason(e);
                return (
                  <div key={e.id} className="log-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-md p-2.5">
                    <div className="flex items-center gap-2 mb-1 flex-wrap">
                      <span className="inline-flex items-center gap-1" style={{ color: st.color }}>
                        {e.status === 'done' ? <CircleCheck size={13} /> : e.status === 'failed' ? <X size={13} /> : e.status === 'running' ? <LoaderCircle size={13} className="animate-spin" /> : null}
                        <span className="rounded-[10px] text-xs font-medium" style={{ background: st.bg, color: st.color, padding: '1px 6px' }}>{st.label}</span>
                        {e.status === 'running' && (
                          <span className="text-xs font-mono text-faint" title={`Running for ${elapsedMins} min`}>{elapsedMins}m</span>
                        )}
                        {e.status === 'running' && (
                          <span className="text-xs font-mono text-faint">{fmtTokens(e.tokens_input)}↑{fmtTokens(e.tokens_output)}↓</span>
                        )}
                        {stuck && (
                          <span className="text-xs font-semibold rounded px-1.5 py-px" style={{ background: '#fff2eb', color: '#b2622d', border: '1px solid #b2622d' }} title={stuck}>⚠ may be stuck</span>
                        )}
                      </span>
                      <span className="text-xs text-faint font-semibold" style={{ fontVariantNumeric: 'tabular-nums' }}>{e.task_id ? `#${e.task_id}` : '—'}</span>
                      <span className="flex-1" />
                      <span className="text-xs text-status-done font-semibold" style={{ fontVariantNumeric: 'tabular-nums' }}>{fmtUsd(e.cost_usd)}</span>
                    </div>
                    <div className="text-sm+ text-ink dark:text-text-dark-DEFAULT mb-1">{truncate(e.instructions || e.task_title || '', 120)}</div>
                    <div className="flex items-center gap-2 flex-wrap text-xs text-text-muted dark:text-text-dark-muted mb-1.5">
                      <span className="rounded-[10px]" style={{ background: `${modelColor}22`, color: modelColor, padding: '1px 6px' }}>{getModelLabel(e)}</span>
                      <RagBadge exec={e} />
                      <FilesBadge exec={e} />
                      <span>{fmtTimeFull(e.started_at)}</span>
                    </div>
                    <div className="flex gap-1.5 flex-wrap items-center">
                      {e.task_id ? (
                        <button data-tip={`Open new chat for task #${e.task_id}`} className="btn-xs" style={{ color: '#7a5aa6', borderColor: 'rgba(122,90,166,.4)', background: 'rgba(122,90,166,.15)' }} onClick={() => newChatForTask(e.task_id)}>Chat</button>
                      ) : e.chat_id ? (
                        <button data-tip="Open this execution's chat" className="btn-xs" style={{ color: '#7a5aa6', borderColor: 'rgba(122,90,166,.4)', background: 'rgba(122,90,166,.15)' }} onClick={() => window.dispatchEvent(new CustomEvent('aingel:open-chat', { detail: { chatId: e.chat_id } }))}>Chat</button>
                      ) : null}
                      {e.status === 'running' && canEditExec(e.project_id) && (
                        <button data-tip="Cancel execution and reset task to pending" onClick={(ev) => { ev.stopPropagation(); handleCancel(e.id); }} className="btn-xs text-danger border-danger">Cancel</button>
                      )}
                      {hasOutput ? (
                        <button data-tip="View execution output" className="btn-xs" onClick={() => handleView(e.id)}>View</button>
                      ) : hasError ? (
                        <button data-tip="View the error details" className="btn-xs text-danger border-danger" onClick={() => handleError(e)}>Error</button>
                      ) : null}
                      {e.status === 'done' && memStatus === 'pending' && gitState && gitState.kind !== 'branch' && hasOutput ? (
                        <span className="text-xs text-status-done inline-flex items-center gap-0.5">{gitState.kind === 'merged' ? <GitMerge size={11} /> : <GitCommitHorizontal size={11} />}{gitState.label}</span>
                      ) : e.status === 'done' && memStatus === 'pending' ? (
                        <>
                          {canEditExec(e.project_id) && (
                            <>
                              <button data-tip="Approve to project memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'project')}>Proj</button>
                              <button data-tip="Approve to phase memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'phase')}>Phase</button>
                              {e.session_id ? <button data-tip="Approve to workflow memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'workflow')}>WF</button> : null}
                              <button data-tip="Reject this execution" className="btn-xs text-danger" onClick={() => setRejectFormId(e.id)}>Reject</button>
                              <button data-tip="Skip this execution" className="btn-xs text-faint" onClick={() => handleSkip(e.id)}>Skip</button>
                            </>
                          )}
                        </>
                      ) : e.status === 'done' && memStatus === 'rejected' && hasOutput ? (
                        <>
                          <span className="text-xs text-faint inline-flex items-center gap-0.5"><RefreshCw size={11} /> re-queued</span>
                          {canEditExec(e.project_id) && (
                            <>
                              <button data-tip="Approve to project memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'project')}>Proj</button>
                              <button data-tip="Approve to phase memory" className="btn-xs text-status-done" onClick={() => handleApprove(e.id, 'phase')}>Phase</button>
                            </>
                          )}
                        </>
                      ) : e.status === 'done' ? (
                        <>
                          <span className="text-xs inline-flex items-center gap-0.5">
                            {memStatus.startsWith('approved') ? <><CircleCheck size={11} className="text-status-done" /> {memStatus.split(':')[1] || 'saved'}</> : memStatus === 'skipped' ? <><CircleSlash size={11} /> skipped</> : <><RefreshCw size={11} /> re-queued</>}
                          </span>
                          {memStatus.startsWith('approved') && canEditExec(e.project_id) ? <button data-tip="Revert this approval" className="btn-xs text-faint" onClick={() => handleRevert(e.id)}>Revert</button> : null}
                        </>
                      ) : null}
                      {(e.status === 'failed' || e.status === 'rejected') && canEditExec(e.project_id) ? (
                        <button data-tip="Fork this execution" className="btn-xs" style={{ color: '#7a5aa6' }} onClick={() => setForkFormId(e.id)}>Fork</button>
                      ) : null}
                    </div>
                    {rejectFormId === e.id && (
                      <div className="flex gap-1 items-center mt-1.5">
                        <input data-tip="Enter rejection feedback" type="text" placeholder="Feedback..." value={rejectFeedback} onChange={(ev) => setRejectFeedback(ev.target.value)} className="text-xs px-1 border border-default rounded-sm flex-1" style={{ padding: '1px 4px', borderWidth: 1 }} />
                        <button data-tip="Submit rejection feedback" className="btn-xs text-danger" onClick={() => handleReject(e.id)}>Send</button>
                        <button data-tip="Cancel rejection" className="btn-xs" onClick={() => { setRejectFormId(null); setRejectFeedback(''); }}>Cancel</button>
                      </div>
                    )}
                    {forkFormId === e.id && (
                      <div className="flex gap-1 items-center mt-1.5">
                        <input data-tip="Enter fork topic or feedback" type="text" placeholder="Topic/feedback..." value={forkFeedback} onChange={(ev) => setForkFeedback(ev.target.value)} className="text-xs px-1 border border-default rounded-sm flex-1" style={{ padding: '1px 4px', borderWidth: 1 }} />
                        <button data-tip="Fork into a new task" className="btn-xs" style={{ color: '#7a5aa6' }} onClick={() => handleFork(e.id)}>Fork</button>
                        <button data-tip="Cancel fork" className="btn-xs" onClick={() => { setForkFormId(null); setForkFeedback(''); }}>Cancel</button>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        )}
      </div>

      {diffModal.open && (
        <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-content justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setDiffModal({ open: false, title: '', content: '' }); }}>
          <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong" onClick={(e) => e.stopPropagation()} style={{ minWidth: 600 }}>
            <h3 className="m-0 mb-4 text-lg">{diffModal.title}</h3>
            {(diffModal.baseBranch || diffModal.taskBranch) ? (
              <div className="text-sm+ text-faint py-1 border-b border-default font-mono">
                {diffModal.baseBranch ? <span>base: {diffModal.baseBranch}  </span> : null}
                {diffModal.taskBranch ? <span>branch: {diffModal.taskBranch}</span> : null}
              </div>
            ) : null}
            {diffModal.truncated ? (
              <div className="text-status-pending text-xs py-1 border-b border-default">
                Diff truncated
              </div>
            ) : null}
            <pre className="whitespace-pre-wrap text-sm+ leading-[1.5] font-mono p-2 rounded m-0 max-h-[60vh] overflow-auto bg-surface-muted dark:bg-surface-dark-muted">
              {diffModal.content || 'No diff available.'}
            </pre>
            <div className="modal-actions flex justify-end gap-2 mt-4">
              <button data-tip="Close diff view" className="btn" onClick={() => setDiffModal({ open: false, title: '', content: '' })}>Close</button>
            </div>
          </div>
        </div>
      )}

      {errorModal.open && (
        <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setErrorModal({ open: false, title: '', content: '' }); }}>
          <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong" onClick={(e) => e.stopPropagation()}>
            <h3 className="m-0 mb-4 text-lg">{errorModal.title}</h3>
            <pre className="whitespace-pre-wrap text-sm text-status-failed font-mono p-2 rounded max-h-[60vh] overflow-auto bg-status-failed-bg dark:bg-status-failed-dark-bg dark:text-status-failed-dark">
              {errorModal.content}
            </pre>
            <div className="modal-actions flex justify-end gap-2 mt-4">
              <button data-tip="Close error details" className="btn" onClick={() => setErrorModal({ open: false, title: '', content: '' })}>Close</button>
            </div>
          </div>
        </div>
      )}

      {outputModal.open && (
        <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setOutputModal({ open: false, title: '', content: '', loading: false }); }}>
          <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] w-[900px] max-w-[92vw] max-h-[88vh] flex flex-col shadow-strong" onClick={(e) => e.stopPropagation()}>
            <h3 className="m-0 mb-3 text-lg">{outputModal.title}</h3>
            {outputModal.loading ? (
              <div className="text-text-faint text-sm py-6">Loading…</div>
            ) : (
              <pre className="whitespace-pre-wrap break-words text-sm font-mono p-3 rounded flex-1 overflow-auto bg-surface-muted dark:bg-surface-dark-muted dark:text-text-dark-soft">
                {outputModal.content}
              </pre>
            )}
            <div className="modal-actions flex justify-end gap-2 mt-4">
              <button
                data-tip="Copy output to clipboard"
                className="btn"
                onClick={() => { navigator.clipboard?.writeText(outputModal.content).catch(() => {}); }}
                disabled={outputModal.loading}
              >
                Copy
              </button>
              <button data-tip="Close output view" className="btn" onClick={() => setOutputModal({ open: false, title: '', content: '', loading: false })}>Close</button>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className="fixed bottom-5 right-5 z-[9999] text-sm font-mono py-2.5 px-4 rounded-lg shadow-medium" style={{ background: '#56633f', color: '#fff' }}>
          {toast}
        </div>
      )}
    </>
  );
};

// ── File cell ─────────────────────────────────────────────────────────────

/**
 * Expand a git diff --stat rename entry.
 * Git abbreviates renames as `old => new` and, when prefix/suffix are shared,
 * as `prefix/{old => new}/suffix` (e.g. `{DU202433000-sig.md => DPP-WOPN.717.10.2024.AC.md}`
 * or `Working Docs/{old => new}`). Returns expanded paths; for non-rename
 * returns [raw]. Caller should link the NEW name (last element).
 */
const expandDiffstatRename = (raw: string): string[] => {
  const s = (raw || '').trim().replace(/^\.{3}\//, '').replace(/\.\.\.\//g, '');
  if (!s) return [];
  if (!s.includes(' => ')) return [s];
  const open = s.indexOf('{');
  const close = open !== -1 ? s.indexOf('}', open) : -1;
  if (open !== -1 && close !== -1) {
    const inside = s.slice(open + 1, close);
    if (inside.includes(' => ')) {
      const prefix = s.slice(0, open);
      const suffix = s.slice(close + 1);
      const [oldPart, newPart] = inside.split(' => ');
      const oldP = (prefix + (oldPart || '').trim() + suffix).trim().replace(/\.\.\.\//g, '');
      const newP = (prefix + (newPart || '').trim() + suffix).trim().replace(/\.\.\.\//g, '');
      const out: string[] = [];
      if (oldP) out.push(oldP);
      if (newP) out.push(newP);
      return out.length ? out : [s];
    }
  }
  const [oldP, newP] = s.split(' => ');
  const out: string[] = [];
  const o = (oldP || '').trim().replace(/\.\.\.\//g, '');
  const n = (newP || '').trim().replace(/\.\.\.\//g, '');
  if (o) out.push(o);
  if (n) out.push(n);
  return out.length ? out : [s];
};

const parseDiffstatFiles = (diffstat: string | undefined): string[] => {
  const out: string[] = [];
  if (!diffstat) return out;
  // Definition / bookkeeping files that are not task deliverables — even if
  // git tracks them (GUIDE.md is regenerated from DB), they should not appear
  // in the Exec Log "Files" column. Case-insensitive and path-agnostic.
  const EXCLUDED_BASENAMES = new Set([
    'aingel.json',
    'guide.md',
    'readmefirst.md',
    'claude.md',
    'skills.md',
    'tasks.md',
  ]);
  for (const line of diffstat.split('\n')) {
    const m = line.match(/^\s*(?:\.{3}\/?)?(.+?)\s+\|/);
    if (!m || !m[1]) continue;
    let fname = m[1].trim().replace(/^\.{3}\//, '');
    if (!fname) continue;
    // Handle rename entries: split brace expansion and ` => `, link NEW file.
    const expanded = expandDiffstatRename(fname);
    // Use the NEW name (last element) as the file URL; old is kept out of the
    // File column to avoid 404s for moved files (helper still returns both for
    // backend attribution).
    fname = expanded.length ? expanded[expanded.length - 1] : fname;
    fname = fname.replace(/^\.{3}\//, '').replace(/\.\.\.\//g, '').trim();
    if (!fname) continue;
    // Skip if the raw brace pattern leaked through (safety)
    if (fname.includes('{') || fname.includes(' => ')) continue;
    const base = fname.split('/').pop()?.toLowerCase() ?? '';
    if (EXCLUDED_BASENAMES.has(base)) continue;
    // Also exclude exact path match for definition files at root (covers case variants)
    const lower = fname.toLowerCase();
    if (lower === 'guide.md' || lower === 'readmefirst.md' || lower === 'claude.md' || lower === 'skills.md' || lower === 'tasks.md' || lower === 'aingel.json') continue;
    if (fname === 'aingel.json' || fname.startsWith('.vibe/') || fname.startsWith('.claude/')) continue;
    // Exec-output bookkeeping files are linked separately via hasOutput — skip
    // only those, NOT every Artifacts/ path: task deliverables created in the
    // Lane B output folder (Artifacts/outputs/<task>-<slug>/) must stay visible
    // (task 10001171: 'This is a test' was invisible in the File column).
    if (/^exec-\d+-output\.md$/i.test(base)) continue;
    if (!out.includes(fname)) out.push(fname);
  }
  return out;
};

const ExecFileCell = ({ exec }: { exec: Execution }) => {
  const { activeProject } = useStore();
  const files = useMemo(() => parseDiffstatFiles(exec.git_diffstat), [exec.git_diffstat]);
  const hasOutput = exec.has_output_file;
  const totalCount = files.length + (hasOutput ? 1 : 0);
  const [expanded, setExpanded] = useState(false);
  if (totalCount === 0) return <span className="text-faint">{'\u2014'}</span>;
  const visible = expanded ? files : files.slice(0, 2);
  const hidden = files.length - visible.length;
  return (
    <div className="flex flex-col gap-0.5">
      {visible.map((f) => {
        const url = relUrl(f, activeProject);
        const basename = f.split('/').pop() || f;
        if (!url) return <span key={f} className="text-xs text-faint truncate" title={f}>{basename}</span>;
        return (
          <a key={f} href={url} target="_blank" rel="noopener noreferrer"
             className="text-xs text-link hover:underline truncate" title={f}
             style={{ maxWidth: 110 }}>
            {basename}
          </a>
        );
      })}
      {hasOutput ? (
        <a
          href={`/files/${activeProject}/${encodeURIComponent('Artifacts')}/${encodeURIComponent('outputs')}/exec-${exec.id}-output.md`}
          target="_blank" rel="noopener noreferrer"
          className="text-xs text-link hover:underline truncate"
          title={`Output file (exec #${exec.id}, task #${exec.task_id})`}
          style={{ maxWidth: 110 }}
        >
          <GitBranch size={11} className="inline-block align-[-2px] mr-0.5" /> output-{exec.task_id}.md
        </a>
      ) : null}
      {(hidden > 0 || (files.length > 2 && expanded)) ? (
        <button
          data-tip={expanded ? 'Collapse files' : `Show all ${files.length} files`}
          onClick={() => setExpanded((e) => !e)}
          className="text-xs text-link hover:underline text-left inline-flex items-center gap-0.5 border-none bg-transparent p-0"
        >
          {expanded ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
          {expanded ? 'less' : `+${hidden} more`}
        </button>
      ) : null}
    </div>
  );
};

export default ExecutionLog;