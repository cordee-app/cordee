// Board search matching. Pure helpers so the filter stays testable and has no
// dependency on the store or the DOM.

import type { Task } from '../types';

/** Normalise for matching: lowercase, strip diacritics, collapse whitespace. */
export function normalizeForSearch(s: string): string {
  return (s ?? '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

/** True when every whitespace-separated term of `query` occurs in `text`.
 *  Empty query matches everything; a leading '#' on a term is ignored. */
export function matchesQuery(text: string, query: string): boolean {
  const terms = normalizeForSearch(query).split(' ').filter(Boolean);
  if (!terms.length) return true;
  const haystack = normalizeForSearch(text ?? '');
  return terms.every((raw) => {
    const term = raw.replace(/^#+/, '');
    return !term || haystack.includes(term);
  });
}

/** True when every whitespace-separated term of `query` occurs in the task's
 *  title, description, display code, numeric id, or phase name. Empty query
 *  matches everything. A leading '#' on a term (copied task chip) is ignored. */
export function matchesTaskQuery(task: Task, query: string): boolean {
  return matchesQuery(
    [
      task.title ?? '',
      task.description ?? '',
      task.display_code ?? '',
      String(task.id ?? ''),
      task.phase_name ?? '',
    ].join(' '),
    query
  );
}
