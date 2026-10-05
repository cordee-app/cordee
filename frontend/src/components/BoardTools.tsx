import { useState } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';

/**
 * Compact archive/selection controls rendered inline in the black tab bar
 * when the Board tab is active (desktop), or inside the board header (mobile).
 * Visual style: dark (#1c1c21 active-tab background), white text — reads as
 * an indentation of the "Project Board" tab into the black menu.
 */
export const BoardTools = ({ variant }: { variant: 'tabbar' | 'mobile' }) => {
  const showArchived = useStore((s) => s.showArchived);
  const setShowArchived = useStore((s) => s.setShowArchived);
  const selectionMode = useStore((s) => s.selectionMode);
  const toggleSelectionMode = useStore((s) => s.toggleSelectionMode);
  const exitSelectionMode = useStore((s) => s.exitSelectionMode);
  const deselectAll = useStore((s) => s.deselectAll);
  const selectedTasks = useStore((s) => s.selectedTasks);
  const setTasks = useStore((s) => s.setTasks);
  const tasks = useStore((s) => s.tasks);
  const activeProject = useStore((s) => s.activeProject);
  const perms = useProjectPermissions(activeProject);

  const [archiving, setArchiving] = useState(false);

  const toArchive = tasks.filter((t) => selectedTasks.has(t.id) && !t.archived);

  const handleArchive = async () => {
    if (toArchive.length === 0 || archiving) return;
    setArchiving(true);
    try {
      for (const t of toArchive) {
        await api.tasks.archive(t.id);
      }
      const ids = new Set(toArchive.map((t) => t.id));
      setTasks((prev) => prev.map((t) => (ids.has(t.id) ? { ...t, archived: 1 } : t)));
      exitSelectionMode();
    } catch (e) {
      console.error('Bulk archive failed:', e);
    } finally {
      setArchiving(false);
    }
  };

  if (variant === 'tabbar') {
    return (
      <div className="board-tools-tabbar flex items-center gap-2.5 ml-auto mr-1 h-full">
        <label
          className="flex items-center gap-1.5 cursor-pointer text-white/60 hover:text-white/85 transition-colors text-md- select-none"
          title="Show archived tasks in the board"
        >
          <input
            type="checkbox"
            data-tip="Show archived tasks in the board"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
            className="board-tools-checkbox"
          />
          <span>Show archived</span>
        </label>
        {perms.canEdit && (
          <button
            data-tip={selectionMode ? 'Exit selection mode' : 'Multiple selection for archivation'}
            className="board-tools-btn"
            onClick={() => (selectionMode ? exitSelectionMode() : toggleSelectionMode())}
          >
            {selectionMode ? 'Exit' : 'Multiple selection for archivation'}
          </button>
        )}
        {selectionMode && (
          <span className="board-tools-selection inline-flex items-center gap-2">
            <span className="text-white/70 text-md-">{selectedTasks.size} selected</span>
            <button
              data-tip="Clear the current selection"
              className="board-tools-btn board-tools-btn-ghost"
              onClick={deselectAll}
              disabled={archiving}
            >
              Deselect
            </button>
            {perms.canEdit && (
              <button
                data-tip={`Archive ${toArchive.length} task${toArchive.length === 1 ? '' : 's'}`}
                className="board-tools-btn board-tools-btn-danger"
                onClick={handleArchive}
                disabled={archiving || toArchive.length === 0}
              >
                {archiving
                  ? 'Archiving…'
                  : `Archive${toArchive.length > 0 ? ` (${toArchive.length})` : ''}`}
              </button>
            )}
          </span>
        )}
      </div>
    );
  }

  // Mobile variant — rendered inside the board scroll area
  return (
    <div className="flex items-center gap-3 mb-2 text-sm+">
      <label className="flex items-center gap-1.5 cursor-pointer">
        <input
          type="checkbox"
          data-tip="Show archived tasks in the board"
          checked={showArchived}
          onChange={(e) => setShowArchived(e.target.checked)}
        />
        <span>Show archived</span>
      </label>
      {perms.canEdit && (
        <button
          data-tip={selectionMode ? 'Exit selection mode' : 'Multiple selection for archivation'}
          className="btn-xs"
          onClick={() => (selectionMode ? exitSelectionMode() : toggleSelectionMode())}
        >
          {selectionMode ? 'Exit' : 'Multiple selection for archivation'}
        </button>
      )}
      {selectionMode && (
        <span className="inline-flex items-center gap-2">
          <span className="text-muted">{selectedTasks.size} selected</span>
          <button data-tip="Clear the current selection" className="btn-xs" onClick={deselectAll} disabled={archiving}>
            Deselect All
          </button>
          {perms.canEdit && (
            <button
              data-tip={`Archive ${toArchive.length} task${toArchive.length === 1 ? '' : 's'}`}
              className="btn-xs"
              onClick={handleArchive}
              disabled={archiving || toArchive.length === 0}
              style={{
                background: 'rgba(163,64,47,0.25)',
                border: '1px solid #a3402f',
                color: '#f3b9ad',
                fontWeight: 600,
                opacity: archiving ? 0.6 : 1,
                cursor: archiving ? 'not-allowed' : 'pointer',
              }}
            >
              {archiving
                ? 'Archiving…'
                : `Archive${toArchive.length > 0 ? ` ${toArchive.length}` : ''}`}
            </button>
          )}
        </span>
      )}
    </div>
  );
};

export default BoardTools;
