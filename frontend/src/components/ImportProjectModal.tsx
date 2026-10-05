import { useRef, useState } from 'react';
import { useStore, refreshQuotas } from '../store';
import { api, ApiError } from '../api';
import { Upload } from 'lucide-react';

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export const ImportProjectModal = () => {
  const {
    showImportProjectModal, setShowImportProjectModal,
    setProjects, setDashboardData,
  } = useStore();
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState('');
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  if (!showImportProjectModal) return null;

  const reset = () => {
    setFile(null);
    setDragging(false);
    setProgress('');
    setError('');
  };

  const close = () => {
    setShowImportProjectModal(false);
    reset();
  };

  const pick = (f: File | null) => {
    if (!f) return;
    if (!/\.zip$/i.test(f.name)) {
      setError('Please choose a .aingel.zip archive.');
      return;
    }
    setError('');
    setFile(f);
  };

  const handleInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    pick(e.target.files?.[0] ?? null);
    e.target.value = '';
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (busy) return;
    pick(e.dataTransfer.files?.[0] ?? null);
  };

  const handleImport = async () => {
    if (!file || busy) return;
    setBusy(true);
    setError('');
    setProgress('');
    try {
      await api.projects.importProject(file, setProgress);
      // New project consumed a slot and storage — refresh accounts so the
      // Dashboard cards and QuotaBar update without waiting for the poll.
      refreshQuotas();
      try {
        const [projs, dash] = await Promise.all([
          api.projects.list(),
          api.dashboard(),
        ]);
        setProjects(projs);
        setDashboardData(dash);
      } catch { /* project was imported; the 30s poll reconciles the lists */ }
      close();
    } catch (err: unknown) {
      // Clear the stale "Uploading…/Importing…" line so it cannot be mistaken
      // for progress after the request has failed.
      setProgress('');
      if (err instanceof ApiError && err.status === 409) {
        // 409 covers name, id and folder collisions — the server's own message
        // is the only accurate explanation.
        setError(err.message || 'conflict');
      } else if (err instanceof ApiError && err.status === 400) {
        setError('Invalid archive: ' + (err.message || 'the file is not a valid archive.'));
      } else if (err instanceof ApiError && err.status === 507) {
        setError('Not enough storage quota to import this project: ' + (err.message || 'quota exceeded'));
      } else {
        const msg = err instanceof Error ? err.message : 'unknown error';
        setError('Failed to import project: ' + msg);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={(e) => { if (e.target === e.currentTarget && !busy) close(); }}>
      <div className="modal-content bg-surface-raised dark:bg-surface-dark-raised rounded-lg p-6 min-w-[440px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong">
        <h3 className="m-0 mb-4 text-base">Import project from archive</h3>

        <div className="mb-4 text-sm text-text-soft dark:text-text-dark-soft">
          Select a <code>.aingel.zip</code> archive produced by the archive action. The
          project database, files and git history are restored under your account.
        </div>

        <div
          className="border border-dashed border-border-strong dark:border-border-dark-strong rounded-md p-5 text-center cursor-pointer transition-colors hover:border-accent dark:hover:border-accent-dark-DEFAULT"
          onClick={() => !busy && inputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); if (!busy) setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          style={dragging ? { borderColor: 'var(--c-accent)' } : undefined}
        >
          <Upload size={22} className="mx-auto mb-2 text-text-faint dark:text-text-dark-faint" />
          <div className="text-sm font-semibold text-text-soft dark:text-text-dark-soft">
            Drop a .aingel.zip here or click to choose
          </div>
          <div className="text-xs text-text-faint dark:text-text-dark-faint mt-1">
            Archives are unencrypted — only import files you trust.
          </div>
          <input
            ref={inputRef}
            type="file"
            accept=".zip,application/zip"
            className="hidden"
            onChange={handleInput}
            disabled={busy}
          />
        </div>

        {file && (
          <div className="mt-3 flex items-center justify-between gap-2 p-2.5 rounded-md border border-border-muted dark:border-border-dark-muted text-sm">
            <span className="font-semibold text-text-soft dark:text-text-dark-soft overflow-hidden text-ellipsis whitespace-nowrap">{file.name}</span>
            <span className="text-text-faint dark:text-text-dark-faint whitespace-nowrap">{formatSize(file.size)}</span>
          </div>
        )}

        {progress ? (
          <div className="mt-2 text-xs text-text-muted dark:text-text-dark-muted">{progress}</div>
        ) : null}

        {error ? (
          <div className="mt-3 text-dangerStrong text-sm">{error}</div>
        ) : null}

        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button
            data-tip="Close without importing"
            className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
            onClick={close}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            data-tip="Import the selected archive"
            className="btn btn-primary py-[7px] px-[18px] rounded cursor-pointer text-md- disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={handleImport}
            disabled={!file || busy}
          >
            {busy ? 'Importing...' : 'Import project'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ImportProjectModal;
