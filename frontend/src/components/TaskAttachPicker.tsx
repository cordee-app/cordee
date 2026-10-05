import { useState, useEffect, useMemo } from 'react';
import { api } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { ContextOptions, ContextOptionItem, AttachableTask, TaskAttachment } from '../types';

type CategoryKey = 'definitions' | 'memories' | 'tasks' | 'working_docs';

const CATEGORY_LABELS: Record<CategoryKey, string> = {
  definitions: 'Definitions',
  memories: 'Memories',
  tasks: 'Tasks',
  working_docs: 'Working Docs',
};

interface Props {
  open: boolean;
  projectId: number | null;
  picked: TaskAttachment[];
  onChange: (picked: TaskAttachment[]) => void;
  onClose: () => void;
}

const attachKey = (a: { kind: string; ref: string | number }) => `${a.kind}::${a.ref}`;

export const TaskAttachPicker = ({ open, projectId, picked, onChange, onClose }: Props) => {
  const perms = useProjectPermissions(projectId);
  const [contextOptions, setContextOptions] = useState<ContextOptions | null>(null);
  const [attachableTasks, setAttachableTasks] = useState<AttachableTask[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || projectId === null) return;
    setLoading(true);
    Promise.all([
      api.projects.contextOptions(projectId).catch(() => null),
      api.tasks.attachable(projectId).catch(() => [] as AttachableTask[]),
    ]).then(([opts, tasks]) => {
      setContextOptions(opts);
      setAttachableTasks((tasks as AttachableTask[]) || []);
      setLoading(false);
    });
  }, [open, projectId]);

  const pickedSet = useMemo(() => new Set(picked.map(attachKey)), [picked]);

  const toggle = (item: ContextOptionItem | AttachableTask) => {
    const key = attachKey(item);
    const existing = picked.find((p) => attachKey(p) === key);
    if (existing) {
      onChange(picked.filter((p) => attachKey(p) !== key));
    } else {
      onChange([
        ...picked,
        {
          kind: item.kind as TaskAttachment['kind'],
          ref: String(item.ref),
          label: item.label || item.name || String(item.ref),
        },
      ]);
    }
  };

  if (!open || !perms.canEdit) return null;

  const renderCategory = (key: CategoryKey, items: Array<ContextOptionItem | AttachableTask>) => {
    if (!items || items.length === 0) {
      return (
        <div className="mb-2.5" key={key}>
          <div className="text-lg uppercase text-text-faint font-semibold mb-1">{CATEGORY_LABELS[key]}</div>
          <div className="text-base text-text-faint italic px-2">None available.</div>
        </div>
      );
    }
    return (
      <div className="mb-2.5 border border-border-muted rounded" key={key}>
        <div className="flex justify-between items-center py-3 px-4 bg-surface-subtle border-b border-border-muted">
          <span className="text-lg font-semibold">{CATEGORY_LABELS[key]}</span>
          <span className="text-base text-text-faint">{items.length}</span>
        </div>
        <div className="py-2 px-4 max-h-[480px] overflow-y-auto">
          {items.map((it, idx) => {
            const k = attachKey(it);
            const ck = pickedSet.has(k);
            const sub: string[] = [];
            const sz = (it as ContextOptionItem).size;
            if (sz) sub.push(`${(sz / 1024).toFixed(1)} KB`);
            const pn = (it as ContextOptionItem).phase_name;
            if (pn) sub.push(`· ${pn}`);
            if (it.status) sub.push(`· ${it.status}`);
            return (
              <label
                key={`${k}-${idx}`}
                className="flex items-center gap-2 py-2 text-base cursor-pointer"
                style={ck ? { background: 'rgba(188,140,255,.10)', borderRadius: 4, paddingLeft: 6, paddingRight: 6 } : undefined}
              >
                <input
                  data-tip="Include this item as context"
                  type="checkbox"
                  checked={ck}
                  onChange={() => toggle(it)}
                />
                <span>{it.label || it.name || String(it.ref)}{sub.length > 0 && <span className="text-text-faint text-xs ml-1">{sub.join(' ')}</span>}</span>
              </label>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div
      className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong"
        style={{ minWidth: 640 }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="m-0 mb-2 text-base">Attach context for improvement</h3>
        <p className="text-xs text-text-faint mb-3">
          Pick files to use as additional context when improving the task instructions. Only tasks with saved outputs are shown.
        </p>
        {loading || !contextOptions ? (
          <p className="text-sm text-text-faint">Loading…</p>
        ) : (
          <div className="max-h-[55vh] overflow-y-auto mb-2">
            {renderCategory('definitions', contextOptions.definitions || [])}
            {renderCategory('memories', contextOptions.memories || [])}
            {renderCategory('tasks', attachableTasks as unknown as ContextOptionItem[])}
            {renderCategory('working_docs', contextOptions.working_docs || [])}
          </div>
        )}
        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button
            data-tip="Close the context picker"
            className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
            onClick={onClose}
          >
            Done ({picked.length})
          </button>
        </div>
      </div>
    </div>
  );
};

export default TaskAttachPicker;