import { useCallback, useRef, useState } from 'react';
import type { UnmetDep } from '../types';
import { unmetLine } from '../utils/deps';

export interface UnmetDepsConfirmState {
  unmet: UnmetDep[];
  /** 'save' = whole-task save (create flow); 'add' = single dependency add. */
  mode: 'save' | 'add';
}

/**
 * Blocking confirm for the dependency gate: the user is saving a task (or
 * adding a dependency) whose dependency isn't done, which will block the
 * task from running. Nothing persists on "anyway" — the acknowledge only
 * accompanies the save; enforcement happens at run time.
 */
export const useUnmetDepsConfirm = () => {
  const [state, setState] = useState<UnmetDepsConfirmState | null>(null);
  const resolveRef = useRef<((ok: boolean) => void) | null>(null);

  /** Open the dialog; resolves true on "…anyway", false on cancel/dismiss. */
  const confirm = useCallback((unmet: UnmetDep[], mode: 'save' | 'add') => {
    return new Promise<boolean>((resolve) => {
      // A previous confirm still pending is superseded (resolve it as
      // cancelled so its awaiting caller doesn't hang).
      resolveRef.current?.(false);
      resolveRef.current = resolve;
      setState({ unmet, mode });
    });
  }, []);

  /** Close the dialog, resolving the awaiting caller. */
  const settle = useCallback((ok: boolean) => {
    const r = resolveRef.current;
    resolveRef.current = null;
    setState(null);
    r?.(ok);
  }, []);

  return { state, confirm, settle };
};

/**
 * The confirm dialog itself. Rendered inside the modal it blocks — its
 * overlay (z-toast, above z-modal) covers the whole screen so the modal
 * can't be dismissed behind its back.
 */
export const UnmetDepsConfirmDialog = ({
  state,
  onConfirm,
  onCancel,
}: {
  state: UnmetDepsConfirmState | null;
  onConfirm: () => void;
  onCancel: () => void;
}) => {
  if (!state) return null;
  return (
    <div
      className="fixed inset-0 bg-black/50 dark:bg-black/75 flex items-center justify-center z-toast"
      onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}
    >
      <div
        className="bg-surface-raised dark:bg-surface-dark-raised rounded-lg p-5 max-w-[520px] w-[90vw] shadow-strong border border-default dark:border-border-dark-default"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="text-base font-semibold mb-2 text-text-default dark:text-text-dark-default">
          🚧 Unfinished dependencies
        </div>
        <div className="text-sm mb-2.5 text-text-soft dark:text-text-dark-soft">
          {state.mode === 'save'
            ? "This task won't run until these dependencies are done. Save anyway?"
            : 'This task will not run until this dependency is done. Add anyway?'}
        </div>
        <ul className="text-sm mb-4 ml-4 list-disc max-h-[220px] overflow-y-auto">
          {state.unmet.map((u) => (
            <li key={String(u.id)} className="mb-0.5 text-text-default dark:text-text-dark-default">
              {unmetLine(u)}
            </li>
          ))}
        </ul>
        <div className="flex justify-end gap-2">
          <button
            data-tip="Keep editing — nothing is saved"
            className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            data-tip="Save with the unfinished dependency acknowledged"
            className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white"
            onClick={onConfirm}
          >
            {state.mode === 'save' ? 'Save anyway' : 'Add anyway'}
          </button>
        </div>
      </div>
    </div>
  );
};
