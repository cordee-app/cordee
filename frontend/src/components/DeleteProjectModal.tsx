import { useState } from 'react';
import { useStore } from '../store';
import { api, ApiError } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { Project } from '../types';

type BlockKind = 'running' | 'confirmed';

interface BlockInfo {
  kind: BlockKind;
  tasks: { id: number; title?: string; status?: string }[];
}

export const DeleteProjectModal = () => {
  const {
    showDeleteProjectModal, setShowDeleteProjectModal,
    deleteProjectTarget, setDeleteProjectTarget,
    projects, setProjects, setDashboardData, setActiveProject,
  } = useStore();
  const [typed, setTyped] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');
  const [block, setBlock] = useState<BlockInfo | null>(null);

  const perms = useProjectPermissions(deleteProjectTarget);

  if (!showDeleteProjectModal) return null;

  const project: Project | undefined = projects.find((p) => p.id === deleteProjectTarget);
  if (!project) {
    // Target vanished (e.g. already deleted) — just close.
    setShowDeleteProjectModal(false);
    setDeleteProjectTarget(null);
    return null;
  }

  const euOnly = !!project.eu_only;
  const name = project.name;
  const matches = typed === name;

  const handleBackdrop = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) {
      setShowDeleteProjectModal(false);
      setDeleteProjectTarget(null);
      setTyped('');
      setError('');
      setBlock(null);
    }
  };

  const close = () => {
    setShowDeleteProjectModal(false);
    setDeleteProjectTarget(null);
    setTyped('');
    setError('');
    setBlock(null);
  };

  const handleDelete = async () => {
    if (!matches) return;
    setDeleting(true);
    setError('');
    setBlock(null);
    try {
      await api.projects.delete(project.id);
      // Refresh both the canonical project list and the dashboard payload
      // (the home cards read from dashboardData, not projects).
      const [projs, dash] = await Promise.all([
        api.projects.list(),
        api.dashboard(),
      ]);
      setProjects(projs);
      setDashboardData(dash);
      if (useStore.getState().activeProject === project.id) setActiveProject(null);
      close();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409 && err.body) {
        const running = err.body.running as BlockInfo['tasks'] | undefined;
        const confirmed = err.body.confirmed as BlockInfo['tasks'] | undefined;
        if (Array.isArray(running) && running.length) {
          setBlock({ kind: 'running', tasks: running });
          setError('Stop these running tasks before deleting the project.');
          return;
        }
        if (Array.isArray(confirmed) && confirmed.length) {
          setBlock({ kind: 'confirmed', tasks: confirmed });
          setError('Cancel these queued tasks before deleting the project.');
          return;
        }
      }
      const msg = err instanceof Error ? err.message : 'unknown error';
      setError('Failed to delete project: ' + msg);
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={handleBackdrop}>
      <div className="modal-content bg-surface-raised dark:bg-surface-dark-raised rounded-lg p-6 min-w-[440px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong">
        <h3 className="m-0 mb-4 text-base text-danger dark:text-danger-dark-DEFAULT">
          Delete project — {name}
        </h3>

        {euOnly && (
          <div className="mb-4 p-3 rounded-md border border-danger/40 dark:border-danger-dark-DEFAULT/40 bg-danger/10 dark:bg-danger-dark-DEFAULT/10 text-sm">
            <strong>EU-only confidential project.</strong> Deletion is{' '}
            <strong>permanent and cannot be undone</strong> — there is no trash
            window. The Scaleway session (KMS-encrypted bucket) is crypto-shredded
            and the local folder is removed immediately.
          </div>
        )}

        <div className="mb-4 text-sm text-text-soft dark:text-text-dark-soft">
          This will permanently remove:
          <ul className="mt-2 ml-4 list-disc space-y-1">
            <li>The project folder and all its files, chats, memory and outputs.</li>
            <li>All tasks, executions and cost history from the database.</li>
            <li>{euOnly ? 'The KMS-encrypted Scaleway session (crypto-shredded).' : 'Any active Scaleway session and GPU deployment windows.'}</li>
          </ul>
          {!euOnly && (
            <p className="mt-2 text-xs text-text-faint dark:text-text-dark-faint">
              Non-EU projects are moved to a trash folder and hard-deleted after 7 days.
            </p>
          )}
        </div>

        <div className="mb-4">
          <label className="block text-sm font-semibold text-text-soft dark:text-text-dark-soft mb-2">
            Type the project name to confirm:
          </label>
          <div className="mb-2 text-lg font-bold text-danger dark:text-danger-dark-DEFAULT tracking-wide">
            {name}
          </div>
          <input
            type="text"
            data-tip="Type the project name exactly to enable deletion"
            className="w-full py-[7px] px-2.5 border border-default rounded text-base"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={name}
            autoFocus
          />
        </div>

        {error ? (
          <div className="text-dangerStrong text-sm mb-2">{error}</div>
        ) : null}

        {block && block.tasks.length > 0 && (
          <div className="mb-3 p-3 rounded-md border border-danger/40 dark:border-danger-dark-DEFAULT/40 bg-danger/10 dark:bg-danger-dark-DEFAULT/10 text-sm">
            <div className="font-semibold mb-1">
              {block.kind === 'running' ? 'Running tasks' : 'Queued (confirmed) tasks'}
            </div>
            <ul className="ml-4 list-disc space-y-0.5 max-h-40 overflow-y-auto">
              {block.tasks.map((t) => (
                <li key={t.id} className="text-text-soft dark:text-text-dark-soft">
                  #{t.id} — {t.title || '(untitled)'}
                </li>
              ))}
            </ul>
          </div>
        )}

        {!perms.canAdminister && (
          <div className="mb-4 p-3 rounded-md border border-border-muted text-sm text-text-soft">
            You have read-only access. Only a project owner can delete this project.
          </div>
        )}

        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button
            data-tip="Close without deleting"
            className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
            onClick={close}
            disabled={deleting}
          >
            Cancel
          </button>
          {perms.canAdminister && (
            <button
              data-tip="Permanently delete this project"
              className="btn btn-danger py-[7px] px-[18px] rounded cursor-pointer text-md- disabled:opacity-50 disabled:cursor-not-allowed"
              onClick={handleDelete}
              disabled={!matches || deleting || !!block}
            >
              {deleting ? 'Deleting...' : 'Delete project'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default DeleteProjectModal;
