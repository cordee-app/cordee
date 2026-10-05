import { useState, useRef, useEffect } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { Task } from '../types';

interface Props {
  task: Task;
  /** Whether the card is in read-only state (running/done/failed). Some actions still apply. */
  isArchived?: boolean;
  /** Task is held by an AIngel gate (H2a review/questions pending) — block edit/archive/delete. */
  held?: boolean;
}

/**
 * Overflow "⋯" menu for a task card. Surfaces power-user actions:
 *   Edit, Move to phase, Duplicate, Redo (reset to pending), Archive, Delete.
 *
 * Replaces the inline Edit/Archive buttons on TaskCard. Keeps the card compact
 * while grouping rare-but-destructive actions behind a single button.
 */
export const TaskOverflowMenu = ({ task, isArchived = false, held = false }: Props) => {
  const perms = useProjectPermissions(task.project_id);
  const [open, setOpen] = useState(false);
  const [phaseSubOpen, setPhaseSubOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [depCount, setDepCount] = useState<number | null>(null);
  const [showDup, setShowDup] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [showRedo, setShowRedo] = useState(false);
  const [newPhasePrompt, setNewPhasePrompt] = useState(false);
  const [newPhaseName, setNewPhaseName] = useState('');

  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const setTasks = useStore((s) => s.setTasks);
  const setShowAddModal = useStore((s) => s.setShowAddModal);
  const setModModalTaskId = useStore((s) => s.setModModalTaskId);
  const projects = useStore((s) => s.projects);
  const phasesData = useStore((s) => s.phasesData) as unknown as { project: string; path?: string; phases?: { title: string }[] }[];

  const isDone = task.status === 'done';
  const isFailed = task.status === 'failed';
  const isSkip = task.status === 'skip';
  // When `held` (AIngel gate), block every mutating action — the user must
  // answer the review questions or override before editing the task.
  const canArchive = !isArchived && !held && (isDone || isFailed || isSkip);
  const canRedo = !held && (isDone || isFailed || isSkip);
  const canEdit = !isArchived && !held;

  // Phases available for this task's project. `get_all_phases` returns
  // {project: <name>, phases: [{title}]} — there is no project_id field, so
  // we resolve the project name from the store's projects list.
  const projectName = projects.find((p) => p.id === task.project_id)?.name;
  const projectPhases: string[] = (() => {
    const entry = phasesData.find((p) => p.project === projectName);
    return entry?.phases?.map((ph) => ph.title) ?? [];
  })();

  // Close on outside click / Esc.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node) &&
          btnRef.current && !btnRef.current.contains(e.target as Node)) {
        setOpen(false);
        setPhaseSubOpen(false);
        setNewPhasePrompt(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        setPhaseSubOpen(false);
        setNewPhasePrompt(false);
        setShowDup(false);
        setShowDelete(false);
        setDepCount(null);
        setShowRedo(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // Fetch the dependent count each time the delete confirm opens. Fetching once
  // per mount let the dialog omit the "N tasks depend on this" warning after a
  // dependency was added — on the one confirmation that needs it to be right.
  useEffect(() => {
    if (!showDelete) return;
    let cancelled = false;
    api.tasks.dependencies.list(task.id)
      .then((d) => { if (!cancelled) setDepCount(d.depended_on_by?.length || 0); })
      .catch(() => { if (!cancelled) setDepCount(0); });
    return () => { cancelled = true; };
  }, [showDelete, task.id]);

  const stop = (e: React.MouseEvent) => { e.stopPropagation(); };

  // Clear the count on close so a stale one never flashes on the next open.
  const openDelete = () => { setDepCount(null); setShowDelete(true); };
  const closeDelete = () => { setShowDelete(false); setDepCount(null); };

  const closeAll = () => {
    setOpen(false);
    setPhaseSubOpen(false);
    setNewPhasePrompt(false);
  };

  const handleEdit = (e: React.MouseEvent) => {
    stop(e);
    closeAll();
    setShowAddModal(true);
    setModModalTaskId(task.id);
  };

  const handleClearHf = async (e: React.MouseEvent) => {
    stop(e);
    closeAll();
    setBusy(true);
    try {
      await api.tasks.update(task.id, { hf_repo_id: '', awaiting_model: 0 });
      setTasks((prev) => prev.map((t) =>
        t.id === task.id ? { ...t, hf_repo_id: '', awaiting_model: 0 } : t
      ));
    } catch (err) {
      console.error('Clear HF model failed:', err);
    } finally {
      setBusy(false);
    }
  };

  const handleMovePhase = async (phaseName: string) => {
    closeAll();
    setBusy(true);
    try {
      await api.tasks.update(task.id, { phase_name: phaseName });
      setTasks((prev) => prev.map((t) =>
        t.id === task.id ? { ...t, phase_name: phaseName } : t
      ));
    } catch (err) {
      console.error('Move phase failed:', err);
    } finally {
      setBusy(false);
    }
  };

  const handleArchive = async (e: React.MouseEvent) => {
    stop(e);
    closeAll();
    if (!window.confirm(`Archive task #${task.id} "${task.title}"?`)) return;
    setBusy(true);
    try {
      await api.tasks.archive(task.id);
      setTasks((prev) => prev.map((t) => (t.id === task.id ? { ...t, archived: 1 } : t)));
    } catch (err) {
      console.error('Archive failed:', err);
    } finally {
      setBusy(false);
    }
  };

  const handleRedoConfirm = async () => {
    setShowRedo(false);
    setBusy(true);
    try {
      const updated = await api.tasks.redo(task.id);
      if (updated && typeof updated === 'object' && 'id' in updated) {
        setTasks((prev) => prev.map((t) =>
          t.id === task.id ? { ...t, ...updated } : t
        ));
      } else {
        // Fallback: optimistically reset known fields.
        setTasks((prev) => prev.map((t) =>
          t.id === task.id
            ? { ...t, status: 'pending', work_session_slot: undefined, actual_cost: undefined }
            : t
        ));
      }
    } catch (err) {
      console.error('Redo failed:', err);
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteConfirm = async () => {
    closeDelete();
    setBusy(true);
    try {
      await api.tasks.delete(task.id);
      setTasks((prev) => prev.filter((t) => t.id !== task.id));
    } catch (err) {
      console.error('Delete failed:', err);
    } finally {
      setBusy(false);
    }
  };

  // Duplicate modal state
  const [dupTitle, setDupTitle] = useState('');
  const [dupCopyDeps, setDupCopyDeps] = useState(false);

  const openDup = (e: React.MouseEvent) => {
    stop(e);
    closeAll();
    setDupTitle(`${task.title} (copy)`);
    setDupCopyDeps(false);
    setShowDup(true);
  };

  const handleDupConfirm = async () => {
    setShowDup(false);
    setBusy(true);
    try {
      const created = await api.tasks.copy(task.id, {
        title: dupTitle.trim() || undefined,
        copy_deps: dupCopyDeps,
      });
      if (created && typeof created === 'object' && 'id' in created) {
        // Insert the new task into the store list.
        setTasks((prev) => [...prev, created as Task]);
      } else {
        // Fallback: full reload via the api.
        const all = await api.tasks.list();
        setTasks(all);
      }
    } catch (err) {
      console.error('Duplicate failed:', err);
    } finally {
      setBusy(false);
    }
  };

  const submitNewPhase = (e: React.MouseEvent) => {
    stop(e);
    const name = newPhaseName.trim();
    if (!name) return;
    handleMovePhase(name);
    setNewPhaseName('');
  };

  const menuBtnStyle: React.CSSProperties = {
    background: 'transparent',
    border: '1px solid transparent',
    color: '#666',
    padding: '0 6px',
    borderRadius: 4,
    cursor: 'pointer',
    fontSize: 14,
    lineHeight: '20px',
    fontWeight: 700,
  };

  const itemStyle: React.CSSProperties = {
    display: 'block',
    width: '100%',
    textAlign: 'left',
    background: 'transparent',
    border: 'none',
    padding: '6px 14px',
    fontSize: 13,
    color: '#333',
    cursor: 'pointer',
  };

  const dangerStyle: React.CSSProperties = { ...itemStyle, color: '#c62828' };

  const overlayStyle: React.CSSProperties = {
    position: 'fixed',
    inset: 0,
    background: 'rgba(0,0,0,0.25)',
    zIndex: 50,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  };

  const modalStyle: React.CSSProperties = {
    background: 'white',
    borderRadius: 8,
    padding: 18,
    minWidth: 340,
    maxWidth: 460,
    boxShadow: '0 10px 40px rgba(0,0,0,0.25)',
    zIndex: 51,
  };

  return (
    <>
      {perms.canEdit && (
        <button
          ref={btnRef}
          data-tip="Task actions"
          className="task-overflow-btn"
          style={menuBtnStyle}
          onClick={(e) => { stop(e); setOpen((v) => !v); }}
          disabled={busy}
          aria-haspopup="menu"
          aria-expanded={open}
        >
          {busy ? '\u2026' : '\u22EF'}
        </button>
      )}

      {open && (
        <div
          ref={menuRef}
          role="menu"
          onClick={stop}
          style={{
            position: 'absolute',
            top: '100%',
            right: 0,
            marginTop: 4,
            minWidth: 200,
            background: 'white',
            border: '1px solid #ddd',
            borderRadius: 6,
            boxShadow: '0 6px 24px rgba(0,0,0,0.15)',
            zIndex: 40,
            padding: '4px 0',
          }}
        >
          {canEdit && (
            <button data-tip="Edit this task" style={itemStyle} onMouseEnter={() => setPhaseSubOpen(false)} onClick={handleEdit}>
              Edit{'\u2026'}
            </button>
          )}

          {canEdit && task.awaiting_model && task.hf_repo_id && (
            <button data-tip="Revert to the catalogue model" style={itemStyle} onClick={handleClearHf}>
              Clear HF model
            </button>
          )}

          {!isArchived && !held && (
            <div>
              <button
                data-tip="Move this task to another phase"
                style={itemStyle}
                onClick={(e) => { stop(e); setPhaseSubOpen((v) => !v); }}
              >
                Move to phase {phaseSubOpen ? '\u25BC' : '\u25B6'}
              </button>
              {phaseSubOpen && (
                <div style={{ padding: '2px 0', background: '#fafafa' }}>
                  {projectPhases.length === 0 && (
                    <div style={{ padding: '6px 14px', fontSize: 12, color: '#999' }}>
                      No phases defined
                    </div>
                  )}
                  {projectPhases.map((ph) => (
                    <button
                      key={ph}
                      data-tip={`Move to phase ${ph}`}
                      style={{
                        ...itemStyle,
                        paddingLeft: 24,
                        fontWeight: ph === task.phase_name ? 700 : 400,
                        color: ph === task.phase_name ? '#1565c0' : '#333',
                      }}
                      onClick={(e) => { stop(e); handleMovePhase(ph); }}
                    >
                      {ph === task.phase_name ? '\u2713' : '\u00A0\u00A0'} {ph}
                    </button>
                  ))}
                  <div style={{ borderTop: '1px solid #eee', margin: '4px 0' }} />
                  {!newPhasePrompt ? (
                    <button
                      data-tip="Create a new phase for this task"
                      style={{ ...itemStyle, paddingLeft: 24 }}
                      onClick={(e) => { stop(e); setNewPhasePrompt(true); }}
                    >
                      + New phase{'\u2026'}
                    </button>
                  ) : (
                    <div style={{ padding: '6px 14px' }} onClick={stop}>
                      <input
                        autoFocus
                        type="text"
                        data-tip="Type the new phase name"
                        value={newPhaseName}
                        onChange={(e) => setNewPhaseName(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter') submitNewPhase(e as unknown as React.MouseEvent); }}
                        placeholder="Phase name"
                        style={{
                          width: '100%',
                          padding: '4px 6px',
                          border: '1px solid #ccc',
                          borderRadius: 4,
                          fontSize: 12,
                        }}
                      />
                      <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                        <button
                          data-tip="Confirm the new phase name"
                          style={{ ...itemStyle, padding: '3px 8px', fontSize: 12, border: '1px solid #ddd', borderRadius: 4 }}
                          onClick={submitNewPhase}
                        >
                          OK
                        </button>
                        <button
                          data-tip="Cancel the new phase"
                          style={{ ...itemStyle, padding: '3px 8px', fontSize: 12, border: '1px solid #ddd', borderRadius: 4 }}
                          onClick={(e) => { stop(e); setNewPhasePrompt(false); setNewPhaseName(''); }}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {!held && (
            <button data-tip="Duplicate this task" style={itemStyle} onClick={openDup}>
              Duplicate{'\u2026'}
            </button>
          )}

          {canRedo && (
            <button data-tip="Reset this task to pending" style={itemStyle} onClick={(e) => { stop(e); closeAll(); setShowRedo(true); }}>
              Redo (reset to pending){'\u2026'}
            </button>
          )}

          {canArchive && (
            <button data-tip="Archive this task" style={itemStyle} onClick={handleArchive}>
              Archive
            </button>
          )}

          <div style={{ borderTop: '1px solid #eee', margin: '4px 0' }} />

          {!held && (
            <button data-tip="Delete this task permanently" style={dangerStyle} onClick={(e) => { stop(e); closeAll(); openDelete(); }}>
              Delete task{'\u2026'}
            </button>
          )}
          {held && (
            <div style={{ padding: '6px 14px', fontSize: 12, color: '#999', maxWidth: 200 }}>
              Answer the Guide review questions (or override) before editing this task.
            </div>
          )}
        </div>
      )}

      {/* Redo confirm */}
      {showRedo && (
        <div style={overlayStyle} onClick={(e) => { if (e.target === e.currentTarget) setShowRedo(false); }}>
          <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>
              Redo task #{task.id}?
            </div>
            <div style={{ fontSize: 13, color: '#555', marginBottom: 14, lineHeight: 1.5 }}>
              This resets the task to <strong>pending</strong>. Actual cost, gate reports,
              handoff context and slot assignment will be cleared. Past executions remain
              in the log (history preserved).<br />
              <span style={{ color: '#e65100' }}>
                Dependent tasks {'—'} including ones already completed {'—'} will be
                flagged stale so you can re-run them. Skipped tasks are left alone.
              </span>
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                data-tip="Cancel and close without resetting"
                style={{ padding: '6px 14px', border: '1px solid #ddd', borderRadius: 4, cursor: 'pointer', fontSize: 13 }}
                onClick={() => setShowRedo(false)}
              >
                Cancel
              </button>
              <button
                data-tip="Reset this task to pending"
                style={{ padding: '6px 14px', border: '1px solid #2e7d32', borderRadius: 4, cursor: 'pointer', fontSize: 13, background: '#2e7d32', color: 'white' }}
                onClick={handleRedoConfirm}
              >
                Reset to pending
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete confirm */}
      {showDelete && (
        <div style={overlayStyle} onClick={(e) => { if (e.target === e.currentTarget) closeDelete(); }}>
          <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8, color: '#c62828' }}>
              Delete task #{task.id}?
            </div>
            <div style={{ fontSize: 13, color: '#555', marginBottom: 14, lineHeight: 1.5 }}>
              This <strong>permanently removes</strong> the task.
              {' '}Past executions are unlinked but kept in the execution log.
              {depCount !== null && depCount > 0 && (
                <>
                  {' '}
                  <strong style={{ color: '#c62828' }}>
                    {depCount} task{depCount === 1 ? '' : 's'}
                  </strong>{' '}
                  depend on this task and will lose this prerequisite.
                </>
              )}
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                data-tip="Cancel and keep this task"
                style={{ padding: '6px 14px', border: '1px solid #ddd', borderRadius: 4, cursor: 'pointer', fontSize: 13 }}
                onClick={closeDelete}
              >
                Cancel
              </button>
              <button
                data-tip="Delete this task permanently"
                style={{ padding: '6px 14px', border: '1px solid #c62828', borderRadius: 4, cursor: 'pointer', fontSize: 13, background: '#c62828', color: 'white' }}
                onClick={handleDeleteConfirm}
              >
                Delete permanently
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Duplicate modal */}
      {showDup && (
        <div style={overlayStyle} onClick={(e) => { if (e.target === e.currentTarget) setShowDup(false); }}>
          <div style={modalStyle} onClick={(e) => e.stopPropagation()}>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>
              Duplicate task #{task.id}
            </div>
            <label style={{ display: 'block', fontSize: 12, fontWeight: 600, color: '#555', marginBottom: 4 }}>
              New title
            </label>
            <input
              type="text"
              data-tip="Title for the duplicated task"
              value={dupTitle}
              onChange={(e) => setDupTitle(e.target.value)}
              style={{
                width: '100%',
                padding: '6px 8px',
                border: '1px solid #ccc',
                borderRadius: 4,
                fontSize: 13,
                marginBottom: 12,
              }}
              autoFocus
            />
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13, color: '#333', marginBottom: 14, cursor: 'pointer' }}>
              <input
                type="checkbox"
                data-tip="Copy this task's dependencies to the copy"
                checked={dupCopyDeps}
                onChange={(e) => setDupCopyDeps(e.target.checked)}
              />
              Copy dependencies ({'what this task depends on'})
            </label>
            <div style={{ fontSize: 12, color: '#888', marginBottom: 14, lineHeight: 1.4 }}>
              The new task is created as <strong>pending</strong> with no slot, no
              executions, and a fresh gate state. Description, model, phase,
              priority and estimates are copied.
            </div>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                data-tip="Cancel duplication"
                style={{ padding: '6px 14px', border: '1px solid #ddd', borderRadius: 4, cursor: 'pointer', fontSize: 13 }}
                onClick={() => setShowDup(false)}
              >
                Cancel
              </button>
              <button
                data-tip="Create the duplicate task"
                style={{ padding: '6px 14px', border: '1px solid #1565c0', borderRadius: 4, cursor: 'pointer', fontSize: 13, background: '#1565c0', color: 'white' }}
                onClick={handleDupConfirm}
              >
                Create copy
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
};

export default TaskOverflowMenu;