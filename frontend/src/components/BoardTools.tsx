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
  const toUnarchive = tasks.filter((t) => selectedTasks.has(t.id) && t.archived);
  const selectedCount = toArchive.length + toUnarchive.length;

  const plural = (n: number) => (n === 1 ? 'task' : 'tasks');

  const confirmMessage = (): string => {
    const archivePart = `${toArchive.length} ${plural(toArchive.length)}`;
    const unarchivePart = `${toUnarchive.length} ${plural(toUnarchive.length)}`;
    if (toArchive.length > 0 && toUnarchive.length > 0) {
      return `Archive ${archivePart} and unarchive ${unarchivePart}?`;
    }
    if (toArchive.length > 0) {
      return `Archive ${archivePart}? Archived tasks are hidden unless "Show archived" is enabled.`;
    }
    return `Unarchive ${unarchivePart}? They will reappear on the board.`;
  };

  const handleArchive = async () => {
    if (selectedCount === 0 || archiving) return;
    if (!window.confirm(confirmMessage())) return;
    setArchiving(true);
    try {
      for (const t of toArchive) {
        await api.tasks.archive(t.id);
      }
      for (const t of toUnarchive) {
        await api.tasks.unarchive(t.id);
      }
      const archivedIds = new Set(toArchive.map((t) => t.id));
      const unarchivedIds = new Set(toUnarchive.map((t) => t.id));
      setTasks((prev) => prev.map((t) => {
        if (archivedIds.has(t.id)) return { ...t, archived: 1 };
        if (unarchivedIds.has(t.id)) return { ...t, archived: 0 };
        return t;
      }));
      exitSelectionMode();
    } catch (e) {
      console.error('Bulk archive/unarchive failed:', e);
    } finally {
      setArchiving(false);
    }
  };

  const actionLabel = (withCounts: boolean): string => {
    if (archiving) return 'Updating…';
    const a = toArchive.length;
    const u = toUnarchive.length;
    if (a > 0 && u > 0) {
      return withCounts ? `Archive ${a} · Unarchive ${u}` : 'Archive / Unarchive';
    }
    if (u > 0) return `Unarchive${withCounts ? ` (${u})` : ''}`;
    return `Archive${withCounts && a > 0 ? ` (${a})` : ''}`;
  };

  const actionTitle = (): string => {
    const parts: string[] = [];
    if (toArchive.length > 0) parts.push(`archive ${toArchive.length} ${plural(toArchive.length)}`);
    if (toUnarchive.length > 0) parts.push(`unarchive ${toUnarchive.length} ${plural(toUnarchive.length)}`);
    if (parts.length === 0) return 'Select tasks first';
    const title = parts.join(' and ');
    return title.charAt(0).toUpperCase() + title.slice(1);
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
                data-tip={actionTitle()}
                className="board-tools-btn board-tools-btn-danger"
                onClick={handleArchive}
                disabled={archiving || selectedCount === 0}
              >
                {actionLabel(true)}
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
              data-tip={actionTitle()}
              className="btn-xs"
              onClick={handleArchive}
              disabled={archiving || selectedCount === 0}
              style={{
                background: 'rgba(163,64,47,0.25)',
                border: '1px solid #a3402f',
                color: '#f3b9ad',
                fontWeight: 600,
                opacity: archiving ? 0.6 : 1,
                cursor: archiving ? 'not-allowed' : 'pointer',
              }}
            >
              {actionLabel(false)}
            </button>
          )}
        </span>
      )}
    </div>
  );
};

export default BoardTools;
