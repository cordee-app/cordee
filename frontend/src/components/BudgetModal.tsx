import { useState, useEffect, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { BudgetInfo } from '../types';

export const BudgetModal = () => {
  const { showBudgetModal, setShowBudgetModal, activeProject, projects } = useStore();
  const perms = useProjectPermissions(activeProject);
  const [budget, setBudget] = useState<BudgetInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [monthlyBudget, setMonthlyBudget] = useState('');
  const [resetDay, setResetDay] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!activeProject) return;
    setLoading(true);
    setError('');
    try {
      const b = await api.projects.budget.get(activeProject);
      setBudget(b);
      setMonthlyBudget(b.monthly_budget ? String(b.monthly_budget) : '');
      setResetDay(b.budget_reset_day ? String(b.budget_reset_day) : '1');
    } catch {
      setError('Failed to load budget');
    } finally {
      setLoading(false);
    }
  }, [activeProject]);

  useEffect(() => {
    if (showBudgetModal) load();
  }, [showBudgetModal, load]);

  const project = projects.find((p) => p.id === activeProject);

  if (!showBudgetModal) return null;

  const handleBackdrop = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) setShowBudgetModal(false);
  };

  const handleSave = async () => {
    if (!activeProject) return;
    const rawAmount = monthlyBudget.trim();
    const rawDay = resetDay.trim();
    const day = parseInt(rawDay, 10);
    if (rawDay !== '' && (!Number.isFinite(day) || day < 1 || day > 28)) {
      setError('Reset day must be between 1 and 28.');
      return;
    }
    const amount = rawAmount === '' ? 0 : parseFloat(rawAmount);
    if (Number.isNaN(amount) || amount < 0) {
      setError('Budget must be a non-negative number.');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const body: { monthly_budget?: number; budget_reset_day?: number } = {};
      body.monthly_budget = amount;
      if (rawDay !== '') body.budget_reset_day = day;
      await api.projects.budget.set(activeProject, body);
      setShowBudgetModal(false);
    } catch (err: unknown) {
      setError('Failed to save: ' + (err instanceof Error ? err.message : 'unknown'));
    } finally {
      setSaving(false);
    }
  };

  const percent = budget?.percent_used ?? 0;
  const exceeded = budget?.exceeded ?? false;
  const barColor = exceeded ? '#a3402f' : percent >= 80 ? '#b2622d' : '#7a8a5e';

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={handleBackdrop}>
      <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong">
        <h3 id="budget-modal-title" className="m-0 mb-4 text-base">
          Budget{project ? ` — ${project.name}` : ''}
        </h3>

        {loading ? (
          <div className="p-4 text-center text-text-faint">Loading...</div>
        ) : error ? (
          <div className="p-4 text-dangerStrong text-md-">{error}</div>
        ) : budget ? (
          <>
            <div
              id="budget-modal-snapshot"
              className="text-md- mb-4 p-3 bg-surface-subtle rounded-md border border-border-muted"
            >
              <div className="mb-2">
                <span className="text-text-faint">Monthly budget: </span>
                <strong>${(budget.monthly_budget || 0).toFixed(2)}</strong>
              </div>
              <div className="mb-2">
                <span className="text-text-faint">Current spend: </span>
                <strong>${(budget.current_month_spend || 0).toFixed(4)}</strong>
              </div>
              <div className="mb-2">
                <span className="text-text-faint">Remaining: </span>
                <strong style={{ color: exceeded ? '#a3402f' : '#56633f' }}>
                  ${(budget.remaining || 0).toFixed(4)}
                </strong>
              </div>
              <div className="mb-1 flex justify-between text-xs">
                <span>{percent.toFixed(1)}% used</span>
                {exceeded ? <span className="text-danger font-semibold">Budget exceeded!</span> : null}
              </div>
              <div className="h-2 bg-[#eee7db] rounded overflow-hidden">
                <div
                  className="h-full rounded transition-[width] duration-300"
                  style={{
                    width: `${Math.min(percent, 100)}%`,
                    background: barColor,
                  }}
                />
              </div>
              {budget.budget_reset_day ? (
                <div className="mt-2 text-xs text-text-faint">
                  Resets on day {budget.budget_reset_day} of each month
                </div>
              ) : null}
            </div>

            <div className="form-group mb-3">
              <label className="block text-sm font-semibold text-text-soft mb-[3px]">Monthly budget (USD)</label>
              <input
                type="number"
                data-tip="Set the monthly budget in USD"
                className="w-full py-[7px] px-2.5 border border-default rounded text-base"
                min="0"
                step="0.01"
                value={monthlyBudget}
                onChange={(e) => setMonthlyBudget(e.target.value)}
                placeholder="0.00 — leave 0 to disable"
                disabled={!perms.canAdminister}
              />
            </div>
            <div className="form-group mb-3">
              <label className="block text-sm font-semibold text-text-soft mb-[3px]">Reset day (1-28)</label>
              <input
                type="number"
                data-tip="Day of month the budget resets (1-28)"
                className="w-full py-[7px] px-2.5 border border-default rounded text-base"
                min="1"
                max="28"
                step="1"
                value={resetDay}
                onChange={(e) => setResetDay(e.target.value)}
                disabled={!perms.canAdminister}
              />
            </div>
            {error ? (
              <div className="text-dangerStrong text-sm mb-2">{error}</div>
            ) : null}
            <div className="modal-actions flex justify-end gap-2 mt-4">
              <button data-tip="Close without saving" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={() => setShowBudgetModal(false)}>Cancel</button>
              {perms.canAdminister && (
                <button data-tip="Save the budget settings" className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white" onClick={handleSave} disabled={saving}>
                  {saving ? 'Saving...' : 'Save'}
                </button>
              )}
            </div>
          </>
        ) : (
          <div className="p-4 text-center text-text-faint">
            Select a project to manage its budget.
          </div>
        )}
      </div>
    </div>
  );
};

export default BudgetModal;
