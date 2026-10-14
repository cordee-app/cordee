// Human-facing task identifiers. Internal numeric ids stay the keys/URLs/store
// values everywhere — these chips are presentation only.

/** Chip for a task row: display code when backfilled, else the raw id. */
export function taskChip(t: { id: number; display_code?: string | null }): string {
  return t.display_code ? `#${t.display_code}` : `#${t.id}`;
}

/** Chip for a bare task id, resolving the display code from a task list. */
export function taskChipById(id: number, tasks?: { id: number; display_code?: string | null }[]): string {
  return taskChip(tasks?.find((t) => t.id === id) ?? { id });
}

/** Chip for an execution row: "code·E<n>" when present, else today's "#<task_id>". */
export function execChip(e: { task_id: number; task_display_code?: string | null; exec_no?: number | null }): string {
  if (!e.task_id) return '—';
  if (!e.task_display_code) return `#${e.task_id}`;
  return `#${e.task_display_code}${e.exec_no != null ? `\u00b7E${e.exec_no}` : ''}`;
}

/** Display-code shape (e.g. RP-014) — dependency inputs accept id or code. */
export const TASK_CODE_RE = /^[A-Z][A-Z0-9]{1,5}-\d+$/;

export function isTaskCode(s: string): boolean {
  return TASK_CODE_RE.test(s);
}
