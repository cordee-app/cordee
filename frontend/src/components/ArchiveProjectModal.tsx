import { useEffect, useState } from 'react';
import { useStore, refreshQuotas } from '../store';
import { api, ApiError } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import { fmtBytes } from '../utils/file';
import type { Project } from '../types';

type BlockKind = 'running' | 'confirmed';

/** Execution rows (409 `running`) carry no title; confirmed task rows do. */
interface BlockTask {
  id: number;
  task_id?: number;
  title?: string;
  status?: string;
}

interface BlockInfo {
  kind: BlockKind;
  tasks: BlockTask[];
}

/** Flow is a strict two-step machine: confirm (build + hand off the zip),
 *  then remove (an explicit, separate destructive action). */
type Step = 'confirm' | 'remove';

/** Which call in the current step threw — drives the error copy. */
type FailedStep = 'archive' | 'download' | 'delete';

interface ArchiveInfo {
  filename: string;
  size: number;
  importLimit: number;
  syncWarning?: string;
}

export const ArchiveProjectModal = () => {
  const {
    showArchiveProjectModal, setShowArchiveProjectModal,
    archiveProjectTarget, setArchiveProjectTarget,
    projects, setProjects, setDashboardData, setActiveProject,
  } = useStore();
  const [step, setStep] = useState<Step>('confirm');
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [block, setBlock] = useState<BlockInfo | null>(null);
  const [failedStep, setFailedStep] = useState<FailedStep | null>(null);
  const [archive, setArchive] = useState<ArchiveInfo | null>(null);
  const [oversizedAck, setOversizedAck] = useState(false);

  const perms = useProjectPermissions(archiveProjectTarget);

  // The target can vanish underneath us (e.g. removed in another tab). Close
  // from an effect rather than during render, which React warns about.
  useEffect(() => {
    if (!showArchiveProjectModal) return;
    if (archiveProjectTarget === null) return;
    if (projects.some((p) => p.id === archiveProjectTarget)) return;
    setShowArchiveProjectModal(false);
    setArchiveProjectTarget(null);
  }, [showArchiveProjectModal, archiveProjectTarget, projects, setShowArchiveProjectModal, setArchiveProjectTarget]);

  if (!showArchiveProjectModal) return null;

  const project: Project | undefined = projects.find((p) => p.id === archiveProjectTarget);
  if (!project) return null;

  const name = project.name;
  const matches = typed === name;

  const reset = () => {
    setStep('confirm');
    setTyped('');
    setError('');
    setBlock(null);
    setFailedStep(null);
    setArchive(null);
    setOversizedAck(false);
  };

  const close = () => {
    setShowArchiveProjectModal(false);
    setArchiveProjectTarget(null);
    reset();
  };

  const handleBackdrop = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget && !busy) close();
  };

  /** Surface a 409 running/confirmed block and a step-appropriate message. */
  const describeFailure = (err: unknown, at: FailedStep) => {
    if (err instanceof ApiError && err.status === 409 && err.body) {
      const running = err.body.running as BlockTask[] | undefined;
      const confirmed = err.body.confirmed as BlockTask[] | undefined;
      let kind: BlockKind | null = null;
      if (Array.isArray(running) && running.length) kind = 'running';
      else if (Array.isArray(confirmed) && confirmed.length) kind = 'confirmed';
      if (kind) setBlock({ kind, tasks: kind === 'running' ? running! : confirmed! });
      if (at !== 'delete') {
        // Archive was never built — the block is the whole story.
        if (kind === 'running') {
          setError('Stop these running tasks before archiving the project.');
          return;
        }
        if (kind === 'confirmed') {
          setError('Cancel these queued tasks before archiving the project.');
          return;
        }
      }
    }
    const msg = err instanceof Error ? err.message : 'unknown error';
    // A failure at the remove step means the archive is already downloaded:
    // say so, and keep the Remove button available for a retry.
    setError(at === 'delete'
      ? 'Archive downloaded, but the project could not be removed: ' + msg
      : 'Failed to archive project: ' + msg);
  };

  // Step 1 — build the archive and hand the zip to the browser. No delete.
  const handleArchive = async () => {
    if (!matches || busy) return;
    setBusy(true);
    setError('');
    setBlock(null);
    setFailedStep(null);
    let at: FailedStep = 'archive';
    try {
      const res = await api.projects.archive(project.id);
      at = 'download';
      await api.projects.downloadArchive(project.id, res.filename);
      setArchive({
        filename: res.filename,
        size: res.size,
        importLimit: res.import_limit,
        syncWarning: res.sync_warning,
      });
      // Entering the destructive step must require a fresh typed confirmation:
      // clear the name typed in step 1 so the Remove button starts disabled.
      setTyped('');
      setStep('remove');
    } catch (err: unknown) {
      setFailedStep(at);
      describeFailure(err, at);
    } finally {
      setBusy(false);
    }
  };

  // Step 2 — only the user, after confirming the file exists, may remove.
  const handleRemove = async () => {
    if (!matches || busy) return;
    if (archive && archive.size > archive.importLimit && !oversizedAck) return;
    setBusy(true);
    setError('');
    setBlock(null);
    setFailedStep(null);
    try {
      await api.projects.delete(project.id);
    } catch (err: unknown) {
      setFailedStep('delete');
      describeFailure(err, 'delete');
      setBusy(false);
      return;
    }
    // Removal already happened — a refresh failure must not read as a failed
    // archive. The 30s loadAll poll catches up if this throws.
    try {
      const [projs, dash] = await Promise.all([
        api.projects.list(),
        api.dashboard(),
      ]);
      setProjects(projs);
      setDashboardData(dash);
    } catch { /* non-fatal: project is gone; poll will reconcile */ }
    refreshQuotas();
    if (useStore.getState().activeProject === project.id) setActiveProject(null);
    close();
    setBusy(false);
  };

  const oversized = !!archive && archive.size > archive.importLimit;
  const importLimitMb = archive ? Math.round(archive.importLimit / 1024 / 1024) : 0;
  const canRemove = matches && !busy && (!oversized || oversizedAck);
  const archiveBlocked = !!block && failedStep !== 'delete';

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={handleBackdrop}>
      <div className="modal-content bg-surface-raised dark:bg-surface-dark-raised rounded-lg p-6 min-w-[440px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong">
        <h3 className="m-0 mb-4 text-base">
          {step === 'confirm' ? `Archive project — ${name}` : `Archive downloaded — remove ${name}?`}
        </h3>

        {step === 'confirm' ? (
          <div className="mb-4 text-sm text-text-soft dark:text-text-dark-soft">
            This downloads a zip archive of the project. It does <strong>not</strong> remove
            anything — you confirm that separately in the next step:
            <ul className="mt-2 ml-4 list-disc space-y-1">
              <li>The database, all project files and git history, packaged as a single zip.</li>
              <li>You will then be asked to confirm the file was saved before the project is removed.</li>
            </ul>
            <div className="mt-3 p-3 rounded-md border border-danger/40 dark:border-danger-dark-DEFAULT/40 bg-danger/10 dark:bg-danger-dark-DEFAULT/10 text-sm">
              <strong>The zip is not encrypted</strong> and may contain confidential data.
              Keep it somewhere safe — it is required to restore the project.
            </div>
          </div>
        ) : (
          <div className="mb-4 text-sm text-text-soft dark:text-text-dark-soft">
            The archive has been handed to your browser. Before removing the project,
            please open <code className="break-all">{archive?.filename}</code> and
            confirm it was actually saved to disk.
            <ul className="mt-2 ml-4 list-disc space-y-1">
              <li>Archive size: <strong>{archive ? fmtBytes(archive.size) : '—'}</strong>.</li>
              <li>Once removed, the project folder, chats, tasks and cost history are
                gone permanently — the zip is your only copy.</li>
            </ul>
            {archive?.syncWarning ? (
              <div className="mt-3 p-3 rounded-md border border-warning/40 bg-warning/10 text-sm">
                <strong>Bucket sync warning:</strong> {archive.syncWarning}. Some
                cloud-stored objects may be missing from this archive.
              </div>
            ) : null}
            {oversized ? (
              <div className="mt-3 p-3 rounded-md border border-warning/50 bg-warning/10 text-sm">
                <strong>This archive exceeds the import limit.</strong> It is{' '}
                {fmtBytes(archive!.size)}, larger than the {importLimitMb} MB maximum
                the app accepts on import. It <strong>cannot be restored through the
                app UI</strong> — keep it safe and restore it manually if needed.
                <label className="mt-2 flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    data-tip="Acknowledge the archive cannot be re-imported through the app"
                    className="mt-0.5"
                    checked={oversizedAck}
                    onChange={(e) => setOversizedAck(e.target.checked)}
                  />
                  <span>I understand this archive cannot be re-imported through the app.</span>
                </label>
              </div>
            ) : null}
          </div>
        )}

        <div className="mb-4">
          <label className="block text-sm font-semibold text-text-soft dark:text-text-dark-soft mb-2">
            Type the project name to confirm{step === 'remove' ? ' removal' : ''}:
          </label>
          <div className="mb-2 text-lg font-bold text-danger dark:text-danger-dark-DEFAULT tracking-wide">
            {name}
          </div>
          <input
            type="text"
            data-tip="Type the project name exactly to enable the action"
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
                  {t.title
                    ? `#${t.id} — ${t.title}`
                    : `exec #${t.id}${t.task_id != null ? ` (task #${t.task_id})` : ''}`}
                </li>
              ))}
            </ul>
          </div>
        )}

        {!perms.canAdminister && (
          <div className="mb-4 p-3 rounded-md border border-border-muted text-sm text-text-soft">
            You have read-only access. Only a project owner can archive this project.
          </div>
        )}

        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button
            data-tip={step === 'remove' ? 'Close without removing; the downloaded archive is kept' : 'Close without archiving'}
            className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
            onClick={close}
            disabled={busy}
          >
            {step === 'remove' ? 'Close' : 'Cancel'}
          </button>
          {perms.canAdminister && step === 'confirm' && (
            <button
              data-tip="Build and download a zip archive; the project is removed in a separate step"
              className="btn btn-danger py-[7px] px-[18px] rounded cursor-pointer text-md- disabled:opacity-50 disabled:cursor-not-allowed"
              onClick={handleArchive}
              disabled={!matches || busy || archiveBlocked}
            >
              {busy ? 'Archiving...' : 'Archive & download'}
            </button>
          )}
          {perms.canAdminister && step === 'remove' && (
            <button
              data-tip="Permanently remove the project now that the archive is saved"
              className="btn btn-danger py-[7px] px-[18px] rounded cursor-pointer text-md- disabled:opacity-50 disabled:cursor-not-allowed"
              onClick={handleRemove}
              disabled={!canRemove}
            >
              {busy ? 'Removing...' : 'Remove project'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default ArchiveProjectModal;
