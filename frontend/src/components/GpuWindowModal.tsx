import { useState, useEffect, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { cn } from '../utils/cn';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { HfQueueGroup, GpuWindow, GpuWindowModel, GpuRunStatus, HfImportVerify } from '../types';

const fmtEur = (n: number) => `€${(n || 0).toFixed(2)}`;
const fmtUsd = (n: number) => `$${(n || 0).toFixed(2)}`;
const timeAgo = (s: string) => {
  if (!s) return '';
  const d = new Date(s.replace(' ', 'T') + (s.includes('Z') || s.includes('+') ? '' : 'Z'));
  const mins = Math.floor((Date.now() - d.getTime()) / 60000);
  if (isNaN(mins)) return '';
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ${mins % 60}m`;
  return `${Math.floor(hrs / 24)}d`;
};

export const GpuWindowModal = () => {
  const { showGpuWindowModal, setShowGpuWindowModal, projects, executions } = useStore();
  const perms = useProjectPermissions();

  const [groups, setGroups] = useState<HfQueueGroup[]>([]);
  const [windows, setWindows] = useState<GpuWindow[]>([]);
  const [loading, setLoading] = useState(false);

  const [showPicker, setShowPicker] = useState(false);
  const [hfModels, setHfModels] = useState<GpuWindowModel[]>([]);
  const [selectedRepo, setSelectedRepo] = useState('');
  const [selectedOption, setSelectedOption] = useState('');
  const [depNodeType, setDepNodeType] = useState('L4');
  const [depIdleMin, setDepIdleMin] = useState('30');
  const [depBusy, setDepBusy] = useState(false);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [runBusy, setRunBusy] = useState(false);

  const [createError, setCreateError] = useState<string | null>(null);
  const [runWarnings, setRunWarnings] = useState<string[]>([]);
  const [activeRunDep, setActiveRunDep] = useState<number | null>(null);
  const [runProgress, setRunProgress] = useState<GpuRunStatus | null>(null);

  // Repair an un-servable group whose model was deleted from the Scaleway
  // library: re-import it (attributed to the first task's project, per the
  // project-scoped import rule).
  const [repairRepo, setRepairRepo] = useState<string | null>(null);
  const [repairVerify, setRepairVerify] = useState<HfImportVerify | null>(null);
  const [repairError, setRepairError] = useState<string | null>(null);
  const [repairBusy, setRepairBusy] = useState(false);

  const projectName = (pid: number | null) => {
    if (pid == null) return '';
    return projects.find((p) => p.id === pid)?.name || `#${pid}`;
  };

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [q, w] = await Promise.all([
        api.hfQueue().catch(() => ({ groups: [] as HfQueueGroup[] })),
        api.gpuWindow.list().catch(() => ({ windows: [] as GpuWindow[] })),
      ]);
      setGroups(q.groups || []);
      setWindows(w.windows || []);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!showGpuWindowModal) return;
    load();
    const i = setInterval(load, 15000);
    return () => clearInterval(i);
  }, [showGpuWindowModal, load]);

  useEffect(() => {
    if (activeRunDep == null) return;
    let stop = false;
    const tick = async () => {
      try {
        const r = await api.gpuWindow.runStatus(activeRunDep);
        if (stop) return;
        if (r.run) {
          setRunProgress(r.run);
          const vals = Object.values(r.run.tasks);
          if (vals.length && vals.every((v) => v.status === 'done' || v.status === 'failed')) {
            setActiveRunDep(null);
            load();
            return;
          }
        } else {
          // Fallback: task was started via regular run flow, not the
          // GPU-window endpoint — _GPU_RUN_STATE is empty but executions
          // exist. Derive running state from the store's executions so the
          // window actually shows live progress (P1.5 fix).
          const dep = windows.find((w) => w.id === activeRunDep);
          const prefix = dep ? `scw-dep-${dep.scw_deployment_id}` : '';
          const fallbackExecs = prefix ? executions.filter((e) => e.model === prefix && e.status === 'running') : [];
          if (fallbackExecs.length > 0) {
            setRunProgress({
              dep_id: activeRunDep,
              started_at: fallbackExecs[0].started_at || new Date().toISOString(),
              tasks: Object.fromEntries(fallbackExecs.map((e) => [String(e.task_id), { status: 'running', error: '' }])),
            });
          } else {
            setRunProgress(null);
          }
        }
      } catch {
        /* ignore */
      }
    };
    tick();
    const i = setInterval(tick, 3000);
    return () => { stop = true; clearInterval(i); };
  }, [activeRunDep, load, executions, windows]);

  const recommendedNode = (o: GpuWindowModel['options'][number] | undefined) => {
    if (!o || !o.node_types?.length) return 'L4';
    return o.node_types.find((nt) => o.stock_status?.[nt] === 'available') || o.node_types[0];
  };

  if (!showGpuWindowModal) return null;

  const selectedHf = hfModels.find((m) => m.repo_id === selectedRepo);
  const selectedDeployable = selectedHf?.options.find((o) => o.id === selectedOption);

  const openPicker = async () => {
    setCreateError(null);
    setShowPicker(true);
    setSelectedRepo('');
    setSelectedOption('');
    setDepNodeType('L4');
    try {
      const r = await api.gpuWindow.models();
      setHfModels(r.models || []);
    } catch (e) {
      setHfModels([]);
      setCreateError(e instanceof Error ? e.message : 'unknown');
    }
  };

  const handleCreate = async () => {
    if (!selectedOption) return;
    setDepBusy(true);
    setCreateError(null);
    try {
      await api.gpuWindow.open({
        model_name: selectedOption,
        node_type: depNodeType,
        idle_delete_minutes: parseInt(depIdleMin, 10) || 30,
      });
      setShowPicker(false);
      await load();
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : 'unknown');
    } finally {
      setDepBusy(false);
    }
  };

  const handleClose = async (depId: number) => {
    if (!window.confirm('Close this GPU window? It will stop billing immediately and split its cost across the projects that used it.')) return;
    setDepBusy(true);
    try {
      await api.gpuWindow.close(depId);
      await load();
    } catch (e) {
      alert('Error: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setDepBusy(false);
    }
  };

  const handleRepairVerify = async (repoId: string) => {
    setRepairRepo(repoId);
    setRepairVerify(null);
    setRepairError(null);
    setRepairBusy(true);
    try {
      const r = await api.hfModelImports.verify(repoId);
      setRepairVerify(r);
    } catch (e) {
      setRepairVerify({ ok: false, error: e instanceof Error ? e.message : 'unknown' });
      setRepairError(e instanceof Error ? e.message : 'unknown');
    } finally {
      setRepairBusy(false);
    }
  };

  const handleRepairImport = async (repoId: string, projectId: number | null) => {
    if (projectId == null) {
      setRepairError('No project owns a task for this model — import it from a project\'s Settings → HF Scout.');
      return;
    }
    setRepairError(null);
    setRepairBusy(true);
    try {
      await api.hfModelImports.create(projectId, { repo_id: repoId });
      setRepairRepo(null);
      setRepairVerify(null);
      await load();
    } catch (e) {
      // Keep Import disabled after a rejection so the user can't re-click into
      // the same error; they must re-Verify (e.g. after re-importing correctly).
      setRepairVerify({ ok: false, error: e instanceof Error ? e.message : 'unknown' });
      setRepairError(e instanceof Error ? e.message : 'unknown');
    } finally {
      setRepairBusy(false);
    }
  };

  const handleRun = async (depId: number) => {
    if (selected.size === 0) return;
    setRunBusy(true);
    setRunWarnings([]);
    try {
      const res = await api.gpuWindow.run(depId, Array.from(selected));
      setSelected(new Set());
      setRunWarnings(res.warnings || []);
      setActiveRunDep(depId);
      setRunProgress(null);
      await load();
    } catch (e) {
      alert('Error: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setRunBusy(false);
    }
  };

  const toggleTask = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const readyWindow = windows.find((w) => w.status === 'ready');
  const runnableTasks = groups.flatMap((g) => g.tasks).filter((t) => t.status === 'confirmed' || t.status === 'pending');

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={() => setShowGpuWindowModal(false)}>
      <div
        className="modal-content bg-surface-raised rounded-lg p-6 w-[96vw] h-[92vh] max-w-none max-h-none flex flex-col shadow-strong"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">GPU Run Window</h3>
          <button data-tip="Close GPU window" className="btn py-1 px-3 border border-default rounded cursor-pointer text-sm-" onClick={() => setShowGpuWindowModal(false)}>Close</button>
        </div>

        <div className="flex-1 overflow-y-auto grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Left: HF task queue grouped by model */}
          <div className="flex flex-col gap-3">
            <div className="form-group">
              <label className="block text-sm font-semibold text-text-soft mb-1">HF tasks (all projects)</label>
              {loading && groups.length === 0 && <div className="text-xs text-text-faint">Loading…</div>}
              {!loading && groups.length === 0 && (
                <div className="text-xs text-text-faint">No tasks are assigned to a Hugging Face model yet.</div>
              )}
              {groups.map((g) => (
                <div key={g.repo_id} className="mb-2 border border-border-subtle rounded-md p-2">
                  <div className="flex items-center gap-2 text-sm+">
                    <span className="font-semibold truncate" title={g.repo_id}>{g.label}</span>
                    {g.servable && g.import_ready ? (
                      <span
                        className="text-2xs px-1.5 py-px rounded-full font-semibold bg-status-running/20 text-status-running"
                        title={g.import_model_name ? `Imported as ${g.import_model_name} — deploy via Open GPU Window` : 'Imported — deploy via Open GPU Window'}
                      >imported</span>
                    ) : g.servable ? (
                      <span className="text-2xs px-1.5 py-px rounded-full font-semibold bg-status-running/20 text-status-running">servable</span>
                    ) : (g.import_status === 'preparing' || g.import_status === 'downloading') ? (
                      <span className="text-2xs px-1.5 py-px rounded-full font-semibold bg-status-pending/20 text-status-pending">import: {g.import_status}…</span>
                    ) : (
                      <span className="text-2xs px-1.5 py-px rounded-full font-semibold bg-status-pending/20 text-status-pending">awaiting self-host</span>
                    )}
                    {!g.servable && g.import_status !== 'preparing' && g.import_status !== 'downloading' && perms.canAdminister && (
                      <button
                        data-tip="Download this Hugging Face model into the Scaleway library so it can be deployed"
                        className="ml-auto btn py-0.5 px-2 border border-accent rounded cursor-pointer text-xs text-accent"
                        onClick={() => handleRepairVerify(g.repo_id)}
                        disabled={repairBusy}
                      >Import to Scaleway</button>
                    )}
                  </div>

                  {repairRepo === g.repo_id && (
                    <div className="mt-1.5 p-2 bg-surface-subtle rounded-md">
                      {repairVerify?.ok ? (
                        <div className="text-xs text-status-running mb-1">
                          Importable — nodes {repairVerify.nodes?.join(', ') || '—'}
                          {repairVerify.max_context_size != null && ` · up to ${repairVerify.max_context_size.toLocaleString()} ctx`}
                          {repairVerify.size_bytes != null && ` · ${(repairVerify.size_bytes / 1e9).toFixed(1)} GB`}
                        </div>
                      ) : (repairVerify?.error || repairError) ? (
                        <div className="p-2 rounded text-xs bg-status-pending/20 text-status-pending mb-1">
                          {repairVerify?.error || repairError}
                        </div>
                      ) : (
                        <div className="text-xs text-text-faint mb-1">Checking…</div>
                      )}
                      <div className="flex gap-2">
                        <button
                          data-tip="Import this repo into the Scaleway model library"
                          className="btn btn-primary py-1 px-3 border border-accent rounded cursor-pointer text-sm- bg-accent text-white"
                          onClick={() => handleRepairImport(g.repo_id, g.tasks[0]?.project_id ?? null)}
                          disabled={repairBusy || (repairVerify ? !repairVerify.ok : true)}
                        >{repairBusy ? 'Working…' : 'Import'}</button>
                        <button
                          data-tip="Cancel"
                          className="btn py-1 px-3 border border-default rounded cursor-pointer text-sm-"
                          onClick={() => { setRepairRepo(null); setRepairVerify(null); setRepairError(null); }}
                        >Cancel</button>
                      </div>
                    </div>
                  )}

                  {g.tasks.map((t) => (
                    <div key={t.id} className="flex items-center gap-2 py-1 text-sm+ border-b border-border-subtle last:border-0">
                      <input
                        data-tip="Select task to run on GPU"
                        type="checkbox"
                        checked={selected.has(t.id)}
                        disabled={!perms.canAdminister || !readyWindow || !(t.status === 'confirmed' || t.status === 'pending')}
                        onChange={() => toggleTask(t.id)}
                      />
                      <span className="text-text-faint">#{t.id}</span>
                      <span className="flex-1 truncate">{t.title}</span>
                      <span className="text-xs text-text-faint">{t.project_name}</span>
                      <span className="text-xs text-text-muted">{t.status}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>

          {/* Right: GPU windows + controls */}
          <div className="flex flex-col gap-3">
            <div className="form-group">
              <label className="block text-sm font-semibold text-text-soft mb-1">GPU windows</label>
              <div className="text-xs text-text-faint mb-2">
                GPU windows bill hourly (€/h) while open, including idle time. Cost is split across the projects that run on it.
              </div>

              {windows.filter((w) => w.status !== 'deleted').map((w) => (
                <div key={w.id} className="mb-2 border border-border-subtle rounded-md p-2">
                  <div className="flex items-center gap-2 text-sm+">
                    <span className="font-semibold">{w.model_name}</span>
                    <span className="text-text-faint">· {w.node_type}</span>
                    <span className={cn('px-1.5 rounded text-xs', w.status === 'ready' ? 'bg-status-running/20 text-status-running' : 'bg-surface text-text-muted')}>{w.status}</span>
                    {w.provider_status && w.provider_status !== w.status && (
                      <span className="text-xs text-text-faint">({w.provider_status})</span>
                    )}
                    <span className="text-xs font-mono text-text-muted">{fmtEur(w.hourly_eur)}/h</span>
                    <span className="text-xs font-mono text-text-soft">{fmtUsd(w.accrued_cost_usd)}</span>
                    <span className="text-xs text-text-faint">{timeAgo(w.created_at)}</span>
                    <span className="ml-auto flex gap-2">
                      {perms.canAdminister && w.status === 'ready' && (
                        <button
                          data-tip="Run selected tasks on this GPU"
                          className="btn py-0.5 px-2 border border-accent rounded cursor-pointer text-xs text-accent"
                          onClick={() => handleRun(w.id)}
                          disabled={runBusy || selected.size === 0}
                        >
                          Run {selected.size > 0 ? `(${selected.size})` : ''}
                        </button>
                      )}
                      {perms.canAdminister && (
                        <button
                          data-tip="Close GPU window and stop billing"
                          className="btn py-0.5 px-2 border border-danger rounded cursor-pointer text-xs text-danger"
                          onClick={() => handleClose(w.id)}
                          disabled={depBusy}
                        >Close</button>
                      )}
                    </span>
                  </div>
                  {w.cost_by_project && Object.keys(w.cost_by_project).length > 0 && (
                    <div className="mt-1 text-xs text-text-faint">
                      Cost split: {Object.entries(w.cost_by_project).map(([pid, usd]) => `${projectName(Number(pid))} ${fmtUsd(usd)}`).join(' · ')}
                    </div>
                  )}
                  {w.id === activeRunDep && runWarnings.map((warn, i) => (
                    <div key={i} className="mt-1 text-xs text-status-pending font-semibold">{warn}</div>
                  ))}
                  {w.id === activeRunDep && runProgress && (
                    <div className="mt-1 text-xs text-text-faint">
                      {Object.entries(runProgress.tasks).map(([tid, s]) => (
                        <div key={tid}>#{tid}: {s.status}{s.error ? ` — ${s.error}` : ''}</div>
                      ))}
                    </div>
                  )}
                  {w.id !== activeRunDep && (() => {
                    const prefix = `scw-dep-${w.scw_deployment_id}`;
                    const running = executions.filter((e) => e.model === prefix && e.status === 'running');
                    if (running.length === 0) return null;
                    return (
                      <div className="mt-1 text-xs text-text-faint">
                        {running.map((e) => <div key={e.id}>#{e.task_id}: running</div>)}
                      </div>
                    );
                  })()}
                </div>
              ))}

              {!perms.canAdminister ? null : showPicker ? (
                <div className="p-2.5 bg-surface-subtle rounded-md">
                  <div className="flex flex-col gap-2">
                    <select data-tip="Choose a Hugging Face model" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={selectedRepo} onChange={(e) => { setSelectedRepo(e.target.value); setSelectedOption(''); }}>
                      <option value="">— Select HF model —</option>
                      {hfModels.map((m) => (
                        <option key={m.repo_id} value={m.repo_id} disabled={!m.servable}>
                          {m.label} ({m.task_count} task{m.task_count === 1 ? '' : 's'}){m.servable ? '' : ' — needs self-host'}
                        </option>
                      ))}
                    </select>

                    {selectedHf && !selectedHf.servable && (
                      <div className="text-xs text-status-pending font-semibold">
                        This model has no Scaleway serving path — it needs self-hosting and cannot run on a Scaleway GPU.
                      </div>
                    )}

                    {selectedHf && selectedHf.servable && (
                      <>
                        <select data-tip="Choose a deployable model" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={selectedOption} onChange={(e) => { const o = selectedHf.options.find((x) => x.id === e.target.value); setSelectedOption(e.target.value); setDepNodeType(recommendedNode(o)); }}>
                          <option value="">— Select deployable model —</option>
                          {selectedHf.options.map((o) => (
                            <option key={o.id} value={o.id}>{o.name}</option>
                          ))}
                        </select>

                        {selectedDeployable && (
                          <select data-tip="Choose GPU node type" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={depNodeType} onChange={(e) => setDepNodeType(e.target.value)}>
                            {selectedDeployable.node_types.map((nt) => (
                              <option key={nt} value={nt}>{nt} — {fmtEur(selectedDeployable.hourly_eur?.[nt] ?? 0)}/h</option>
                            ))}
                          </select>
                        )}

                        <div className="text-xs text-text-faint">
                          {selectedHf.params_b > 0 && (
                            <span>~{selectedHf.params_b}B params · recommended {selectedHf.estimated_node} ({fmtEur(selectedHf.estimated_hourly_eur)}/h)</span>
                          )}
                          {selectedHf.params_b <= 0 && <span>Parameter size unknown</span>}
                          {selectedDeployable && selectedDeployable.max_context_size != null && (
                            <span> · context {selectedDeployable.max_context_size.toLocaleString()} tokens</span>
                          )}
                        </div>
                        {selectedDeployable && selectedDeployable.status && !['ready', 'available', ''].includes(selectedDeployable.status) && (
                          <div className="text-xs text-status-pending font-semibold">
                            Scaleway reports this model as "{selectedDeployable.status}" — it may not be deployable.
                          </div>
                        )}
                      </>
                    )}

                    {createError && (
                      <div className="p-2 rounded text-xs bg-status-pending/20 text-status-pending">{createError}</div>
                    )}

                    <div className="flex items-center gap-2">
                      <label className="text-xs text-text-soft">Idle delete (min):</label>
                      <input data-tip="Minutes idle before auto-delete" type="number" className="w-20 py-1 px-2 border border-default rounded text-sm+" value={depIdleMin} onChange={(e) => setDepIdleMin(e.target.value)} min={1} />
                    </div>
                    <div className="flex gap-2">
                      <button data-tip="Deploy the selected model" className="btn btn-primary py-1 px-3 border border-accent rounded cursor-pointer text-sm- bg-accent text-white" onClick={handleCreate} disabled={!selectedOption || depBusy}>
                        {depBusy ? 'Deploying…' : 'Deploy'}
                      </button>
                      <button data-tip="Cancel model deployment" className="btn py-1 px-3 border border-default rounded cursor-pointer text-sm-" onClick={() => setShowPicker(false)}>Cancel</button>
                    </div>
                  </div>
                </div>
              ) : (
                <button data-tip="Deploy a new GPU window" className="btn py-1 px-3 border border-default rounded cursor-pointer text-sm-" onClick={openPicker}>Open GPU Window</button>
              )}
            </div>

            {runnableTasks.length > 0 && !readyWindow && (
              <div className="text-xs text-text-faint">
                {runnableTasks.length} runnable task(s) waiting — open a GPU window to run them.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
