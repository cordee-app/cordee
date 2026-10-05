/**
 * Files browser — unified File Manager (Phase 1).
 *
 * Features:
 *  - Top toolbar: Upload Files / Upload Folder / New Folder + selection actions
 *  - Drag-and-drop zone (flat files; folder recursion via webkitGetAsEntry when available)
 *  - Checkbox multi-select + Move / Copy / Delete modals
 *  - Inline rename (click name -> edit -> move)
 *  - Per-file tags as chips, click to edit (tags + note), filter by tag
 *  - Category chips, search, sort, show-system toggle (existing)
 *  - Preview links via relUrl, grouped views preserved
 *
 * Data comes from api.files.catalog or MemoryPanel's files_catalog prop.
 * After every mutation we dispatch `aingel:files-changed` and optimistically
 * reload via api.files.catalog if needed (MemoryPanel also listens).
 */
import { useState, useMemo, useRef, useCallback, useEffect } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { FileEntry, FileCategory, TaskFileGroup } from '../types';
import {
  fmtBytes, fmtRelative, extIcon, relUrl,
} from '../utils/file';
import {
  Search, X, Eye, EyeOff, ChevronRight, ChevronDown,
  Folder, FileCode, FileText, Brain, MessageSquare,
  Calendar, HardDrive, ListChecks, Inbox, BookOpen,
  Upload, FolderPlus, Trash2, Copy, Move, Tag, Edit2, Check, Lock,
  Globe, ExternalLink, Clipboard, ClipboardCheck,
} from 'lucide-react';
import { cn } from '../utils/cn';

