import { useState, useEffect, useMemo } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';
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

const CATEGORY_KIND: Record<CategoryKey, string> = {
  definitions: 'definition',
  memories: 'memory',
  tasks: 'task',
  working_docs: 'working_doc',
};

interface Props {
  open: boolean;
  projectId: number | null;
  picked: TaskAttachment[];
  onChange: (picked: TaskAttachment[]) => void;
  onClose: () => void;
  phaseOrder?: string[];
}

const attachKey = (a: { kind: string; ref: string | number }) => `${a.kind}::${a.ref}`;

export const TaskAttachPicker = ({ open, projectId, picked, onChange, onClose, phaseOrder = [] }: Props) => {
  const perms = useProjectPermissions(projectId);
  const [contextOptions, setContextOptions] = useState<ContextOptions | null>(null);
  const [attachableTasks, setAttachableTasks] = useState<AttachableTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<CategoryKey, boolean>>({ definitions: true, memories: true, tasks: false, working_docs: true });
  const [search, setSearch] = useState('');

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

  const taskItems = attachableTasks as unknown as ContextOptionItem[];

  const pickedCountFor = (key: CategoryKey) => picked.filter((p) => p.kind === CATEGORY_KIND[key]).length;

  const matchesSearch = (it: ContextOptionItem | AttachableTask) => {
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return `${it.label || it.name || String(it.ref)}`.toLowerCase().includes(q);
  };

  const renderItem = (it: ContextOptionItem | AttachableTask, idx: number, suppressPhase: boolean) => {
    const k = attachKey(it);
    const ck = pickedSet.has(k);
    const sub: string[] = [];
    const sz = (it as ContextOptionItem).size;
    if (sz) sub.push(`${(sz / 1024).toFixed(1)} KB`);
    const pn = (it as ContextOptionItem).phase_name;
    if (pn && !suppressPhase) sub.push(`· ${pn}`);
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
  };

  const renderTaskGroups = (items: Array<ContextOptionItem | AttachableTask>) => {
    const groups = new Map<string, Array<ContextOptionItem | AttachableTask>>();
    for (const it of items) {
      const phase = (it as ContextOptionItem).phase_name || 'Notebook';
      const arr = groups.get(phase);
      if (arr) arr.push(it);
      else groups.set(phase, [it]);
    }
    const seen = new Set<string>();
    const ordered: string[] = [];
    for (const p of phaseOrder) {
      if (!seen.has(p)) { seen.add(p); ordered.push(p); }
    }
    const rest = Array.from(groups.keys()).filter((p) => !seen.has(p)).sort((a, b) => a.localeCompare(b));
    return [...ordered.filter((p) => groups.has(p)), ...rest].map((phase) => {
      const groupItems = groups.get(phase) || [];
      return (
        <div key={phase} className="mb-1">
          <div className="flex justify-between items-center py-1 text-xs font-semibold text-text-soft">
            <span>{phase}</span>
            <span className="text-text-faint">{groupItems.length}</span>
          </div>
          {groupItems.map((it, idx) => renderItem(it, idx, true))}
        </div>
      );
    });
  };

  const renderCategory = (key: CategoryKey, items: Array<ContextOptionItem | AttachableTask>) => {
    const filtered = search ? items.filter(matchesSearch) : items;
    if (search && filtered.length === 0) return null;
    const isCollapsed = search ? false : collapsed[key];
    return (
      <div className="mb-2.5 border border-border-muted rounded" key={key}>
        <button
          type="button"
          data-tip={isCollapsed ? 'Expand this section' : 'Collapse this section'}
          className="w-full flex justify-between items-center py-3 px-4 bg-surface-subtle border-b border-border-muted cursor-pointer text-left"
          onClick={() => setCollapsed((c) => ({ ...c, [key]: !c[key] }))}
        >
          <span className="flex items-center gap-1 text-lg font-semibold">
            <span className="text-text-faint">{isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}</span>
            {CATEGORY_LABELS[key]}
          </span>
          <span className="text-base text-text-faint">{`${pickedCountFor(key)}/${items.length}`}</span>
        </button>
        {!isCollapsed && (
          <div className="py-2 px-4 max-h-[480px] overflow-y-auto">
            {filtered.length === 0 ? (
              <div className="text-base text-text-faint italic px-2">None available.</div>
            ) : key === 'tasks' ? (
              renderTaskGroups(filtered)
            ) : (
              filtered.map((it, idx) => renderItem(it, idx, false))
            )}
          </div>
        )}
      </div>
    );
  };

  const noMatches = search !== '' && ![
    contextOptions?.definitions || [],
    contextOptions?.memories || [],
    taskItems,
    contextOptions?.working_docs || [],
  ].some((items) => items.some(matchesSearch));

  return (
    <div
      className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="modal-content bg-surface-raised dark:bg-surface-dark-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong"
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
          <>
            <input
              className="w-full py-1 px-2 border border-default rounded text-sm mb-2"
              placeholder="Search tasks, memories, working docs…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <div className="max-h-[55vh] overflow-y-auto mb-2">
              {noMatches ? (
                <div className="text-base text-text-faint italic px-4 py-2">No matches.</div>
              ) : (
                <>
                  {renderCategory('definitions', contextOptions.definitions || [])}
                  {renderCategory('memories', contextOptions.memories || [])}
                  {renderCategory('tasks', taskItems)}
                  {renderCategory('working_docs', contextOptions.working_docs || [])}
                </>
              )}
            </div>
          </>
        )}
        <div className="modal-actions flex justify-end gap-2 mt-4">
          <span className="mr-auto text-2xs text-text-faint self-center">These enrich "Guide my prompt" only. To inject files into the actual run prompt, use the task's "Context files (explicit)" picker.</span>
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
