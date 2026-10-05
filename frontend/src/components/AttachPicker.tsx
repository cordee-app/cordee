import { useState, useEffect } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { AttachItem, ContextOptions, ContextOptionItem } from '../types';

interface PickedItem {
  kind: string;
  label: string;
  ref: string;
}

type CategoryKey = 'definitions' | 'memories' | 'tasks' | 'working_docs';

const CATEGORY_LABELS: Record<CategoryKey, string> = {
  definitions: 'Definitions',
  memories: 'Memories',
  tasks: 'Tasks',
  working_docs: 'Working Docs',
};

export const AttachPicker = () => {
  const {
    showAttachPicker, setShowAttachPicker,
    activeChat, setActiveChat, activeProject,
  } = useStore();
  const perms = useProjectPermissions(activeProject);

  const [contextOptions, setContextOptions] = useState<ContextOptions | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!showAttachPicker || !activeProject) return;
    api.projects.contextOptions(activeProject).then(setContextOptions).catch(() => {});
  }, [showAttachPicker, activeProject]);

  useEffect(() => {
    if (showAttachPicker && activeChat?.attachments) {
      const existing = new Set(activeChat.attachments.map((a) => makeKey(a)));
      setPicked(existing);
    } else if (showAttachPicker) {
      setPicked(new Set());
    }
  }, [showAttachPicker, activeChat]);

  const makeKey = (item: Pick<AttachItem, 'kind' | 'ref' | 'label' | 'name' | 'path'>): string =>
    item.kind && item.ref ? `${item.kind}::${item.label || item.name || item.ref}::${item.ref}` : item.path || '';

  const isChecked = (item: ContextOptionItem): boolean => picked.has(makeItemKey(item));

  const makeItemKey = (item: ContextOptionItem): string => `${item.kind}::${item.label || item.name || item.ref}::${item.ref}`;

  const toggleItem = (item: ContextOptionItem) => {
    setPicked((prev) => {
      const next = new Set(prev);
      const key = makeItemKey(item);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const selectAll = (_category: CategoryKey, items: ContextOptionItem[]) => {
    setPicked((prev) => {
      const next = new Set(prev);
      for (const item of items) next.add(makeItemKey(item));
      return next;
    });
  };

  const deselectAll = (_category: CategoryKey, items: ContextOptionItem[]) => {
    setPicked((prev) => {
      const next = new Set(prev);
      for (const item of items) next.delete(makeItemKey(item));
      return next;
    });
  };

  const handleAttach = async () => {
    if (!activeChat || !perms.canEdit) return;
    setSubmitting(true);
    try {
      const pickedItems: PickedItem[] = [];
      for (const key of picked) {
        const parts = key.split('::');
        if (parts.length >= 3) {
          pickedItems.push({ kind: parts[0], label: parts[1], ref: parts[2] });
        }
      }

      await api.chats.update(activeChat.id, { attachments: pickedItems });
      const fresh = await api.chats.get(activeChat.id);
      setActiveChat(fresh);
      setShowAttachPicker(false);
    } catch (e) {
      console.error('Attach failed', e);
    } finally {
      setSubmitting(false);
    }
  };

  if (!showAttachPicker || !perms.canEdit) return null;

  const renderCategory = (key: CategoryKey, items: ContextOptionItem[]) => {
    if (!items.length) return null;

    const allSelected = items.every((i) => isChecked(i));
    const someSelected = items.some((i) => isChecked(i));

    return (
      <div className="attach-category mb-2.5 border border-border-muted rounded" key={key}>
        <div className="attach-cat-header flex justify-between items-center py-1.5 px-2.5 bg-surface-subtle border-b border-border-muted">
          <label className="attach-cat-label flex items-center gap-1.5 text-sm font-semibold cursor-pointer">
            <input
              data-tip="Select or deselect all items in this category"
              type="checkbox"
              checked={allSelected}
              ref={(el) => {
                if (el) el.indeterminate = someSelected && !allSelected;
              }}
              onChange={() =>
                allSelected ? deselectAll(key, items) : selectAll(key, items)
              }
            />
            {CATEGORY_LABELS[key]}
          </label>
          <span className="attach-cat-count text-xs text-text-muted">{items.length}</span>
        </div>
        <div className="attach-items py-1 px-2.5 max-h-40 overflow-y-auto">
          {items.map((item) => (
            <label key={makeItemKey(item)} className="attach-item flex items-center gap-1.5 py-[3px] text-sm+ cursor-pointer">
              <input
                data-tip="Include this item as context"
                type="checkbox"
                checked={isChecked(item)}
                onChange={() => toggleItem(item)}
              />
              <span>{item.label || item.name}</span>
            </label>
          ))}
        </div>
      </div>
    );
  };

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={(e) => { if (e.target === e.currentTarget) setShowAttachPicker(false); }}>
      <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong" onClick={(e) => e.stopPropagation()}>
        <h3 className="m-0 mb-4 text-base">Attach Context</h3>

        {!contextOptions ? (
          <p className="text-sm text-text-faint">Loading...</p>
        ) : (
          <div className="attach-options max-h-[50vh] overflow-y-auto mb-2">
            {renderCategory('definitions', contextOptions.definitions || [])}
            {renderCategory('memories', contextOptions.memories || [])}
            {renderCategory('tasks', contextOptions.tasks || [])}
            {renderCategory('working_docs', contextOptions.working_docs || [])}
          </div>
        )}

        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button
            data-tip="Close without attaching"
            className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
            onClick={() => setShowAttachPicker(false)}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            data-tip="Attach selected items to this chat"
            className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white"
            onClick={handleAttach}
            disabled={submitting || picked.size === 0}
          >
            {submitting ? 'Attaching...' : `Attach (${picked.size})`}
          </button>
        </div>
      </div>
    </div>
  );
};

export default AttachPicker;