// ── Writable-root helpers (Batch 2: F3) — mirrors agent_files._WRITABLE_VARIANTS ──
const WRITABLE_VARIANTS = ['My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs'] as const;
const CANONICAL = 'Working Documents';
function toCanonicalRel(input: string): string {
  let s = input.trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+$/, '');
  if (!s) return CANONICAL;
  for (const v of WRITABLE_VARIANTS) {
    if (s === v || s.startsWith(v + '/')) return s.replace(/\/\//g, '/');
  }
  return `${CANONICAL}/${s}`.replace(/\/\//g, '/');
}

type CategoryKey = 'all' | FileCategory;
type SortKey = 'name' | 'date' | 'size';

interface Props {
  files: FileEntry[];
  byCategory: Record<string, number>;
  byTask: TaskFileGroup[];
  projectId: number;
  projectSlug?: string;
  onSelectTask?: (taskId: number) => void;
  onFilesChanged?: () => void;
  tagsMap?: Record<string, { tags: string[]; note: string; updated_at?: string }>;
  folders?: string[];
}

// ── WebDAV URL helpers ───────────────────────────────────────────────────
// Built from the current origin so every install shows its own host. The
// alternative assumes a sibling dav.<domain> host (served by the standalone
// WebDAV app on port 8002), which only exists if the operator set it up.
function davUrls(slug: string) {
  const encSlug = encodeURIComponent(slug);
  const { protocol, host, hostname } = window.location;
  const primary = `${protocol}//${host}/dav/${encSlug}/Working%20Documents/`;
  const davHost = `dav.${hostname.split('.').slice(1).join('.') || hostname}`;
  const alt = `${protocol}//${davHost}/${encSlug}/Working%20Documents/`;
  return { primary, alt, davHost };
}

const CATEGORIES: { key: CategoryKey; label: string; icon: React.ReactNode; description: string }[] = [
  { key: 'all',          label: 'All',          icon: <Inbox size={14} />,            description: 'All visible files across categories' },
  { key: 'deliverable',  label: 'Deliverables', icon: <Inbox size={14} />,            description: 'AI-produced files from task runs' },
  { key: 'output',       label: 'Outputs',      icon: <FileText size={14} />,         description: 'Raw per-execution output files' },
  { key: 'code',         label: 'Code',         icon: <FileCode size={14} />,         description: 'Task-generated source code' },
  { key: 'reference',    label: 'Reference',    icon: <Folder size={14} />,           description: 'User reference materials (Working Docs)' },
  { key: 'definition',   label: 'Definition',   icon: <BookOpen size={14} />,         description: 'READMEFIRST, GUIDE, Skills, master-spec' },
  { key: 'memory',       label: 'Memory',       icon: <Brain size={14} />,            description: 'Accumulated memory files' },
  { key: 'chat',         label: 'Chats',        icon: <MessageSquare size={14} />,    description: 'Conversation transcripts' },
  { key: 'archive',      label: 'Archive',      icon: <Folder size={14} />,           description: 'Legacy files (memory/, old session chats, TASKS.md)' },
  { key: 'system',       label: 'System',       icon: <Folder size={14} />,           description: 'Hidden bookkeeping (only when toggled)' },
];

const TASK_GROUPED: CategoryKey[] = ['deliverable', 'output', 'code'];

function emitFilesChanged(projectId: number, onFilesChanged?: () => void) {
  try { window.dispatchEvent(new CustomEvent('aingel:files-changed', { detail: { projectId } })); } catch {}
  if (onFilesChanged) onFilesChanged();
}

// Recursively read DataTransfer entries when browser supports webkitGetAsEntry
async function collectDroppedFiles(dataTransfer: DataTransfer): Promise<Array<{ file: File; key: string }>> {
  const items = Array.from(dataTransfer.items || []);
  // If no items or no webkitGetAsEntry support, fallback to flat FileList
  const hasEntry = items.some((it) => typeof (it as unknown as { webkitGetAsEntry?: unknown }).webkitGetAsEntry === 'function');
  if (!hasEntry || items.length === 0) {
    return Array.from(dataTransfer.files || []).map((f) => ({
      file: f,
      key: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name,
    }));
  }
  const out: Array<{ file: File; key: string }> = [];
  const readEntry = (entry: unknown, prefix: string): Promise<void> => {
    const e = entry as { isFile?: boolean; isDirectory?: boolean; file?: (cb: (file: File) => void, err?: (e: unknown) => void) => void; createReader?: () => { readEntries: (cb: (entries: unknown[]) => void) => void }; name?: string; fullPath?: string };
    if (!e) return Promise.resolve();
    if (e.isFile) {
      return new Promise<void>((resolve) => {
        e.file?.((file: File) => {
          const key = prefix ? `${prefix}/${file.name}` : (e.fullPath ? e.fullPath.replace(/^\//, '') : file.name);
          out.push({ file, key });
          resolve();
        }, () => resolve());
      });
    }
    if (e.isDirectory) {
      const reader = e.createReader?.();
      if (!reader) return Promise.resolve();
      return new Promise<void>((resolve) => {
        const readBatch = () => {
          reader.readEntries(async (entries: unknown[]) => {
            if (!entries.length) { resolve(); return; }
            for (const child of entries) {
              // For directory entries, recurse with correct prefix
              const dirName = (e.name || prefix.split('/').pop() || '');
              const base = prefix || dirName;
              const childName = (child as { name?: string }).name || '';
              const nextPrefix = base ? `${base}/${childName}`.replace(/^\/+/, '').replace(/\/\//g, '/') : childName;
              // Simpler: use fullPath if available
              const fullPath = (child as { fullPath?: string }).fullPath;
              if (fullPath) {
                await readEntry(child, fullPath.replace(/^\//, '').split('/').slice(0, -1).join('/'));
              } else {
                await readEntry(child, nextPrefix.split('/').slice(0, -1).join('/'));
              }
            }
            readBatch();
          });
        };
        readBatch();
      });
    }
    return Promise.resolve();
  };
  const entries = items
    .map((it) => (it as unknown as { webkitGetAsEntry?: () => unknown }).webkitGetAsEntry?.())
    .filter(Boolean) as unknown[];
  for (const ent of entries) {
    await readEntry(ent, '');
  }
  if (out.length === 0) {
    return Array.from(dataTransfer.files || []).map((f) => ({
      file: f,
      key: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name,
    }));
  }
  return out;
}

export const FilesBrowser = ({ files, byCategory, byTask, projectId, projectSlug, onSelectTask, onFilesChanged, tagsMap, folders }: Props) => {
  const perms = useProjectPermissions(projectId);
  const [activeCategory, setActiveCategory] = useState<CategoryKey>('all');
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<SortKey>('date');
  const [showSystem, setShowSystem] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [tagFilter, setTagFilter] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const dragDepth = useRef(0);
  const collapsedInitDone = useRef(false);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadMsg, setUploadMsg] = useState('');
  const [opBusy, setOpBusy] = useState(false);
  const [opError, setOpError] = useState('');
  const [highlightRels, setHighlightRels] = useState<Set<string>>(new Set());
  const highlightTimerRef = useRef<number | null>(null);

  // Modals
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [mkdirPath, setMkdirPath] = useState('');
  const [moveOpen, setMoveOpen] = useState(false);
  const [moveDst, setMoveDst] = useState('');
  const [movePickerMode, setMovePickerMode] = useState(true);
  const [moveNewSubfolder, setMoveNewSubfolder] = useState('');
  const [copyOpen, setCopyOpen] = useState(false);
  const [copyDst, setCopyDst] = useState('');
  const [copyPickerMode, setCopyPickerMode] = useState(true);
  const [copyNewSubfolder, setCopyNewSubfolder] = useState('');
  const [tagModal, setTagModal] = useState<{ rel: string; tags: string; note: string } | null>(null);
  const [editingRel, setEditingRel] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');
  // WebDAV modal
  const [webdavOpen, setWebdavOpen] = useState(false);
  const [webdavCopied, setWebdavCopied] = useState(false);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  // WebDAV slug resolution — prefer prop, else derive from store
  const projects = useStore((s) => s.projects);
  const resolvedSlug = useMemo(() => {
    if (projectSlug) return projectSlug;
    const p = projects.find((pr) => pr.id === projectId);
    return p?.slug || '';
  }, [projectSlug, projects, projectId]);

  const allTags = useMemo(() => {
    const s = new Set<string>();
    for (const f of files) {
      for (const t of (f.tags || [])) s.add(t);
      const tm = tagsMap?.[f.rel];
      if (tm) for (const t of (tm.tags || [])) s.add(t);
    }
    // also from tagsMap keys not in files (orphaned)
    if (tagsMap) for (const v of Object.values(tagsMap)) for (const t of (v.tags || [])) s.add(t);
    return Array.from(s).sort();
  }, [files, tagsMap]);

  const visible = useMemo(() => {
    let f = files;
    if (!showSystem) f = f.filter((x) => !x.hidden);
    if (activeCategory !== 'all') f = f.filter((x) => x.category === activeCategory);
    if (tagFilter) {
      f = f.filter((x) => {
        const tags = x.tags || tagsMap?.[x.rel]?.tags || [];
        return tags.includes(tagFilter);
      });
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      f = f.filter((x) =>
        x.name.toLowerCase().includes(q) ||
        x.rel.toLowerCase().includes(q) ||
        (x.tags || tagsMap?.[x.rel]?.tags || []).some((t) => t.toLowerCase().includes(q)) ||
        (x.note || tagsMap?.[x.rel]?.note || '').toLowerCase().includes(q),
      );
    }
    const sorted = [...f];
    if (sort === 'name') sorted.sort((a, b) => a.name.localeCompare(b.name));
    else if (sort === 'size') sorted.sort((a, b) => b.size - a.size);
    else sorted.sort((a, b) => (b.modified || '').localeCompare(a.modified || ''));
    return sorted;
  }, [files, activeCategory, search, sort, showSystem, tagFilter, tagsMap]);

  const groupedByFolder = useMemo(() => {
    if (activeCategory !== 'all') return null;
    const m = new Map<string, FileEntry[]>();
    for (const f of visible) {
      const folder = f.rel.includes('/') ? f.rel.slice(0, f.rel.lastIndexOf('/')) : '';
      const arr = m.get(folder) || [];
      arr.push(f);
      m.set(folder, arr);
    }
    // F8: include empty folders as 0-file groups so they are visible
    const extraFolders = folders || [];
    for (const fld of extraFolders) {
      if (!m.has(fld)) m.set(fld, []);
    }
    return Array.from(m.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [visible, activeCategory, folders]);

  const groupedTasks = useMemo(() => {
    if (!TASK_GROUPED.includes(activeCategory)) return null;
    return byTask
      .map((g) => ({
        ...g,
        files: g.files.filter((f) => f.category === activeCategory),
      }))
      .filter((g) => g.files.length > 0);
  }, [byTask, activeCategory]);

  // F1: collapsed-by-default — seed all folder/task group keys as true (collapsed) on first load,
  // then ensure any newly appearing keys also start collapsed without overwriting user toggles.
  useEffect(() => {
    const folderKeys = groupedByFolder ? groupedByFolder.map(([k]) => k) : [];
    const taskKeys = groupedTasks ? groupedTasks.map((g, idx) => (g.task_id !== null ? `t${g.task_id}` : `g${idx}`)) : [];
    const allKeys = [...folderKeys, ...taskKeys];
    if (allKeys.length === 0) return;
    if (!collapsedInitDone.current) {
      // First render where groups are available and collapsed is still empty → seed all as collapsed
      if (Object.keys(collapsed).length === 0) {
        const init: Record<string, boolean> = {};
        for (const k of allKeys) init[k] = true;
        setCollapsed(init);
      }
      collapsedInitDone.current = true;
      return;
    }
    // After init, collapse any new keys that the user hasn't toggled yet
    setCollapsed((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const k of allKeys) {
        if (!(k in next)) { next[k] = true; changed = true; }
      }
      return changed ? next : prev;
    });
  }, [groupedByFolder, groupedTasks]);

  const expandAll = useCallback(() => {
    const folderKeys = groupedByFolder ? groupedByFolder.map(([k]) => k) : [];
    const taskKeys = groupedTasks ? groupedTasks.map((g, idx) => (g.task_id !== null ? `t${g.task_id}` : `g${idx}`)) : [];
    const allKeys = [...folderKeys, ...taskKeys];
    const next: Record<string, boolean> = {};
    for (const k of allKeys) next[k] = false;
    setCollapsed(next);
  }, [groupedByFolder, groupedTasks]);

  const collapseAll = useCallback(() => {
    const folderKeys = groupedByFolder ? groupedByFolder.map(([k]) => k) : [];
    const taskKeys = groupedTasks ? groupedTasks.map((g, idx) => (g.task_id !== null ? `t${g.task_id}` : `g${idx}`)) : [];
    const allKeys = [...folderKeys, ...taskKeys];
    const next: Record<string, boolean> = {};
    for (const k of allKeys) next[k] = true;
    setCollapsed(next);
  }, [groupedByFolder, groupedTasks]);

  // F9: auto-scroll to first uploaded file after folder expand + catalog reload
  useEffect(() => {
    if (highlightRels.size === 0) return;
    const anyPresent = Array.from(highlightRels).some((r) => files.some((f) => f.rel === r));
    if (!anyPresent) return;
    const first = Array.from(highlightRels)[0];
    const id = requestAnimationFrame(() => {
      try {
        const esc = typeof CSS !== 'undefined' && (CSS as unknown as { escape?: (s: string) => string }).escape
          ? (CSS as unknown as { escape: (s: string) => string }).escape(first)
          : first.replace(/"/g, '\\"');
        const el = document.querySelector(`[data-rel="${esc}"]`);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } catch {
        try {
          const el2 = document.querySelector(`[data-rel="${first.replace(/"/g, '\\"')}"]`);
          if (el2) el2.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } catch { /* ignore */ }
      }
    });
    return () => cancelAnimationFrame(id);
  }, [highlightRels, files]);

  // F9: cleanup highlight timer on unmount
  useEffect(() => {
    return () => {
      if (highlightTimerRef.current) window.clearTimeout(highlightTimerRef.current);
    };
  }, []);

  const totalCount = files.filter((f) => showSystem || !f.hidden).length;
  const visibleCount = visible.length;

  // F4: writable folder list for Move/Copy pickers (from all files, plus canonical root)
  const writableFolders = useMemo(() => {
    const set = new Set<string>();
    set.add(CANONICAL);
    for (const f of files) {
      const dir = f.rel.includes('/') ? f.rel.slice(0, f.rel.lastIndexOf('/')) : '';
      if (!dir) continue;
      // Only consider dirs that are under a writable variant
      let isWritable = false;
      for (const v of WRITABLE_VARIANTS) {
        if (dir === v || dir.startsWith(v + '/')) { isWritable = true; break; }
      }
      if (!isWritable) continue;
      // Add this dir and all its writable ancestors (so intermediate folders appear)
      let cur = dir;
      while (cur) {
        let curWritable = false;
        for (const v of WRITABLE_VARIANTS) {
          if (cur === v || cur.startsWith(v + '/')) { curWritable = true; break; }
        }
        if (curWritable) set.add(cur);
        const slash = cur.lastIndexOf('/');
        if (slash === -1) break;
        cur = cur.slice(0, slash);
      }
    }
    // Also include groupedByFolder keys that are writable (covers empty folders that have no files listing for ancestor expansion)
    if (groupedByFolder) {
      for (const [k] of groupedByFolder as unknown as [string, unknown][]) {
        if (!k) continue;
        for (const v of WRITABLE_VARIANTS) {
          if (k === v || k.startsWith(v + '/')) { set.add(k); break; }
        }
      }
    }
    // F8: directly include empty writable folders even when groupedByFolder is null (filtered view)
    if (folders) {
      for (const fld of folders) {
        if (!fld) continue;
        for (const v of WRITABLE_VARIANTS) {
          if (fld === v || fld.startsWith(v + '/')) { set.add(fld); break; }
        }
        // also add writable ancestors of empty folder
        let cur = fld;
        while (cur) {
          let curWritable = false;
          for (const v of WRITABLE_VARIANTS) {
            if (cur === v || cur.startsWith(v + '/')) { curWritable = true; break; }
          }
          if (curWritable) set.add(cur);
          const slash = cur.lastIndexOf('/');
          if (slash === -1) break;
          cur = cur.slice(0, slash);
        }
      }
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [files, groupedByFolder, folders]);

  const toggleSelected = (rel: string) => {
    // F5: only writable (reference) files can be selected — guard against programmatic calls
    const entry = files.find((ff) => ff.rel === rel);
    if (entry && entry.category !== 'reference') return;
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(rel)) next.delete(rel); else next.add(rel);
      return next;
    });
  };
  const selectAllVisible = () => {
    // F5: only select writable (reference) files
    setSelected(new Set(visible.filter((f) => f.category === 'reference').map((f) => f.rel)));
  };
  const clearSelection = () => setSelected(new Set());

  const doUpload = useCallback(async (items: Array<{ file: File; key: string }>) => {
    if (!items.length) return;
    setUploadBusy(true);
    const CHUNK_SIZE = 20 * 1024 * 1024;
    const hasLarge = items.some(({ file }) => file.size > CHUNK_SIZE);
    if (hasLarge) setUploadMsg(`Preparing chunked upload for ${items.length} file${items.length === 1 ? '' : 's'}…`);
    else setUploadMsg(`Uploading ${items.length} file${items.length === 1 ? '' : 's'}…`);
    setOpError('');
    try {
      const res = await api.files.uploadFiles(projectId, items, (msg: string) => setUploadMsg(msg));
      const synced = res.files?.filter((f) => f.synced).length || 0;
      if (synced) setUploadMsg(`Uploaded ${items.length} file${items.length === 1 ? '' : 's'} (${synced} synced to bucket)`);
      else setUploadMsg(`Uploaded ${items.length} file${items.length === 1 ? '' : 's'}`);
      // F9: auto-highlight + unfold destination folder(s) so user finds uploads instantly
      const rels = (res.files || []).map((f) => f.rel).filter(Boolean) as string[];
      if (rels.length) {
        const parents = new Set(rels.map((r) => r.includes('/') ? r.slice(0, r.lastIndexOf('/')) : ''));
        setCollapsed((prev) => {
          const next = { ...prev };
          for (const p of parents) if (p) next[p] = false;
          return next;
        });
        setHighlightRels(new Set(rels));
        if (highlightTimerRef.current) window.clearTimeout(highlightTimerRef.current);
        highlightTimerRef.current = window.setTimeout(() => setHighlightRels(new Set()), 3500) as unknown as number;
      }
      emitFilesChanged(projectId, onFilesChanged);
      setTimeout(() => setUploadMsg(''), 2500);
    } catch (e) {
      const raw = e instanceof Error ? e.message : 'Upload failed';
      const low = raw.toLowerCase();
      const isTimeout = low.includes('failed to fetch') || low.includes('networkerror') || low.includes('network error') || low.includes('524') || low.includes('timeout');
      if (isTimeout) {
        const largeHint = hasLarge ? ' Large files are uploaded in 20 MB chunks automatically.' : '';
        setOpError(`Upload failed — check your connection and try again.${largeHint} If the problem persists, try smaller files or a wired connection.`);
      } else {
        setOpError(raw);
      }
      setUploadMsg('');
    } finally {
      setUploadBusy(false);
    }
  }, [projectId, onFilesChanged]);

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!perms.canEdit) return;
    const list = Array.from(e.target.files || []);
    const items = list.map((f) => ({ file: f, key: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name }));
    e.target.value = '';
    if (items.length) void doUpload(items);
  };

  const handleFolderInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!perms.canEdit) return;
    const list = Array.from(e.target.files || []);
    const items = list.map((f) => ({ file: f, key: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name }));
    e.target.value = '';
    if (items.length) void doUpload(items);
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = 0;
    setDragOver(false);
    if (!perms.canEdit) return;
    const items = await collectDroppedFiles(e.dataTransfer);
    if (items.length) void doUpload(items);
  };

  const handleMkdir = async () => {
    if (!perms.canEdit) return;
    const p = mkdirPath.trim();
    if (!p) return;
    const normalized = toCanonicalRel(p);
    setOpBusy(true); setOpError('');
    try {
      await api.files.mkdir(projectId, normalized);
      setMkdirOpen(false); setMkdirPath('');
      emitFilesChanged(projectId, onFilesChanged);
    } catch (e) { setOpError(e instanceof Error ? e.message : 'mkdir failed'); }
    finally { setOpBusy(false); }
  };

  const handleDelete = async () => {
    if (!perms.canEdit) return;
    if (selected.size === 0) return;
    if (!window.confirm(`Move ${selected.size} item${selected.size === 1 ? '' : 's'} to trash?`)) return;
    setOpBusy(true); setOpError('');
    try {
      await api.files.deleteFiles(projectId, Array.from(selected));
      clearSelection();
      emitFilesChanged(projectId, onFilesChanged);
    } catch (e) { setOpError(e instanceof Error ? e.message : 'Delete failed'); }
    finally { setOpBusy(false); }
  };

  const handleMove = async () => {
    if (!perms.canEdit) return;
    // F3 normalize + F4 picker mode handling
    let rawDst = moveDst.trim();
    if (!rawDst) return;
    // In picker mode, moveDst is a folder; append optional new subfolder
    if (movePickerMode) {
      let folder = toCanonicalRel(rawDst);
      const extra = moveNewSubfolder.trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+$/, '');
      if (extra) folder = `${folder}/${extra}`.replace(/\/\//g, '/');
      rawDst = folder;
      const selectedArr = Array.from(selected);
      if (selectedArr.length === 0) return;
      setOpBusy(true); setOpError('');
      try {
        for (const src of selectedArr) {
          const base = src.split('/').pop() || src;
          const oneDst = rawDst.endsWith('/') ? `${rawDst}${base}` : `${rawDst}/${base}`;
          const normalized = toCanonicalRel(oneDst);
          await api.files.move(projectId, src, normalized);
        }
        clearSelection(); setMoveOpen(false); setMoveDst(''); setMoveNewSubfolder('');
        emitFilesChanged(projectId, onFilesChanged);
      } catch (e) { setOpError(e instanceof Error ? e.message : 'Move failed'); }
      finally { setOpBusy(false); }
      return;
    }
    // Path mode: single file allows rename (direct dst), multi treats as folder+base
    const normalizedDst = toCanonicalRel(rawDst);
    if (selected.size === 1) {
      const src = Array.from(selected)[0];
      setOpBusy(true); setOpError('');
      try {
        await api.files.move(projectId, src, normalizedDst);
        clearSelection(); setMoveOpen(false); setMoveDst(''); setMoveNewSubfolder('');
        emitFilesChanged(projectId, onFilesChanged);
      } catch (e) { setOpError(e instanceof Error ? e.message : 'Move failed'); }
      finally { setOpBusy(false); }
      return;
    }
    setOpBusy(true); setOpError('');
    try {
      for (const src of Array.from(selected)) {
        const base = src.split('/').pop() || src;
        const oneDst = normalizedDst.endsWith('/') ? `${normalizedDst}${base}` : `${normalizedDst}/${base}`;
        await api.files.move(projectId, src, oneDst);
      }
      clearSelection(); setMoveOpen(false); setMoveDst(''); setMoveNewSubfolder('');
      emitFilesChanged(projectId, onFilesChanged);
    } catch (e) { setOpError(e instanceof Error ? e.message : 'Move failed'); }
    finally { setOpBusy(false); }
  };

  const handleCopy = async () => {
    if (!perms.canEdit) return;
    let rawDst = copyDst.trim();
    if (!rawDst) return;
    if (copyPickerMode) {
      let folder = toCanonicalRel(rawDst);
      const extra = copyNewSubfolder.trim().replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+$/, '');
      if (extra) folder = `${folder}/${extra}`.replace(/\/\//g, '/');
      rawDst = folder;
      const selectedArr = Array.from(selected);
      if (selectedArr.length === 0) return;
      setOpBusy(true); setOpError('');
      try {
        for (const src of selectedArr) {
          const base = src.split('/').pop() || src;
          const oneDst = rawDst.endsWith('/') ? `${rawDst}${base}` : `${rawDst}/${base}`;
          const normalized = toCanonicalRel(oneDst);
          await api.files.copy(projectId, src, normalized);
        }
        clearSelection(); setCopyOpen(false); setCopyDst(''); setCopyNewSubfolder('');
        emitFilesChanged(projectId, onFilesChanged);
      } catch (e) { setOpError(e instanceof Error ? e.message : 'Copy failed'); }
      finally { setOpBusy(false); }
      return;
    }
    const normalizedDst = toCanonicalRel(rawDst);
    if (selected.size === 1) {
      const src = Array.from(selected)[0];
      setOpBusy(true); setOpError('');
      try {
        await api.files.copy(projectId, src, normalizedDst);
        clearSelection(); setCopyOpen(false); setCopyDst(''); setCopyNewSubfolder('');
        emitFilesChanged(projectId, onFilesChanged);
      } catch (e) { setOpError(e instanceof Error ? e.message : 'Copy failed'); }
      finally { setOpBusy(false); }
      return;
    }
    setOpBusy(true); setOpError('');
    try {
      for (const src of Array.from(selected)) {
        const base = src.split('/').pop() || src;
        const oneDst = normalizedDst.endsWith('/') ? `${normalizedDst}${base}` : `${normalizedDst}/${base}`;
        await api.files.copy(projectId, src, oneDst);
      }
      clearSelection(); setCopyOpen(false); setCopyDst(''); setCopyNewSubfolder('');
      emitFilesChanged(projectId, onFilesChanged);
    } catch (e) { setOpError(e instanceof Error ? e.message : 'Copy failed'); }
    finally { setOpBusy(false); }
  };

  const handleRename = async (oldRel: string) => {
    if (!perms.canEdit) return;
    const newName = editingName.trim();
    if (!newName || newName === oldRel.split('/').pop()) { setEditingRel(null); return; }
    const dir = oldRel.includes('/') ? oldRel.slice(0, oldRel.lastIndexOf('/') + 1) : '';
    const newRel = dir + newName;
    if (newRel === oldRel) { setEditingRel(null); return; }
    setOpBusy(true); setOpError('');
    try {
      await api.files.move(projectId, oldRel, newRel);
      setEditingRel(null);
      emitFilesChanged(projectId, onFilesChanged);
    } catch (e) { setOpError(e instanceof Error ? e.message : 'Rename failed'); }
    finally { setOpBusy(false); }
  };

  const handleSaveTags = async () => {
    if (!perms.canEdit) return;
    if (!tagModal) return;
    const rawTags = tagModal.tags.split(',').map((t) => t.trim()).filter(Boolean);
    setOpBusy(true); setOpError('');
    try {
      await api.files.setTags(projectId, tagModal.rel, rawTags, tagModal.note);
      setTagModal(null);
      emitFilesChanged(projectId, onFilesChanged);
    } catch (e) { setOpError(e instanceof Error ? e.message : 'Tag save failed'); }
    finally { setOpBusy(false); }
  };

  const openTagModal = (f: FileEntry) => {
    const tm = tagsMap?.[f.rel];
    const tags = f.tags ?? tm?.tags ?? [];
    const note = f.note ?? tm?.note ?? '';
    setTagModal({ rel: f.rel, tags: tags.join(', '), note });
  };

  return (
    <div
      className={cn('files-browser flex flex-col gap-2', dragOver && 'ring-2 ring-accent ring-offset-1 rounded')}
      onDragEnter={(e) => { e.preventDefault(); if (!perms.canEdit) return; dragDepth.current++; setDragOver(true); }}
      onDragOver={(e) => { e.preventDefault(); if (!perms.canEdit) return; e.dataTransfer.dropEffect = 'copy'; }}
      onDragLeave={(e) => { e.preventDefault(); dragDepth.current--; if (dragDepth.current <= 0) { dragDepth.current = 0; setDragOver(false); } }}
      onDrop={(e) => { e.preventDefault(); if (!perms.canEdit) return; dragDepth.current = 0; setDragOver(false); void handleDrop(e); }}
      onDragEnd={() => { dragDepth.current = 0; setDragOver(false); }}
    >
      {/* ── Toolbar: upload + folder + selection actions ── */}
      <div className="files-toolbar flex items-center gap-1.5 flex-wrap">
        {perms.canEdit ? (
          <button
            data-tip="Upload files to Working Documents"
            onClick={() => fileInputRef.current?.click()}
            className="py-1 px-2.5 text-sm border border-default rounded inline-flex items-center gap-1.5 bg-surface-muted hover:bg-surface-raised cursor-pointer"
            disabled={uploadBusy}
          >
            <Upload size={13} /> Upload
          </button>
        ) : null}
        {perms.canEdit ? (
          <button
            data-tip="Upload a folder (keeps structure)"
            onClick={() => folderInputRef.current?.click()}
            className="py-1 px-2 text-sm border border-default rounded inline-flex items-center gap-1 bg-surface-muted hover:bg-surface-raised cursor-pointer"
            disabled={uploadBusy}
          >
            <Folder size={13} /> Folder
          </button>
        ) : null}
        <input ref={fileInputRef} type="file" multiple className="hidden" onChange={handleFileInput} />
        <input ref={folderInputRef} type="file" className="hidden" onChange={handleFolderInput} {...({ webkitdirectory: '', directory: '' } as Record<string, string>)} />
        {perms.canEdit ? (
          <button
            data-tip="New folder under Working Documents"
            onClick={() => setMkdirOpen(true)}
            className="py-1 px-2 text-sm border border-default rounded inline-flex items-center gap-1 bg-surface-muted hover:bg-surface-raised cursor-pointer"
          >
            <FolderPlus size={13} /> New Folder
          </button>
        ) : null}
        <button
          data-tip="Open WebDAV mount instructions"
          onClick={() => setWebdavOpen(true)}
          className="py-1 px-2 text-sm border border-default rounded inline-flex items-center gap-1 bg-surface-muted hover:bg-surface-raised cursor-pointer"
        >
          <Globe size={13} /> WebDAV
        </button>
        <div className="flex-1" />
        {selected.size > 0 ? (
          <div className="flex items-center gap-1">
            <span className="text-xs text-text-faint">{selected.size} selected</span>
            <button data-tip="Select all visible files" onClick={() => selectAllVisible()} className="text-xs text-link hover:underline px-1">All</button>
            <button data-tip="Clear the selection" onClick={clearSelection} className="text-xs text-link hover:underline px-1">Clear</button>
            {perms.canEdit ? (
            <>
            <button
              data-tip="Move selected files"
              onClick={() => {
                // F4 pre-selection
                const sel = Array.from(selected);
                let defFolder = CANONICAL;
                if (sel.length === 1) {
                  const src = sel[0];
                  const dir = src.includes('/') ? src.slice(0, src.lastIndexOf('/')) : '';
                  if (dir) defFolder = dir;
                } else if (sel.length > 1) {
                  // most common folder among selected, fallback to canonical
                  const counts = new Map<string, number>();
                  for (const s of sel) {
                    const d = s.includes('/') ? s.slice(0, s.lastIndexOf('/')) : CANONICAL;
                    counts.set(d, (counts.get(d) || 0) + 1);
                  }
                  let best = CANONICAL; let bestN = -1;
                  for (const [k, n] of counts) if (n > bestN) { best = k; bestN = n; }
                  defFolder = best;
                }
                if (!writableFolders.includes(defFolder)) defFolder = CANONICAL;
                setMoveDst(defFolder);
                setMoveNewSubfolder('');
                setMovePickerMode(true);
                setMoveOpen(true);
              }}
              className="py-1 px-2 text-xs border border-default rounded inline-flex items-center gap-1 bg-surface-muted hover:bg-surface-raised cursor-pointer"
              disabled={opBusy}
            >
              <Move size={11} /> Move
            </button>
            <button
              data-tip="Copy selected files"
              onClick={() => {
                const sel = Array.from(selected);
                let defFolder = CANONICAL;
                if (sel.length === 1) {
                  const src = sel[0];
                  const dir = src.includes('/') ? src.slice(0, src.lastIndexOf('/')) : '';
                  if (dir) defFolder = dir;
                } else if (sel.length > 1) {
                  const counts = new Map<string, number>();
                  for (const s of sel) {
                    const d = s.includes('/') ? s.slice(0, s.lastIndexOf('/')) : CANONICAL;
                    counts.set(d, (counts.get(d) || 0) + 1);
                  }
                  let best = CANONICAL; let bestN = -1;
                  for (const [k, n] of counts) if (n > bestN) { best = k; bestN = n; }
                  defFolder = best;
                }
                if (!writableFolders.includes(defFolder)) defFolder = CANONICAL;
                setCopyDst(defFolder);
                setCopyNewSubfolder('');
                setCopyPickerMode(true);
                setCopyOpen(true);
              }}
              className="py-1 px-2 text-xs border border-default rounded inline-flex items-center gap-1 bg-surface-muted hover:bg-surface-raised cursor-pointer"
              disabled={opBusy}
            >
              <Copy size={11} /> Copy
            </button>
            <button
              data-tip="Move selected files to trash"
              onClick={handleDelete}
              className="py-1 px-2 text-xs border border-danger text-danger rounded inline-flex items-center gap-1 hover:bg-danger/10 cursor-pointer"
              disabled={opBusy}
            >
              <Trash2 size={11} /> Delete
            </button>
            </>
            ) : null}
          </div>
        ) : null}
      </div>

      {uploadBusy && uploadMsg ? (
        <div className="text-xs text-text-faint bg-accent-soft border border-accent/20 rounded px-2 py-1">{uploadMsg}</div>
      ) : uploadMsg ? (
        <div className="text-xs text-success bg-success/10 border border-success/20 rounded px-2 py-1">{uploadMsg}</div>
      ) : null}
      {opError ? (
        <div className="text-xs text-danger bg-danger/10 border border-danger/20 rounded px-2 py-1 flex items-center justify-between">
          <span>{opError}</span>
          <button data-tip="Dismiss this error message" onClick={() => setOpError('')} className="ml-2 text-danger hover:underline text-xs"><X size={11} /></button>
        </div>
      ) : null}
      {dragOver ? (
        <div className="text-xs text-accent bg-accent-soft border border-dashed border-accent rounded px-2 py-2 text-center">
          Drop files or folders here to upload to Working Documents
        </div>
      ) : null}

      {/* ── F6: Persistent selection tray (always visible when any selected, outside filtered visible) ── */}
      {selected.size > 0 ? (
        <div className="bg-accent-soft border border-accent/20 rounded px-2 py-1.5 flex flex-wrap gap-1 items-center">
          <span className="text-xs font-medium text-ink shrink-0">{selected.size} selected:</span>
          {Array.from(selected).slice(0, 8).map((rel) => {
            const base = rel.split('/').pop() || rel;
            return (
              <span key={rel} className="inline-flex items-center gap-1 text-xs bg-surface-raised border border-default rounded-full px-2 py-0.5 max-w-[160px]">
                <span className="truncate" title={rel}>{base}</span>
                <button
                  data-tip="Remove from selection"
                  onClick={() => toggleSelected(rel)}
                  className="shrink-0 text-text-faint hover:text-danger p-0.5 rounded-full hover:bg-danger/10"
                  aria-label={`Deselect ${rel}`}
                >
                  <X size={10} />
                </button>
              </span>
            );
          })}
          {selected.size > 8 ? (
            <span className="text-xs text-text-faint">+{selected.size - 8} more</span>
          ) : null}
          <span className="flex-1" />
          <button data-tip="Clear the selection" onClick={clearSelection} className="text-xs text-link hover:underline shrink-0">Clear</button>
        </div>
      ) : null}

      {/* ── Filter row ── */}
      <div className="files-filter-row flex items-center gap-1.5 flex-wrap">
        <div className="files-search flex-1 flex items-center gap-1.5 px-2 py-1 bg-surface-muted border border-default rounded min-w-[140px]">
          <Search size={12} className="text-text-faint shrink-0" />
          <input
            data-tip="Filter files by name, path, or tags"
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Filter by name, path, or tags…"
            className="flex-1 bg-transparent border-none outline-none text-sm placeholder:text-text-faint"
          />
          {search ? (
            <button data-tip="Clear the search filter" onClick={() => setSearch('')} className="text-text-faint hover:text-ink shrink-0" aria-label="Clear search">
              <X size={12} />
            </button>
          ) : null}
        </div>
        <select
          data-tip="Sort the file list"
          value={sort}
          onChange={(e) => setSort(e.target.value as SortKey)}
          className="text-sm py-1 px-1.5 bg-surface-muted border border-default rounded text-ink"
          aria-label="Sort files"
        >
          <option value="date">📅 Newest</option>
          <option value="name">A → Z</option>
          <option value="size">Size</option>
        </select>
        <button
          data-tip={showSystem ? 'Hide system files' : 'Show system files'}
          onClick={() => setShowSystem((s) => !s)}
          className={cn(
            'py-1 px-2 text-sm border border-default rounded inline-flex items-center gap-1 cursor-pointer',
            showSystem ? 'bg-accent text-white border-accent' : 'bg-surface-muted text-ink hover:bg-surface-raised',
          )}
          aria-pressed={showSystem}
        >
          {showSystem ? <Eye size={12} /> : <EyeOff size={12} />}
          {showSystem ? 'System' : 'Hidden'}
        </button>
      </div>

      {/* ── Tag filter chips ── */}
      {allTags.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-xs text-text-faint inline-flex items-center gap-1"><Tag size={11} /> Tags:</span>
          <button
            data-tip="Show all tags"
            onClick={() => setTagFilter(null)}
            className={cn('py-0.5 px-1.5 text-xs border rounded cursor-pointer', !tagFilter ? 'bg-accent text-white border-accent' : 'bg-surface-muted border-default hover:bg-surface-raised')}
          >
            All
          </button>
          {allTags.map((t) => (
            <button
              key={t}
              data-tip={`Filter by tag "${t}"`}
              onClick={() => setTagFilter((cur) => cur === t ? null : t)}
              className={cn('py-0.5 px-1.5 text-xs border rounded cursor-pointer inline-flex items-center gap-1', tagFilter === t ? 'bg-accent text-white border-accent' : 'bg-surface-muted border-default hover:bg-surface-raised')}
            >
              {t}
            </button>
          ))}
          {tagFilter ? <button data-tip="Clear the tag filter" onClick={() => setTagFilter(null)} className="text-xs text-link hover:underline ml-1"><X size={10} /> clear</button> : null}
        </div>
      ) : null}

      {/* ── Category chips ── */}
      <div className="files-cats flex flex-wrap gap-1">
        {CATEGORIES.map((c) => {
          const count = c.key === 'all'
            ? totalCount
            : (byCategory[c.key] || 0);
          if (c.key === 'system' && !showSystem && count === 0) return null;
          const active = activeCategory === c.key;
          return (
            <button
              key={c.key}
              data-tip={c.description}
              onClick={() => setActiveCategory(c.key)}
              className={cn(
                'py-0.5 px-2 text-sm border border-default rounded inline-flex items-center gap-1 cursor-pointer',
                active
                  ? 'bg-accent text-white border-accent'
                  : 'bg-surface-muted text-ink hover:bg-surface-raised',
              )}
            >
              <span className="shrink-0 opacity-90">{c.icon}</span>
              {c.label}
              {count > 0 ? (
                <span className={cn(
                  'text-sm rounded-sm px-1 inline-block',
                  active ? 'bg-white/20' : 'bg-surface-raised',
                )}>{count}</span>
              ) : null}
            </button>
          );
        })}
      </div>

      {/* ── Expand / Collapse all (F1) ── */}
      {((groupedByFolder && groupedByFolder.length > 0) || (groupedTasks && groupedTasks.length > 0)) ? (
        <div className="flex items-center gap-1 -mt-1">
          <button data-tip="Expand all folder groups" onClick={expandAll} className="text-xs text-link hover:underline px-1">Expand all</button>
          <span className="text-xs text-text-faint">·</span>
          <button data-tip="Collapse all folder groups" onClick={collapseAll} className="text-xs text-link hover:underline px-1">Collapse all</button>
        </div>
      ) : null}

      {/* ── Status line ── */}
      <div className="text-xs text-text-faint -mt-1 flex items-center gap-2 flex-wrap">
        <span>
          {visibleCount} of {totalCount} file{totalCount === 1 ? '' : 's'}
          {activeCategory !== 'all' ? ` in ${CATEGORIES.find((c) => c.key === activeCategory)?.label ?? ''}` : ''}
          {tagFilter ? ` · tag "${tagFilter}"` : ''}
          {search ? ` matching "${search}"` : ''}
        </span>
        {visibleCount > 0 ? (
          <>
            <button data-tip="Select all visible files" onClick={selectAllVisible} className="text-link hover:underline text-xs">Select all</button>
            {selected.size > 0 ? <button data-tip="Clear the selection" onClick={clearSelection} className="text-link hover:underline text-xs">Clear</button> : null}
          </>
        ) : null}
      </div>

      {/* ── Render ── */}
      {visibleCount === 0 ? (
        <EmptyState
          category={activeCategory}
          search={search}
          onClearSearch={() => setSearch('')}
          showSystem={showSystem}
          onShowSystem={() => setShowSystem(true)}
        />
      ) : groupedTasks && groupedTasks.length > 0 ? (
        <TaskGroupedView
          groups={groupedTasks}
          projectId={projectId}
          onSelectTask={onSelectTask}
          collapsed={collapsed}
          setCollapsed={setCollapsed}
          selected={selected}
          onToggleSelected={toggleSelected}
          editingRel={editingRel}
          editingName={editingName}
          setEditingRel={setEditingRel}
          setEditingName={setEditingName}
          onRename={handleRename}
          onOpenTag={openTagModal}
          tagsMap={tagsMap}
          highlightRels={highlightRels}
          forceExpand={!!search.trim() || !!tagFilter}
        />
      ) : groupedByFolder ? (
        <FolderGroupedView
          groups={groupedByFolder}
          projectId={projectId}
          collapsed={collapsed}
          setCollapsed={setCollapsed}
          selected={selected}
          onToggleSelected={toggleSelected}
          editingRel={editingRel}
          editingName={editingName}
          setEditingRel={setEditingRel}
          setEditingName={setEditingName}
          onRename={handleRename}
          onOpenTag={openTagModal}
          tagsMap={tagsMap}
          highlightRels={highlightRels}
          forceExpand={!!search.trim() || !!tagFilter}
        />
      ) : (
        <FlatList
          files={visible}
          projectId={projectId}
          selected={selected}
          onToggleSelected={toggleSelected}
          editingRel={editingRel}
          editingName={editingName}
          setEditingRel={setEditingRel}
          setEditingName={setEditingName}
          onRename={handleRename}
          onOpenTag={openTagModal}
          tagsMap={tagsMap}
          highlightRels={highlightRels}
        />
      )}

      {/* ── Modals ── */}
      {mkdirOpen ? (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setMkdirOpen(false); }}>
          <div className="bg-surface-raised rounded-lg p-4 min-w-[340px] max-w-[90vw] shadow-strong" onClick={(e) => e.stopPropagation()}>
            <h4 className="m-0 mb-3 text-sm font-semibold flex items-center gap-1.5"><FolderPlus size={14} /> New Folder</h4>
            <p className="text-xs text-text-faint mb-2">Folder will be created under Working Documents</p>
            <div className="flex items-center gap-1.5">
              <span className="text-sm text-text-faint shrink-0">Working Documents /</span>
              <input
                data-tip="Enter the new folder path"
                value={mkdirPath}
                onChange={(e) => setMkdirPath(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') void handleMkdir(); if (e.key === 'Escape') setMkdirOpen(false); }}
                placeholder="My Folder"
                className="flex-1 py-1.5 px-2 border border-default rounded text-sm"
                autoFocus
              />
            </div>
            {opError ? <div className="text-xs text-danger mt-2">{opError}</div> : null}
            <div className="flex justify-end gap-2 mt-3">
              <button data-tip="Close without creating a folder" onClick={() => setMkdirOpen(false)} className="py-1 px-3 border border-default rounded text-sm bg-surface-muted">Cancel</button>
              {perms.canEdit ? <button data-tip="Create the new folder" onClick={() => void handleMkdir()} disabled={opBusy || !mkdirPath.trim()} className="py-1 px-3 bg-accent text-white border border-accent rounded text-sm disabled:opacity-50">{opBusy ? 'Creating…' : 'Create'}</button> : null}
            </div>
          </div>
        </div>
      ) : null}

      {moveOpen ? (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setMoveOpen(false); }}>
          <div className="bg-surface-raised rounded-lg p-4 min-w-[360px] max-w-[90vw] shadow-strong" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <h4 className="m-0 text-sm font-semibold flex items-center gap-1.5"><Move size={14} /> Move {selected.size} item{selected.size === 1 ? '' : 's'}</h4>
              <button
                data-tip={movePickerMode ? 'Switch to raw path input (allows rename)' : 'Switch to folder picker'}
                onClick={() => {
                  if (!movePickerMode) setMoveDst((prev) => toCanonicalRel(prev));
                  setMovePickerMode((m) => !m);
                }}
                className="text-xs text-link hover:underline px-2 py-1 border border-default rounded bg-surface-muted"
              >
                {movePickerMode ? '✎ Path' : 'Folder picker'}
              </button>
            </div>
            <div className="text-xs text-text-faint mb-2 truncate">{Array.from(selected).slice(0, 3).join(', ')}{selected.size > 3 ? ` +${selected.size - 3} more` : ''}</div>
            {movePickerMode ? (
              <>
                <p className="text-xs text-text-faint mb-1">Destination folder (each file keeps its name):</p>
                <select
                  data-tip="Choose the destination folder"
                  value={moveDst}
                  onChange={(e) => setMoveDst(e.target.value)}
                  className="w-full py-1.5 px-2 border border-default rounded text-sm bg-surface-raised"
                >
                  {writableFolders.map((f) => (
                    <option key={f} value={f}>{f}</option>
                  ))}
                </select>
                <input
                  data-tip="Optionally create a new subfolder"
                  value={moveNewSubfolder}
                  onChange={(e) => setMoveNewSubfolder(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') void handleMove(); if (e.key === 'Escape') setMoveOpen(false); }}
                  placeholder="New subfolder (optional, e.g. a/b)"
                  className="w-full mt-2 py-1.5 px-2 border border-default rounded text-sm"
                />
                <p className="text-xs text-text-faint mt-1">Files will be moved to <code>{moveDst}{moveNewSubfolder.trim() ? '/' + moveNewSubfolder.trim().replace(/\\/g,'/').replace(/^\/+/,'') : ''}</code></p>
              </>
            ) : (
              <>
                <p className="text-xs text-text-faint mb-1">{selected.size === 1 ? 'Destination path (rename or move):' : 'Destination folder (each file keeps its name):'}</p>
                <div className="flex items-center gap-1.5">
                  <span className="text-sm text-text-faint shrink-0">Working Documents /</span>
                  <input
                    data-tip={selected.size === 1 ? 'Enter the new name or destination' : 'Enter the destination folder'}
                    value={moveDst.startsWith(CANONICAL + '/') ? moveDst.slice(CANONICAL.length + 1) : moveDst === CANONICAL ? '' : moveDst}
                    onChange={(e) => setMoveDst(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void handleMove(); if (e.key === 'Escape') setMoveOpen(false); }}
                    placeholder={selected.size === 1 ? 'new-name.txt' : 'Target Folder'}
                    className="flex-1 py-1.5 px-2 border border-default rounded text-sm"
                    autoFocus
                  />
                </div>
              </>
            )}
            {opError ? <div className="text-xs text-danger mt-2">{opError}</div> : null}
            <div className="flex justify-end gap-2 mt-3">
              <button data-tip="Close without moving files" onClick={() => { setMoveOpen(false); setMoveNewSubfolder(''); }} className="py-1 px-3 border border-default rounded text-sm bg-surface-muted">Cancel</button>
              {perms.canEdit ? <button data-tip="Move the selected files" onClick={() => void handleMove()} disabled={opBusy || !moveDst.trim()} className="py-1 px-3 bg-accent text-white border border-accent rounded text-sm disabled:opacity-50">{opBusy ? 'Moving…' : 'Move'}</button> : null}
            </div>
          </div>
        </div>
      ) : null}

      {copyOpen ? (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setCopyOpen(false); }}>
          <div className="bg-surface-raised rounded-lg p-4 min-w-[360px] max-w-[90vw] shadow-strong" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <h4 className="m-0 text-sm font-semibold flex items-center gap-1.5"><Copy size={14} /> Copy {selected.size} item{selected.size === 1 ? '' : 's'}</h4>
              <button
                data-tip={copyPickerMode ? 'Switch to raw path input (allows rename)' : 'Switch to folder picker'}
                onClick={() => {
                  if (!copyPickerMode) setCopyDst((prev) => toCanonicalRel(prev));
                  setCopyPickerMode((m) => !m);
                }}
                className="text-xs text-link hover:underline px-2 py-1 border border-default rounded bg-surface-muted"
              >
                {copyPickerMode ? '✎ Path' : 'Folder picker'}
              </button>
            </div>
            <div className="text-xs text-text-faint mb-2 truncate">{Array.from(selected).slice(0, 3).join(', ')}{selected.size > 3 ? ` +${selected.size - 3} more` : ''}</div>
            {copyPickerMode ? (
              <>
                <p className="text-xs text-text-faint mb-1">Destination folder:</p>
                <select
                  data-tip="Choose the destination folder"
                  value={copyDst}
                  onChange={(e) => setCopyDst(e.target.value)}
                  className="w-full py-1.5 px-2 border border-default rounded text-sm bg-surface-raised"
                >
                  {writableFolders.map((f) => (
                    <option key={f} value={f}>{f}</option>
                  ))}
                </select>
                <input
                  data-tip="Optionally create a new subfolder"
                  value={copyNewSubfolder}
                  onChange={(e) => setCopyNewSubfolder(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') void handleCopy(); if (e.key === 'Escape') setCopyOpen(false); }}
                  placeholder="New subfolder (optional, e.g. a/b)"
                  className="w-full mt-2 py-1.5 px-2 border border-default rounded text-sm"
                />
                <p className="text-xs text-text-faint mt-1">Files will be copied to <code>{copyDst}{copyNewSubfolder.trim() ? '/' + copyNewSubfolder.trim().replace(/\\/g,'/').replace(/^\/+/,'') : ''}</code></p>
              </>
            ) : (
              <>
                <p className="text-xs text-text-faint mb-1">{selected.size === 1 ? 'Destination path:' : 'Destination folder:'}</p>
                <div className="flex items-center gap-1.5">
                  <span className="text-sm text-text-faint shrink-0">Working Documents /</span>
                  <input
                    data-tip={selected.size === 1 ? 'Enter the new name or destination' : 'Enter the destination folder'}
                    value={copyDst.startsWith(CANONICAL + '/') ? copyDst.slice(CANONICAL.length + 1) : copyDst === CANONICAL ? '' : copyDst}
                    onChange={(e) => setCopyDst(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void handleCopy(); if (e.key === 'Escape') setCopyOpen(false); }}
                    placeholder={selected.size === 1 ? 'copy-name.txt' : 'Target Folder'}
                    className="flex-1 py-1.5 px-2 border border-default rounded text-sm"
                    autoFocus
                  />
                </div>
              </>
            )}
            {opError ? <div className="text-xs text-danger mt-2">{opError}</div> : null}
            <div className="flex justify-end gap-2 mt-3">
              <button data-tip="Close without copying files" onClick={() => { setCopyOpen(false); setCopyNewSubfolder(''); }} className="py-1 px-3 border border-default rounded text-sm bg-surface-muted">Cancel</button>
              {perms.canEdit ? <button data-tip="Copy the selected files" onClick={() => void handleCopy()} disabled={opBusy || !copyDst.trim()} className="py-1 px-3 bg-accent text-white border border-accent rounded text-sm disabled:opacity-50">{opBusy ? 'Copying…' : 'Copy'}</button> : null}
            </div>
          </div>
        </div>
      ) : null}

      {tagModal ? (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setTagModal(null); }}>
          <div className="bg-surface-raised rounded-lg p-4 min-w-[380px] max-w-[90vw] shadow-strong" onClick={(e) => e.stopPropagation()}>
            <h4 className="m-0 mb-3 text-sm font-semibold flex items-center gap-1.5"><Tag size={14} /> Edit tags</h4>
            <div className="text-xs text-text-faint mb-2 truncate" title={tagModal.rel}>{tagModal.rel}</div>
            <label className="block text-xs font-medium mb-1">Tags (comma-separated)</label>
            <input
              data-tip="Enter comma-separated tags"
              value={tagModal.tags}
              onChange={(e) => setTagModal({ ...tagModal, tags: e.target.value })}
              placeholder="e.g. invoice, finance, 2024"
              className="w-full py-1.5 px-2 border border-default rounded text-sm mb-3"
            />
            <label className="block text-xs font-medium mb-1">Note (optional)</label>
            <textarea
              data-tip="Add an optional note for this file"
              value={tagModal.note}
              onChange={(e) => setTagModal({ ...tagModal, note: e.target.value })}
              placeholder="Optional note for this file"
              rows={3}
              className="w-full py-1.5 px-2 border border-default rounded text-sm resize-y"
            />
            {opError ? <div className="text-xs text-danger mt-2">{opError}</div> : null}
            <div className="flex justify-end gap-2 mt-3">
              <button data-tip="Close without saving tags" onClick={() => setTagModal(null)} className="py-1 px-3 border border-default rounded text-sm bg-surface-muted">Cancel</button>
              {perms.canEdit ? <button data-tip="Save tags and note" onClick={() => void handleSaveTags()} disabled={opBusy} className="py-1 px-3 bg-accent text-white border border-accent rounded text-sm disabled:opacity-50">{opBusy ? 'Saving…' : 'Save'}</button> : null}
            </div>
          </div>
        </div>
      ) : null}

      {/* ── WebDAV modal (Lane C) ── */}
      {webdavOpen ? (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[200]" onClick={(e) => { if (e.target === e.currentTarget) setWebdavOpen(false); }}>
          <div className="bg-surface-raised rounded-lg p-5 min-w-[520px] max-w-[92vw] max-h-[85vh] overflow-y-auto shadow-strong" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-3">
              <h4 className="m-0 text-sm font-semibold flex items-center gap-1.5"><Globe size={16} /> WebDAV — Direct File Access</h4>
              <button data-tip="Close the WebDAV dialog" onClick={() => setWebdavOpen(false)} className="p-1 text-text-faint hover:text-ink rounded"><X size={16} /></button>
            </div>
            {!resolvedSlug ? (
              <div className="text-sm text-text-faint">No project slug found. Open a project first.</div>
            ) : (() => {
              const { primary, alt, davHost } = davUrls(resolvedSlug);
              const rootUrl = primary.replace('Working%20Documents/', '');
              return (
                <>
                  <p className="text-xs text-text-faint mb-3">
                    Mount this project&apos;s <code>Working Documents</code> directly in your OS file manager.
                    Writes are confined to <code>Working Documents</code> variants and enforce the same security checks as the in-app File Manager.
                  </p>
                  <div className="space-y-2">
                    <div>
                      <div className="text-xs font-medium mb-1">Primary URL (same host)</div>
                      <div className="flex items-center gap-1.5">
                        <code className="flex-1 text-xs bg-surface-muted border border-default rounded px-2 py-1.5 break-all">{primary}</code>
                        <button
                          data-tip="Copy the WebDAV URL to clipboard"
                          onClick={async () => {
                            try { await navigator.clipboard.writeText(primary); setWebdavCopied(true); setTimeout(() => setWebdavCopied(false), 2000); } catch {}
                          }}
                          className="shrink-0 py-1 px-2 text-xs border border-default rounded inline-flex items-center gap-1 bg-surface-muted hover:bg-surface-raised"
                        >
                          {webdavCopied ? <ClipboardCheck size={12} /> : <Clipboard size={12} />} {webdavCopied ? 'Copied' : 'Copy URL'}
                        </button>
                      </div>
                      <div className="text-xs text-text-faint mt-1">Project root: <code>{rootUrl}</code></div>
                    </div>
                    <div>
                      <div className="text-xs font-medium mb-1">Alternative (if a <code>{davHost}</code> host is configured)</div>
                      <code className="block text-xs bg-surface-muted border border-default rounded px-2 py-1.5 break-all">{alt}</code>
                    </div>
                    <div className="text-xs text-warning bg-warning/10 border-l-2 border-warning rounded px-2 py-1.5">
                      Auth: use the <strong>DAV token</strong> as your password (username can be anything). Set <code>AINGEL_DAV_TOKEN</code> on the server; the browser session does <strong>not</strong> authenticate WebDAV mounts. Tags exposed as <code>aingel:tags</code> / <code>aingel:note</code> deadprops.
                    </div>
                  </div>
                  <div className="mt-4 space-y-3">
                    <h5 className="text-xs font-semibold">Mount instructions</h5>
                    <div className="text-xs space-y-2">
                      <div>
                        <div className="font-medium">Windows</div>
                        <div className="text-text-faint">File Explorer → This PC → Map network drive → Folder:</div>
                        <code className="block bg-surface-muted border border-default rounded px-2 py-1 mt-1 break-all">{primary}</code>
                        <div className="text-text-faint mt-1">or CMD: <code>net use X: {primary}</code></div>
                      </div>
                      <div>
                        <div className="font-medium">macOS</div>
                        <div className="text-text-faint">Finder → Go → Connect to Server (⌘K):</div>
                        <code className="block bg-surface-muted border border-default rounded px-2 py-1 mt-1 break-all">{primary}</code>
                      </div>
                      <div>
                        <div className="font-medium">Linux</div>
                        <div className="text-text-faint"><code>gio mount {primary.replace(/^http/, 'dav')}</code></div>
                        <div className="text-text-faint">or <code>mount -t davfs {primary} /mnt/cordee</code> (needs <code>davfs2</code>)</div>
                      </div>
                    </div>
                    <div className="text-xs text-text-faint border-t border-default pt-2">
                      <a href={primary} target="_blank" rel="noopener" className="text-link hover:underline inline-flex items-center gap-1">
                        <ExternalLink size={11} /> Open in browser (PROPFIND listing)
                      </a>
                      <span className="mx-1">·</span>
                      <span>Depth: 0/1, single file &amp; folder ops via PUT/MKCOL/MOVE/COPY/DELETE.</span>
                    </div>
                  </div>
                </>
              );
            })()}
            <div className="flex justify-end mt-4">
              <button data-tip="Close the WebDAV dialog" onClick={() => setWebdavOpen(false)} className="py-1 px-3 border border-default rounded text-sm bg-surface-muted">Close</button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
};

// ── Sub-views ─────────────────────────────────────────────────────────────

const FolderGroupedView = ({ groups, projectId, collapsed, setCollapsed, selected, onToggleSelected, editingRel, editingName, setEditingRel, setEditingName, onRename, onOpenTag, tagsMap, highlightRels, forceExpand }: {
  groups: [string, FileEntry[]][];
  projectId: number;
  collapsed: Record<string, boolean>;
  setCollapsed: React.Dispatch<React.SetStateAction<Record<string, boolean>>>;
  selected: Set<string>;
  onToggleSelected: (rel: string) => void;
  editingRel: string | null;
  editingName: string;
  setEditingRel: (v: string | null) => void;
  setEditingName: (v: string) => void;
  onRename: (oldRel: string) => void;
  onOpenTag: (f: FileEntry) => void;
  tagsMap?: Record<string, { tags: string[]; note: string }>;
  highlightRels?: Set<string>;
  forceExpand?: boolean;
}) => {
  return (
    <div className="flex flex-col gap-2">
      {groups.map(([folder, folderFiles]) => {
        const isCollapsed = forceExpand ? false : collapsed[folder];
        const totalSize = folderFiles.reduce((s, f) => s + f.size, 0);
        const label = folder || '(project root)';
        return (
          <div key={folder || '__root'} className="border border-border-muted rounded bg-surface-soft">
            <button
              data-tip={isCollapsed ? 'Expand this folder' : 'Collapse this folder'}
              onClick={() => setCollapsed((c) => ({ ...c, [folder]: !c[folder] }))}
              className="w-full flex items-center gap-1.5 py-1.5 px-2 text-sm bg-surface-muted hover:bg-surface-raised border-none cursor-pointer text-left"
              aria-expanded={!isCollapsed}
            >
              {isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
              <Folder size={12} className="text-text-soft" />
              <span className="font-medium flex-1">{label}</span>
              <span className="text-xs text-text-faint">
                {folderFiles.length} file{folderFiles.length === 1 ? '' : 's'} · {fmtBytes(totalSize)}
              </span>
            </button>
            {!isCollapsed ? (
              <div className="p-1">
                <FlatList files={folderFiles} projectId={projectId} selected={selected} onToggleSelected={onToggleSelected} editingRel={editingRel} editingName={editingName} setEditingRel={setEditingRel} setEditingName={setEditingName} onRename={onRename} onOpenTag={onOpenTag} tagsMap={tagsMap} highlightRels={highlightRels} />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
};

const TaskGroupedView = ({ groups, projectId, onSelectTask, collapsed, setCollapsed, selected, onToggleSelected, editingRel, editingName, setEditingRel, setEditingName, onRename, onOpenTag, tagsMap, highlightRels, forceExpand }: {
  groups: TaskFileGroup[];
  projectId: number;
  onSelectTask?: (id: number) => void;
  collapsed: Record<string, boolean>;
  setCollapsed: React.Dispatch<React.SetStateAction<Record<string, boolean>>>;
  selected: Set<string>;
  onToggleSelected: (rel: string) => void;
  editingRel: string | null;
  editingName: string;
  setEditingRel: (v: string | null) => void;
  setEditingName: (v: string) => void;
  onRename: (oldRel: string) => void;
  onOpenTag: (f: FileEntry) => void;
  tagsMap?: Record<string, { tags: string[]; note: string }>;
  highlightRels?: Set<string>;
  forceExpand?: boolean;
}) => {
  const setActiveProject = useStore((s) => s.setActiveProject);
  const setActiveMainTab = useStore((s) => s.setActiveMainTab);
  const handleTask = (id: number) => {
    if (onSelectTask) onSelectTask(id);
    setActiveProject(projectId);
    setActiveMainTab('board');
  };
  return (
    <div className="flex flex-col gap-2">
      {groups.map((g, idx) => {
        const key = g.task_id !== null ? `t${g.task_id}` : `g${idx}`;
        const isCollapsed = forceExpand ? false : collapsed[key];
        const totalSize = g.files.reduce((s, f) => s + f.size, 0);
        return (
          <div key={key} className="border border-border-muted rounded bg-surface-soft">
            <div className="flex items-center gap-1.5 py-1.5 px-2 text-sm bg-surface-muted border-b border-border-muted">
              <button
                data-tip={isCollapsed ? 'Expand this task group' : 'Collapse this task group'}
                onClick={() => setCollapsed((c) => ({ ...c, [key]: !c[key] }))}
                className="inline-flex items-center gap-1 border-none bg-transparent cursor-pointer text-ink p-0"
                aria-expanded={!isCollapsed}
              >
                {isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                <ListChecks size={12} className="text-text-soft" />
              </button>
              {g.task_id !== null ? (
                <button
                  data-tip={`Open task #${g.task_id} on the board`}
                  onClick={() => handleTask(g.task_id!)}
                  className="flex-1 text-left font-medium text-sm cursor-pointer bg-transparent border-none p-0 text-link hover:underline"
                >
                  #{g.task_id} · {g.task_title || 'Untitled task'}
                </button>
              ) : (
                <span className="flex-1 font-medium text-sm">{g.task_title}</span>
              )}
              <span className="text-xs text-text-faint inline-flex items-center gap-1">
                {g.finished_at ? <><Calendar size={11} /> {fmtRelative(g.finished_at)}</> : null}
                {' · '}
                {g.files.length} file{g.files.length === 1 ? '' : 's'} · {fmtBytes(totalSize)}
              </span>
            </div>
            {!isCollapsed ? (
              <div className="p-1">
                <FlatList files={g.files} projectId={projectId} selected={selected} onToggleSelected={onToggleSelected} editingRel={editingRel} editingName={editingName} setEditingRel={setEditingRel} setEditingName={setEditingName} onRename={onRename} onOpenTag={onOpenTag} tagsMap={tagsMap} highlightRels={highlightRels} />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
};

const FlatList = ({ files, projectId, selected, onToggleSelected, editingRel, editingName, setEditingRel, setEditingName, onRename, onOpenTag, tagsMap, highlightRels }: {
  files: FileEntry[];
  projectId: number;
  selected: Set<string>;
  onToggleSelected: (rel: string) => void;
  editingRel: string | null;
  editingName: string;
  setEditingRel: (v: string | null) => void;
  setEditingName: (v: string) => void;
  onRename: (rel: string) => void;
  onOpenTag: (f: FileEntry) => void;
  tagsMap?: Record<string, { tags: string[]; note: string }>;
  highlightRels?: Set<string>;
}) => {
  return (
    <div className="flex flex-col gap-0.5">
      {files.map((f) => (
        <FileRow key={f.path} file={f} projectId={projectId} checked={selected.has(f.rel)} onToggle={() => onToggleSelected(f.rel)} editing={editingRel === f.rel} editingName={editingName} setEditingName={setEditingName} onStartEdit={() => { setEditingRel(f.rel); setEditingName(f.name); }} onConfirmEdit={() => onRename(f.rel)} onCancelEdit={() => setEditingRel(null)} onOpenTag={() => onOpenTag(f)} tagsMap={tagsMap} highlighted={highlightRels?.has(f.rel)} />
      ))}
    </div>
  );
};

const FileRow = ({ file, projectId, checked, onToggle, editing, editingName, setEditingName, onStartEdit, onConfirmEdit, onCancelEdit, onOpenTag, tagsMap, highlighted }: {
  file: FileEntry;
  projectId: number;
  checked: boolean;
  onToggle: () => void;
  editing: boolean;
  editingName: string;
  setEditingName: (v: string) => void;
  onStartEdit: () => void;
  onConfirmEdit: () => void;
  onCancelEdit: () => void;
  onOpenTag: () => void;
  tagsMap?: Record<string, { tags: string[]; note: string }>;
  highlighted?: boolean;
}) => {
  const perms = useProjectPermissions(projectId);
  const url = relUrl(file.rel, projectId);
  const title = file.rel !== file.name ? file.rel : file.name;
  const tags = file.tags ?? tagsMap?.[file.rel]?.tags ?? [];
  const note = file.note ?? tagsMap?.[file.rel]?.note ?? '';
  const meta = (
    <>
      <span>{fmtBytes(file.size)}</span>
      {file.modified ? <span className="text-text-faint"> · {fmtRelative(file.modified)}</span> : null}
      {file.category === 'code' ? <span className="text-accent"> · code</span> : null}
      {file.category === 'archive' ? <span className="text-warning"> · legacy</span> : null}
      {file.category === 'system' ? <span className="text-text-faint"> · system</span> : null}
    </>
  );

  const isWritable = file.category === 'reference';
  const rowInner = (
    <div className="flex items-center gap-1.5">
      {isWritable ? (
        <input data-tip="Select this file for actions" type="checkbox" checked={checked} onChange={onToggle} onClick={(e) => e.stopPropagation()} className="shrink-0" aria-label={`Select ${file.rel}`} />
      ) : (
        <span title="Read-only — managed by Cordée" className="shrink-0 inline-flex"><Lock size={12} className="text-text-faint" /></span>
      )}
      {extIcon(file.name)}
      {editing ? (
        <span className="flex-1 flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
          <input
            data-tip="Enter the new file name"
            value={editingName}
            onChange={(e) => setEditingName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') onConfirmEdit(); if (e.key === 'Escape') onCancelEdit(); }}
            className="flex-1 py-0.5 px-1 border border-accent rounded text-sm bg-surface-raised"
            autoFocus
          />
          {perms.canEdit ? <button data-tip="Confirm rename" onClick={(e) => { e.stopPropagation(); onConfirmEdit(); }} className="p-1 text-success hover:bg-success/10 rounded"><Check size={13} /></button> : null}
          <button data-tip="Cancel rename" onClick={(e) => { e.stopPropagation(); onCancelEdit(); }} className="p-1 text-text-faint hover:bg-surface-muted rounded"><X size={13} /></button>
        </span>
      ) : (
        <button
          data-tip={perms.canEdit ? 'Click to rename (move)' : undefined}
          onClick={(e) => { e.stopPropagation(); if (perms.canEdit) onStartEdit(); }}
          className={cn('flex-1 truncate font-medium text-left bg-transparent border-none p-0 text-sm', perms.canEdit && 'cursor-pointer hover:text-link hover:underline')}
        >
          {file.name}
        </button>
      )}
      <span className="text-xs text-text-faint inline-flex items-center gap-1 shrink-0">{meta}</span>
    </div>
  );

  const tagChips = tags.length > 0 ? (
    <div className="flex flex-wrap gap-1 mt-0.5 pl-5">
      {tags.map((t) => (
        <span key={t} className="text-2xs py-0.5 px-1.5 bg-accent/15 text-accent border border-accent/20 rounded-full">{t}</span>
      ))}
      {note ? <span className="text-2xs text-text-faint italic truncate max-w-[200px]" title={note}>— {note}</span> : null}
    </div>
  ) : note ? (
    <div className="text-2xs text-text-faint italic pl-5 truncate max-w-[260px]" title={note}>— {note}</div>
  ) : null;

  const tagButton = perms.canEdit ? (
    <button
      data-tip={tags.length ? `Tags: ${tags.join(', ')}${note ? ' — ' + note : ''} (click to edit)` : 'Add tags'}
      onClick={(e) => { e.stopPropagation(); onOpenTag(); }}
      className="ml-1 p-0.5 text-text-faint hover:text-accent hover:bg-accent-soft rounded"
    >
      <Tag size={12} />
    </button>
  ) : null;

  const actionsRow = (
    <div className="flex items-center gap-0.5 ml-1 shrink-0">
      {tagButton}
      {perms.canEdit && !editing ? (
        <button
          data-tip="Rename this file"
          onClick={(e) => { e.stopPropagation(); onStartEdit(); }}
          className="p-0.5 text-text-faint hover:text-ink hover:bg-surface-muted rounded"
        >
          <Edit2 size={11} />
        </button>
      ) : null}
    </div>
  );

  if (!url) {
    return (
      <div data-rel={file.rel} className={cn('files-row py-1 px-2 rounded text-sm flex flex-col hover:bg-surface-muted', checked && 'bg-accent-soft', highlighted && 'bg-yellow-100 dark:bg-yellow-900/30 ring-1 ring-yellow-400 animate-pulse')} title={title}>
        <div className="flex items-center gap-1.5 w-full">
          <div className="flex-1 min-w-0">{rowInner}</div>
          {actionsRow}
        </div>
        {file.rel !== file.name ? (
          <div className="text-xs text-text-faint truncate pl-5" title={file.rel}>{file.rel}</div>
        ) : null}
        {tagChips}
      </div>
    );
  }
  return (
    <div data-rel={file.rel} className={cn('files-row py-1 px-2 rounded text-sm flex flex-col hover:bg-surface-muted', checked && 'bg-accent-soft', highlighted && 'bg-yellow-100 dark:bg-yellow-900/30 ring-1 ring-yellow-400 animate-pulse')}>
      <div className="flex items-center gap-1.5 w-full">
        <div className="flex-1 min-w-0">
          {/* Keep checkbox + rename separate from link to avoid nested interactive */}
          <div className="flex items-center gap-1.5">
            {isWritable ? (
              <input data-tip="Select this file for actions" type="checkbox" checked={checked} onChange={onToggle} onClick={(e) => e.stopPropagation()} className="shrink-0" aria-label={`Select ${file.rel}`} />
            ) : (
              <span title="Read-only — managed by Cordée" className="shrink-0 inline-flex"><Lock size={12} className="text-text-faint" /></span>
            )}
            {extIcon(file.name)}
            {editing ? (
              <span className="flex-1 flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                <input
                  data-tip="Rename this file or folder"
                  value={editingName}
                  onChange={(e) => setEditingName(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') onConfirmEdit(); if (e.key === 'Escape') onCancelEdit(); }}
                  className="flex-1 py-0.5 px-1 border border-accent rounded text-sm bg-surface-raised"
                  autoFocus
                />
                {perms.canEdit ? <button data-tip="Confirm rename" onClick={(e) => { e.stopPropagation(); onConfirmEdit(); }} className="p-1 text-success hover:bg-success/10 rounded"><Check size={13} /></button> : null}
                <button data-tip="Cancel rename" onClick={(e) => { e.stopPropagation(); onCancelEdit(); }} className="p-1 text-text-faint hover:bg-surface-muted rounded"><X size={13} /></button>
              </span>
            ) : (
              <a href={url} target="_blank" rel="noopener" className="flex-1 truncate font-medium text-link hover:underline text-sm" title={title} onClick={(e) => e.stopPropagation()}>
                {file.name}
              </a>
            )}
            <span className="text-xs text-text-faint inline-flex items-center gap-1 shrink-0">{meta}</span>
          </div>
        </div>
        {actionsRow}
      </div>
      {file.rel !== file.name ? (
        <div className="text-xs text-text-faint truncate pl-5" title={file.rel}>{file.rel}</div>
      ) : null}
      {tagChips ? <div onClick={perms.canEdit ? (e) => { e.stopPropagation(); onOpenTag(); } : undefined} className={cn(perms.canEdit && 'cursor-pointer')}>{tagChips}</div> : null}
    </div>
  );
};

const EmptyState = ({ category, search, onClearSearch, showSystem, onShowSystem }: {
  category: CategoryKey;
  search: string;
  onClearSearch: () => void;
  showSystem: boolean;
  onShowSystem: () => void;
}) => {
  const label = CATEGORIES.find((c) => c.key === category)?.label || 'files';
  if (search) {
    return (
      <div className="files-empty py-3 px-2 text-sm text-text-muted text-center">
        <Search size={20} className="text-text-faint mx-auto mb-1" />
        No files match <span className="font-medium text-ink">"{search}"</span> in {label}.
        <div className="mt-1.5">
          <button data-tip="Clear the search query" onClick={onClearSearch} className="text-link hover:underline text-sm">Clear search</button>
        </div>
      </div>
    );
  }
  if (category === 'system') {
    return (
      <div className="files-empty py-3 px-2 text-sm text-text-muted text-center">
        <HardDrive size={20} className="text-text-faint mx-auto mb-1" />
        No system files in this project.
      </div>
    );
  }
  if (category === 'reference') {
    return (
      <div className="files-empty py-3 px-2 text-sm text-text-muted text-center">
        <Folder size={20} className="text-text-faint mx-auto mb-1" />
        No reference materials in a Working Docs folder yet.
        <div className="text-xs text-text-faint mt-1">Drop files into My Docs / Working Docs / Working Documents.</div>
      </div>
    );
  }
  if (category === 'deliverable' || category === 'output' || category === 'code') {
    return (
      <div className="files-empty py-3 px-2 text-sm text-text-muted text-center">
        <Inbox size={20} className="text-text-faint mx-auto mb-1" />
        No task outputs yet.
        <div className="text-xs text-text-faint mt-1">Run and approve a task — its files will appear here, grouped by task.</div>
      </div>
    );
  }
  return (
    <div className="files-empty py-3 px-2 text-sm text-text-muted text-center">
      <Inbox size={20} className="text-text-faint mx-auto mb-1" />
      No files in {label}.
      {category === 'all' && !showSystem ? (
        <div className="text-xs text-text-faint mt-1">
          <button data-tip="Show hidden system files" onClick={onShowSystem} className="text-link hover:underline">Show system files</button>
        </div>
      ) : null}
    </div>
  );
};
