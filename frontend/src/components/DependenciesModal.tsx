import { useState, useEffect, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { cn } from '../utils/cn';
import { taskChip, isTaskCode } from '../utils/taskCode';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import { addDependencyWithGate } from '../utils/deps';
import { useUnmetDepsConfirm, UnmetDepsConfirmDialog } from './UnmetDepsConfirm';
import type { Dependency, TaskDependencies } from '../types';

export const DependenciesModal = () => {
  const depsModalTaskId = useStore((s) => s.depsModalTaskId);
  const setDepsModalTaskId = useStore((s) => s.setDepsModalTaskId);
  const tasks = useStore((s) => s.tasks);
  const setTasks = useStore((s) => s.setTasks);
  const refreshDepEdges = useStore((s) => s.refreshDepEdges);

  // Dependency-gate confirm (not-done deps block the task from running).
  const unmetConfirm = useUnmetDepsConfirm();

  const [deps, setDeps] = useState<TaskDependencies | null>(null);
  const [addInput, setAddInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [addError, setAddError] = useState('');

  const task =
    depsModalTaskId !== null ? tasks.find((t) => t.id === depsModalTaskId) : undefined;

  const perms = useProjectPermissions(task?.project_id);

  const siblingTasks = (task?.project_id != null ? tasks : []).filter(
    (t) =>
      t.project_id === task?.project_id &&
      t.id !== task?.id &&
      (t.status as string) !== 'archived' &&
      (t.status as string) !== 'skip',
  );

  const loadDeps = useCallback(async () => {
    if (depsModalTaskId === null) return;
    setLoading(true);
    try {
      const result = await api.tasks.dependencies.list(depsModalTaskId);
      setDeps(result);
    } catch {
      setDeps(null);
    } finally {
      setLoading(false);
    }
  }, [depsModalTaskId]);

  useEffect(() => {
    if (depsModalTaskId !== null) {
      loadDeps();
      setAddInput('');
      setAddError('');
    } else {
      setDeps(null);
    }
  }, [depsModalTaskId, loadDeps]);

  const handleRemoveDep = async (depId: number) => {
    if (depsModalTaskId === null) return;
    try {
      await api.tasks.dependencies.remove(depsModalTaskId, depId);
      await loadDeps();
      const refreshed = await api.tasks.list();
      setTasks(refreshed);
      refreshDepEdges();
    } catch (e: unknown) {
      alert('Error: ' + (e as Error).message);
    }
  };

  // Shared gate for both add paths: pre-confirm when the dep's status is
  // known from the store (quick-picks always are; code-resolved deps may be
  // cross-project and missing), then let addDependencyWithGate handle the
  // 409 dependency_not_done backstop (confirm → retry with acknowledged).
  const addDepWithGate = async (inputId: number) => {
    if (depsModalTaskId === null) return;
    const depTask = tasks.find((t) => t.id === inputId);
    const notDone = Boolean(depTask && depTask.status !== 'done');
    if (notDone && depTask) {
      const ok = await unmetConfirm.confirm(
        [{
          id: depTask.id,
          display_code: depTask.display_code ?? undefined,
          title: depTask.title,
          status: depTask.status,
        }],
        'add',
      );
      if (!ok) return;
    }
    const res = await addDependencyWithGate(
      depsModalTaskId,
      inputId,
      notDone,
      (unmet) => unmetConfirm.confirm(unmet, 'add'),
    );
    if (!res.ok) {
      // The not-done case was confirmed (or declined) inside the gate —
      // surface only real errors; declines stay silent.
      if (res.error !== undefined) {
        setAddError(res.error instanceof Error ? res.error.message : 'Failed to add dependency');
      }
      return;
    }
    setAddInput('');
    await loadDeps();
    const refreshed = await api.tasks.list();
    setTasks(refreshed);
    refreshDepEdges();
  };

  const handleAddDep = async () => {
    const raw = addInput.trim();
    if (depsModalTaskId === null) return;
    let inputId: number | null = null;
    if (isTaskCode(raw)) {
      try {
        const resolved = await api.tasks.resolveTaskCode(raw);
        inputId = resolved.task_id;
      } catch {
        setAddError('Task code not found');
        return;
      }
    } else {
      const parsed = Number(raw);
      if (!parsed || !Number.isInteger(parsed)) {
        setAddError('Enter a valid task ID');
        return;
      }
      inputId = parsed;
    }
    if (inputId === depsModalTaskId) {
      setAddError('A task cannot depend on itself');
      return;
    }
    setAddError('');
    await addDepWithGate(inputId);
  };

  const handleQuickPick = async (id: number) => {
    if (depsModalTaskId === null) return;
    setAddError('');
    await addDepWithGate(id);
  };

  const closeModal = () => {
    unmetConfirm.settle(false); // never leave an awaited gate confirm hanging
    setDepsModalTaskId(null);
  };

  if (depsModalTaskId === null || !task) return null;

  const renderDepRow = (dep: Dependency) => {
    const depId = dep.id;
    const depTask = tasks.find((t) => t.id === depId) ?? dep;
    return (
      <div
        key={depId}
        className="flex items-center justify-between py-1.5 px-2 bg-surface-subtle rounded mb-1"
      >
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm+ text-text-faint">
            {taskChip(depTask)}
          </span>
          <span className="text-sm">
            {depTask?.title ?? `Task ${taskChip(depTask)}`}
          </span>
          {depTask && (
            <span
              className={cn(
                'text-2xs py-px px-1 rounded-sm',
                depTask.status === 'done'
                  ? 'bg-status-done-bg text-status-done'
                  : 'bg-status-pending-bg text-status-pending',
              )}
            >
              {depTask.status}
            </span>
          )}
        </div>
        {perms.canEdit && (
          <button
            data-tip="Remove this dependency"
            className="btn py-0.5 px-2 text-xs border border-default rounded cursor-pointer"
            onClick={() => handleRemoveDep(depId)}
          >
            Remove
          </button>
        )}
      </div>
    );
  };

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={(e) => { if (e.target === e.currentTarget) closeModal(); }}>
      <div
        className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong"
        style={{ minWidth: 480, maxHeight: '90vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          className="flex justify-between items-center mb-4"
        >
          <h3 className="m-0 text-base">
            Dependencies for {taskChip(task)}: {task.title}
          </h3>
          <button
            data-tip="Close this dialog"
            className="btn py-0.5 px-2 border border-default rounded cursor-pointer leading-none"
            onClick={closeModal}
          >
            ✕
          </button>
        </div>

        {loading && (
          <div className="text-text-faint text-sm py-5 text-center">
            Loading...
          </div>
        )}

        {!loading && deps && (
          <>
            <div className="mb-4">
              <h4 className="m-0 mb-2 text-md- text-text-soft">Depends on</h4>
              {deps.depends_on.length === 0 ? (
                <div className="text-text-faint text-sm italic">
                  This task has no dependencies.
                </div>
              ) : (
                deps.depends_on.map(renderDepRow)
              )}
            </div>

            <div className="mb-4">
              <h4 className="m-0 mb-2 text-md- text-text-soft">
                Depended on by
              </h4>
              {deps.depended_on_by.length === 0 ? (
                <div className="text-text-faint text-sm italic">
                  No tasks depend on this one.
                </div>
              ) : (
                deps.depended_on_by.map((dep) => {
                  const depTaskId = dep.id;
                  const depTask = tasks.find((t) => t.id === depTaskId) ?? dep;
                  return (
                    <div
                      key={depTaskId}
                      className="flex items-center py-1.5 px-2 bg-surface-subtle rounded mb-1"
                    >
                      <span className="font-mono text-sm+ text-text-faint">
                        {taskChip(depTask)}
                      </span>
                      <span className="text-sm ml-2">
                        {depTask?.title ?? `Task ${taskChip(depTask)}`}
                      </span>
                      {depTask && (
                        <span
                          className={cn(
                            'text-2xs py-px px-1 rounded-sm ml-2',
                            depTask.status === 'done'
                              ? 'bg-status-done-bg text-status-done'
                              : 'bg-status-pending-bg text-status-pending',
                          )}
                        >
                          {depTask.status}
                        </span>
                      )}
                    </div>
                  );
                })
              )}
            </div>

            {perms.canEdit && (
            <div className="mb-4">
              <h4 className="m-0 mb-2 text-md- text-text-soft">
                Add dependency
              </h4>
              <div className="flex gap-2 items-start">
                <div className="flex-1">
                  <input
                    type="text"
                    data-tip="Enter the ID or code of the task to depend on"
                    className={cn(
                      'w-full py-1.5 px-2.5 border rounded text-md-',
                      addError ? 'border-danger' : 'border-default',
                    )}
                    value={addInput}
                    onChange={(e) => {
                      setAddInput(e.target.value);
                      setAddError('');
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') handleAddDep();
                    }}
                    placeholder="Task ID or code"
                  />
                  {addError && (
                    <div className="text-sm+ text-danger mt-1">
                      {addError}
                    </div>
                  )}
                </div>
                <button data-tip="Add the entered task as a dependency" className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white shrink-0" onClick={handleAddDep}>
                  Add
                </button>
              </div>

              {siblingTasks.length > 0 && (
                <div className="mt-2.5">
                  <div className="text-sm+ text-text-faint mb-1.5">
                    Quick-add from same project:
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {siblingTasks
                      .filter(
                        (t) =>
                          !deps.depends_on.some((d) => d.id === t.id),
                      )
                      .slice(0, 20)
                      .map((t) => (
                        <button
                          key={t.id}
                          data-tip={t.title}
                          className="btn py-[3px] px-2 text-sm+ whitespace-nowrap border border-default rounded cursor-pointer"
                          onClick={() => handleQuickPick(t.id)}
                        >
                          {taskChip(t)} {t.title.slice(0, 30)}
                          {t.title.length > 30 ? '\u2026' : ''}
                          {t.status !== 'done' && (
                            <span
                              className="ml-1 font-semibold"
                              style={{ color: '#b2622d' }}
                              title="Not done — the task will be blocked from running until this dependency finishes"
                            >
                              · {t.status}
                            </span>
                          )}
                        </button>
                      ))}
                    {siblingTasks.filter(
                      (t) => !deps.depends_on.some((d) => d.id === t.id),
                    ).length === 0 && (
                      <span className="text-sm+ text-text-faint">
                        All tasks already added as dependencies.
                      </span>
                    )}
                  </div>
                </div>
              )}
            </div>
            )}
          </>
        )}

        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button data-tip="Close this dialog" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={closeModal}>
            Close
          </button>
        </div>
      </div>

      {/* Dependency-gate confirm — overlays the modal (z-toast) so it can't
          be dismissed behind its back. */}
      <UnmetDepsConfirmDialog
        state={unmetConfirm.state}
        onConfirm={() => unmetConfirm.settle(true)}
        onCancel={() => unmetConfirm.settle(false)}
      />
    </div>
  );
};

export default DependenciesModal;
