// Dependency gate (deps-done rule): a task whose dependencies aren't all
// 'done' refuses to run — no execution row, task stays confirmed. These
// helpers format and parse the `unmet` payload the backend attaches to every
// refusal surface (409 dep-add, run refusal, SSE dep_blocked, PATCH slot-assign
// warning) so the UI speaks the same language everywhere.

import { api } from '../api';
import type { UnmetDep } from '../types';

/** Chip-like code for an unmet dep: display code when known, else the raw id. */
export function unmetCode(u: UnmetDep): string {
  return u.display_code || `#${u.id}`;
}

/** Short badge/toast label: display code when known (the project-edges payload
 *  lacks it — the store enriches it from the tasks in view), else the dep's
 *  title (cross-project deps not in the store), else the raw id. */
export function unmetLabel(u: UnmetDep): string {
  if (u.display_code) return u.display_code;
  if (u.title) return u.title;
  return `#${u.id}`;
}

/** "RP-077 (pending)" — the badge / toast form. */
export function unmetLabelWithStatus(u: UnmetDep): string {
  return `${unmetLabel(u)} (${u.status})`;
}

/** "RP-077 · pending — <title>" — the confirm-dialog list line. */
export function unmetLine(u: UnmetDep): string {
  const code = unmetCode(u);
  return `${code} · ${u.status} — ${u.title || code}`;
}

/**
 * Defensively parse an `unmet` array from a 409 body, an SSE event or a run
 * response. The contract guarantees {id, display_code, title, status} per
 * entry, but these arrive as raw JSON — never trust them.
 */
export function parseUnmetDeps(raw: unknown): UnmetDep[] {
  if (!Array.isArray(raw)) return [];
  const out: UnmetDep[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const r = item as Record<string, unknown>;
    if (r.id === undefined || r.id === null) continue;
    out.push({
      id: r.id as number | string,
      display_code: typeof r.display_code === 'string' ? r.display_code : undefined,
      title: typeof r.title === 'string' ? r.title : '',
      status: typeof r.status === 'string' ? r.status : 'pending',
    });
  }
  return out;
}

/**
 * The `unmet` list when `e` is the dep-add 409 (dependency_not_done), else
 * null. Lets callers turn the backstop into a confirm without importing the
 * ApiError class.
 */
export function unmetFromNotDoneError(e: unknown): UnmetDep[] | null {
  if (typeof e !== 'object' || e === null) return null;
  const err = e as { status?: unknown; body?: unknown };
  if (err.status !== 409) return null;
  const body = err.body as Record<string, unknown> | null | undefined;
  if (!body || body.error !== 'dependency_not_done') return null;
  return parseUnmetDeps(body.unmet);
}

/** Native window.confirm fallback for contexts where the modal is already
 *  closed (the create flow fires its dep adds after closeModal). */
export const confirmUnmetNative = (unmet: UnmetDep[]): Promise<boolean> =>
  Promise.resolve(window.confirm(
    "This task won't run until these dependencies are done:\n\n"
    + unmet.map(unmetLine).join('\n')
    + '\n\nSave anyway?',
  ));

/**
 * POST one dependency, honouring the not-done gate:
 *  - `acknowledged` → the user already confirmed this dep as not-done in a
 *    pre-flight dialog → send `acknowledged: true` straight away.
 *  - Otherwise the request may 409 with dependency_not_done (cross-project
 *    deps resolved by code aren't in the store; a race can flip a status
 *    between the pre-check and the write) → confirm through `confirmUnmet`
 *    and retry with `acknowledged: true`.
 *
 * Nothing persists on acknowledge — enforcement happens at run time.
 * Returns 'added'; 'cancelled' (user declined — the dep is simply not
 * added); or the raw error for the caller's own error surface.
 */
export async function addDependencyWithGate(
  taskId: number,
  depId: number,
  acknowledged: boolean,
  confirmUnmet: (unmet: UnmetDep[]) => Promise<boolean>,
): Promise<{ ok: true } | { ok: false; cancelled?: boolean; error?: unknown }> {
  const send = (ack: boolean) =>
    api.tasks.dependencies.add(taskId, {
      depends_on_id: depId,
      ...(ack ? { acknowledged: true } : {}),
    });
  try {
    await send(acknowledged);
    return { ok: true };
  } catch (e) {
    const unmet = unmetFromNotDoneError(e);
    if (!unmet) return { ok: false, error: e };
    const ok = await confirmUnmet(unmet);
    if (!ok) return { ok: false, cancelled: true };
    try {
      await send(true);
      return { ok: true };
    } catch (e2) {
      return { ok: false, error: e2 };
    }
  }
}
