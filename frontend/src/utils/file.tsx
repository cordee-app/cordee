/**
 * Shared file-display helpers used by MemoryPanel, FilesBrowser,
 * ExecutionLog, and TaskCard. Centralizes the file→URL mapping, size
 * formatting, and extension icon so the components agree on visibility
 * rules and accessibility.
 */
import React from 'react';
import {
  FileText, Globe, Settings, Wrench, BookOpen,
  Brain, MessageSquare, Paperclip, FileCode, FileImage,
  FileSpreadsheet, FileArchive, FileMusic, FileVideo,
} from 'lucide-react';

// Browser-renderable types (raw /files/... stream). Sourced from
// agent_filecat.RENDERABLE_EXTS on the backend.
export const RENDERABLE_EXTS = new Set([
  '.md', '.markdown', '.txt', '.log', '.html', '.htm',
  '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp',
  '.pdf', '.csv', '.json', '.xml', '.yaml', '.yml',
  '.css', '.js', '.ts', '.py', '.sh',
]);

// Office formats that need server-side text extraction via /api/preview.
export const NON_RENDERABLE_EXTS = new Set([
  '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt',
  '.odt', '.ods', '.odp',
]);

/** Convert bytes → human string. Supports B / KB / MB / GB. */
export const fmtBytes = (b: number): string => {
  if (!b || b < 0) return '0 B';
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  return `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`;
};

/** Convert ISO timestamp → short relative string (e.g. "3d ago", "2h ago"). */
export const fmtRelative = (iso: string | undefined | null): string => {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '';
  const diff = Math.max(0, Date.now() - t);
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
};

/** Pick a lucide icon for the file name (extension-based, color + icon). */
export const extIcon = (name: string): React.ReactNode => {
  const ext = name.slice(name.lastIndexOf('.')).toLowerCase();
  const cls = 'shrink-0 text-text-soft';
  switch (ext) {
    case '.pdf':
      return <FileText size={14} className={`${cls} text-danger`} />;
    case '.docx':
    case '.doc':
    case '.odt':
      return <FileText size={14} className={`${cls} text-link`} />;
    case '.xlsx':
    case '.xls':
    case '.ods':
    case '.csv':
      return <FileSpreadsheet size={14} className={`${cls} text-success`} />;
    case '.pptx':
    case '.ppt':
    case '.odp':
      return <FileText size={14} className={`${cls} text-warning`} />;
    case '.txt':
    case '.log':
    case '.md':
    case '.markdown':
      return <FileText size={14} className={cls} />;
    case '.jpg':
    case '.jpeg':
    case '.png':
    case '.gif':
    case '.webp':
    case '.svg':
    case '.bmp':
      return <FileImage size={14} className={`${cls} text-link`} />;
    case '.html':
    case '.htm':
      return <Globe size={14} className={cls} />;
    case '.py':
    case '.js':
    case '.ts':
    case '.jsx':
    case '.tsx':
    case '.go':
    case '.rs':
    case '.java':
    case '.rb':
    case '.php':
    case '.c':
    case '.cpp':
    case '.cc':
    case '.h':
    case '.hpp':
    case '.cs':
    case '.swift':
    case '.kt':
    case '.scala':
    case '.sh':
    case '.sql':
    case '.dart':
    case '.lua':
    case '.css':
    case '.scss':
    case '.vue':
    case '.svelte':
      return <FileCode size={14} className={`${cls} text-accent`} />;
    case '.zip':
    case '.tar':
    case '.gz':
    case '.7z':
    case '.rar':
      return <FileArchive size={14} className={cls} />;
    case '.mp3':
    case '.wav':
    case '.ogg':
    case '.flac':
      return <FileMusic size={14} className={cls} />;
    case '.mp4':
    case '.mov':
    case '.avi':
    case '.webm':
      return <FileVideo size={14} className={cls} />;
    case '.bat':
      return <Settings size={14} className={cls} />;
    default:
      return <Paperclip size={14} className={cls} />;
  }
};

/** Pick an icon by logical memory type (memory/chat/output). */
export const memoryIcon = (fileType: string | undefined): React.ReactNode => {
  const cls = 'shrink-0 text-text-soft';
  if (fileType === 'chat') return <MessageSquare size={14} className={cls} />;
  if (fileType === 'output') return <FileText size={14} className={cls} />;
  return <Brain size={14} className={cls} />;
};

/** Pick an icon for a definition file. */
export const defIcon = (name: string): React.ReactNode => {
  const cls = 'shrink-0 text-text-soft';
  if (name === 'Skills.md') return <Wrench size={14} className={cls} />;
  if (name === 'GUIDE.md') return <BookOpen size={14} className={cls} />;
  return <BookOpen size={14} className={cls} />;
};

/** Resolve an absolute project path to a user-facing URL. */
export const fileUrl = (
  absPath: string,
  projectRoot: string,
  projectId: number | null,
): string | null => {
  if (!absPath || !projectRoot || projectId === null) return null;
  const root = projectRoot.endsWith('/') ? projectRoot : projectRoot + '/';
  if (!absPath.startsWith(root)) return null;
  const rel = absPath.slice(root.length);
  const encoded = rel.split('/').map(encodeURIComponent).join('/');
  const ext = rel.slice(rel.lastIndexOf('.')).toLowerCase();
  if (NON_RENDERABLE_EXTS.has(ext)) {
    return `/api/preview/${projectId}/${encoded}`;
  }
  return `/files/${projectId}/${encoded}`;
};

/** Same as fileUrl but built from a repo-relative path (no root compare). */
export const relUrl = (rel: string, projectId: number | null): string | null => {
  if (!rel || projectId === null) return null;
  const encoded = rel.split('/').map(encodeURIComponent).join('/');
  const ext = rel.slice(rel.lastIndexOf('.')).toLowerCase();
  if (NON_RENDERABLE_EXTS.has(ext)) {
    return `/api/preview/${projectId}/${encoded}`;
  }
  return `/files/${projectId}/${encoded}`;
};
