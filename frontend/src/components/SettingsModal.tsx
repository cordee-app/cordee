import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { cn } from '../utils/cn';
import { fmtEur, fmtUsd } from '../utils/currency';
import { SettingsUsersTab } from './SettingsUsersTab';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { Config, ProviderReadiness, ScwSessionStatus, ProjectHfModel, HfSearchResult, HfImportVerify } from '../types';

// Public source repository: the single place to change if the GitHub org changes.
const SOURCE_URL = 'https://github.com/cordee-app/cordee';

const isVault = Boolean((typeof window !== 'undefined' && (window as unknown as { __AINGEL_VAULT__?: boolean }).__AINGEL_VAULT__) || false);

export const SettingsModal = () => {
  const {
    showSettingsModal, setShowSettingsModal,
    activeProject, projects, models,
    settingsDirty, setSettingsDirty,
    setKanbanTokenBudget,
    user,
    settingsTab: tab, setSettingsTab: setTab,
  } = useStore();

  const isFreeUser = user?.plan === 'free' && user?.role !== 'admin';

  const projectPerms = useProjectPermissions(activeProject);
  const canAdminProject = projectPerms.canAdminister;
  const canConfigGeneral = projectPerms.authOff || projectPerms.isAdmin;
  const ro = !canAdminProject;
  const roClass = ro ? 'opacity-40 cursor-not-allowed' : '';

  const [_config, setConfig] = useState<Config | null>(null);
  const [readiness, setReadiness] = useState<ProviderReadiness | null>(null);
  const [anthropicMode, setAnthropicMode] = useState('claude-code');
  const [mistralMode, setMistralMode] = useState('vibe');
  const [tokenBudget, setTokenBudget] = useState('');
  const [ragCorpora, setRagCorpora] = useState<{ id: string; label: string }[] | null>(null);
  const [budgetStatus, setBudgetStatus] = useState('');

  const [ptype, setPtype] = useState('');
  const [euOnly, setEuOnly] = useState(false);
  const [monthlyBudget, setMonthlyBudget] = useState('0');
  const [budgetResetDay, setBudgetResetDay] = useState('1');
  const [aingelName, setAingelName] = useState('');
  const [aingelModel, setAingelModel] = useState('');
  const [autopilot, setAutopilot] = useState(false);
  const [aingelMode, setAingelMode] = useState('strict');
  const [useRag, setUseRag] = useState(false);
  const [ragCorpusId, setRagCorpusId] = useState('railway');
  const [saving, setSaving] = useState(false);

  const [scwStatus, setScwStatus] = useState<ScwSessionStatus | null>(null);
  const [scwBusy, setScwBusy] = useState(false);
  const [sessionFiles, setSessionFiles] = useState<Array<{ key: string; size: number; last_modified: string; etag: string }>>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState('');

  const [hfQuery, setHfQuery] = useState('');
  const [hfResults, setHfResults] = useState<HfSearchResult[] | null>(null);
  const [hfSearching, setHfSearching] = useState(false);
  const [hfRoster, setHfRoster] = useState<ProjectHfModel[]>([]);
  const [hfBusy, setHfBusy] = useState(false);
  // Import to Scaleway (beta): a project-initiated import whose result is an
  // Organization-global library model any project can deploy.
  const [importRepo, setImportRepo] = useState('');
  const [importVerify, setImportVerify] = useState<HfImportVerify | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);

  // The AIngel model choices were a hardcoded <option> list: it offered both
  // Claude models on EU-only projects (the backend refuses them on save, so the
  // only symptom was an error) and offered no Scaleway models at all — which
  // meant the Autopilot hooks could never run on the EU catalogue. Build the
  // list from the live catalogue instead, filtered as AddTaskModal filters it.
  const isEuModel = (id: string) =>
    id.startsWith('scw-') || id.startsWith('mistral-') || id.startsWith('open-mistral');
  const aingelModelChoices = useMemo(
    () => (euOnly ? models.filter(m => isEuModel(m.id)) : models),
    [models, euOnly],
  );

  // Ticking EU-only here must not leave a non-compliant model selected behind it.
  useEffect(() => {
    if (euOnly && aingelModel && !isEuModel(aingelModel)) setAingelModel('');
  }, [euOnly, aingelModel]);

  const loadConfigAndReadiness = useCallback(async () => {
    try {
      const [cfg, rdy] = await Promise.all([api.config.get(), api.config.readiness()]);
      setConfig(cfg);
      setReadiness(rdy);
      setAnthropicMode(cfg.anthropic_mode || 'claude-code');
      setMistralMode(cfg.mistral_mode || 'vibe');
      setTokenBudget(String(cfg.claude_pro_token_budget || ''));
      setRagCorpora(cfg.rag_corpora ?? null);
    } catch { /* ignore */ }
  }, []);

  // Save sends only the fields that differ from what the form loaded, so a
  // form opened before a change made elsewhere (another tab, a direct DB fix)
  // cannot silently put the old value back.
  const loadedPayload = useRef<Record<string, unknown>>({});
  const buildProjectPayload = (v: {
    euOnly: boolean; monthlyBudget: string; budgetResetDay: string; aingelName: string;
    aingelModel: string; autopilot: boolean; aingelMode: string; useRag: boolean; ragCorpusId: string;
  }): Record<string, unknown> => ({
    eu_only: v.euOnly,
    monthly_budget: parseFloat(v.monthlyBudget) || 0,
    budget_reset_day: parseInt(v.budgetResetDay, 10) || 1,
    aingel_name: v.aingelName.trim() || null,
    aingel_model: v.aingelModel || null,
    aingel_autopilot: v.autopilot ? 1 : 0,
    aingel_mode: v.aingelMode,
    use_rag: v.useRag ? 1 : 0,
    rag_corpus_id: v.ragCorpusId,
  });

  const loadProjectFields = useCallback(() => {
    const proj = activeProject ? projects.find(p => p.id === activeProject) : null;
    if (!proj) return;
    const p = proj as unknown as Record<string, unknown>;
    setPtype(String(p.project_type || ''));
    setEuOnly(Boolean(p.eu_only));
    setMonthlyBudget(String(p.budget_monthly || p.monthly_budget || '0'));
    setBudgetResetDay(String(p.budget_reset_day || '1'));
    setAingelName(String(p.aingel_name || ''));
    setAingelModel(String(p.aingel_model || ''));
    setAutopilot(Boolean(p.aingel_autopilot));
    setAingelMode(String(p.aingel_mode || 'advisory'));
    setUseRag(Boolean(p.use_rag));
    setRagCorpusId(String(p.rag_corpus_id || 'railway'));
    loadedPayload.current = buildProjectPayload({
      euOnly: Boolean(p.eu_only),
      monthlyBudget: String(p.budget_monthly || p.monthly_budget || '0'),
      budgetResetDay: String(p.budget_reset_day || '1'),
      aingelName: String(p.aingel_name || ''),
      aingelModel: String(p.aingel_model || ''),
      autopilot: Boolean(p.aingel_autopilot),
      aingelMode: String(p.aingel_mode || 'advisory'),
      useRag: Boolean(p.use_rag),
      ragCorpusId: String(p.rag_corpus_id || 'railway'),
    });
  }, [activeProject, projects]);

  useEffect(() => {
    if (showSettingsModal) {
      loadConfigAndReadiness();
      setSettingsDirty(false);
    }
  }, [showSettingsModal, loadConfigAndReadiness, setSettingsDirty]);

  // App.tsx polls loadAll() every 30s, which replaces the `projects` array and
  // so changes `loadProjectFields`'s identity. Depending on it here re-ran the
  // loader on every poll and overwrote the form with server values — unsaved
  // edits vanished roughly 30 seconds after being typed. Reload only on the
  // events that should reload: opening the modal, switching project, or
  // switching tab. AddTaskModal avoids this by reading the store through
  // getState() with stable deps; this is the same idea via a ref.
  const loadProjectFieldsRef = useRef(loadProjectFields);
  loadProjectFieldsRef.current = loadProjectFields;

  useEffect(() => {
    if (!showSettingsModal) return;
    loadProjectFieldsRef.current();
  }, [showSettingsModal, activeProject, tab]);

  const loadScwAndDeps = useCallback(async () => {
    if (!activeProject) return;
    const proj = projects.find(p => p.id === activeProject);
    const p = proj as unknown as { eu_only?: boolean; scw_session_enabled?: boolean };
    if (!(isVault || p.eu_only || p.scw_session_enabled)) {
      setScwStatus(null);
      return;
    }
    try {
      const s = await api.projects.scwSession.status(activeProject).catch(() => null);
      setScwStatus(s);
      if (s?.enabled) {
        const files = await api.projects.scwSession.files(activeProject).catch(() => ({ files: [] }));
        setSessionFiles(files.files || []);
      } else {
        setSessionFiles([]);
      }
    } catch { /* ignore */ }
  }, [activeProject, projects]);

  useEffect(() => {
    if (showSettingsModal && tab === 'project' && activeProject) loadScwAndDeps();
  }, [showSettingsModal, tab, activeProject, loadScwAndDeps]);

  const handleEnableSession = async () => {
    if (!activeProject) return;
    setScwBusy(true);
    try {
      await api.projects.scwSession.create(activeProject);
      await loadScwAndDeps();
    } catch (e) {
      alert('Error: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setScwBusy(false);
    }
  };

  // Legacy SCW per-file upload — retained for reference; File Manager now uses unified /files/upload
  const handleUploadFiles = async (files: File[]) => {
    if (!activeProject) return;
    setUploading(true);
    let lastSynced = 0;
    let syncWarning = '';
    try {
      for (let i = 0; i < files.length; i++) {
        const f = files[i];
        const relPath = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
        setUploadProgress(`Uploading ${i + 1}/${files.length}: ${relPath}`);
        const res = await api.projects.scwSession.upload(activeProject, f, relPath) as { synced?: number; sync_warning?: string };
        if (typeof res?.synced === 'number') lastSynced += res.synced;
        if (res?.sync_warning) syncWarning = res.sync_warning;
      }
      const result = await api.projects.scwSession.files(activeProject);
      setSessionFiles(result.files || []);
      if (lastSynced > 0) {
        setUploadProgress(`Synced ${lastSynced} file${lastSynced === 1 ? '' : 's'} to Working Documents — check Files > Reference.`);
        await new Promise(r => setTimeout(r, 1800));
      }
      if (syncWarning) {
        setUploadProgress(`Uploaded but sync warning: ${syncWarning} — try Sync bucket.`);
        await new Promise(r => setTimeout(r, 2200));
      }
      setUploadProgress('');
      try { window.dispatchEvent(new CustomEvent('aingel:files-changed', { detail: { projectId: activeProject } })); } catch {}
    } catch (e) {
      alert('Upload failed: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setUploading(false);
      if (!syncWarning) setUploadProgress('');
    }
  };
  void handleUploadFiles;

  const handleSyncBucket = async () => {
    if (!activeProject) return;
    setScwBusy(true);
    try {
      const res = await api.projects.scwSession.sync(activeProject);
      const files = await api.projects.scwSession.files(activeProject).catch(() => ({ files: [] as typeof sessionFiles }));
      setSessionFiles(files.files || []);
      setUploadProgress(`Synced ${res.synced} file${res.synced === 1 ? '' : 's'} to Working Documents.`);
      setTimeout(() => setUploadProgress(''), 2000);
      try { window.dispatchEvent(new CustomEvent('aingel:files-changed', { detail: { projectId: activeProject } })); } catch {}
    } catch (e) {
      alert('Sync failed: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setScwBusy(false);
    }
  };

  const handleCloseSession = async () => {
    if (!activeProject) return;
    if (!window.confirm('This will permanently crypto-shred all data in the session bucket. This cannot be undone.')) return;
    setScwBusy(true);
    try {
      await api.projects.scwSession.close(activeProject);
      await loadScwAndDeps();
    } catch (e) {
      alert('Error: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setScwBusy(false);
    }
  };

  const loadHfRoster = useCallback(async () => {
    if (!activeProject) { setHfRoster([]); return; }
    try {
      const r = await api.projects.hfModels.list(activeProject);
      setHfRoster(r.models || []);
    } catch { /* ignore */ }
  }, [activeProject]);

  useEffect(() => {
    if (showSettingsModal && tab === 'project' && activeProject) loadHfRoster();
  }, [showSettingsModal, tab, activeProject, loadHfRoster]);

  // A repo staged for import must not carry across a project switch — imports are
  // attributed to the project that initiated them.
  useEffect(() => {
    setImportRepo('');
    setImportVerify(null);
    setImportError(null);
  }, [activeProject]);

  // Poll while any roster model has an in-flight import so its status advances
  // (preparing → downloading → ready) without the user reopening Settings.
  useEffect(() => {
    const inFlight = hfRoster.some((m) => m.import_status
      && ['preparing', 'downloading'].includes(m.import_status));
    if (!showSettingsModal || tab !== 'project' || !inFlight) return;
    const i = setInterval(loadHfRoster, 15000);
    return () => clearInterval(i);
  }, [showSettingsModal, tab, hfRoster, loadHfRoster]);

  const handleHfSearch = async () => {
    const q = hfQuery.trim();
    if (!q) return;
    setHfSearching(true);
    setHfResults(null);
    try {
      const r = await api.hf.search({ q });
      setHfResults(r.results || []);
    } catch (e) {
      alert('Error: ' + (e as Error).message);
    } finally {
      setHfSearching(false);
    }
  };

  const handleHfAdopt = async (repoId: string, setDefault: boolean) => {
    if (!activeProject) return;
    setHfBusy(true);
    try {
      await api.hf.register({ project_id: activeProject, repo_id: repoId, set_default: setDefault });
      await loadHfRoster();
    } catch (e) {
      alert('Error: ' + (e as Error).message);
    } finally {
      setHfBusy(false);
    }
  };

  const handleHfRemove = async (repoId: string) => {
    if (!activeProject) return;
    setHfBusy(true);
    try {
      await api.projects.hfModels.remove(activeProject, repoId);
      await loadHfRoster();
    } catch (e) {
      alert('Error: ' + (e as Error).message);
    } finally {
      setHfBusy(false);
    }
  };

  const handleImportVerify = async (repoId: string) => {
    setImportError(null);
    setImportVerify(null);
    if (!repoId.trim()) return;
    setImportBusy(true);
    try {
      const r = await api.hfModelImports.verify(repoId.trim());
      setImportVerify(r);
    } catch (e) {
      // api.post throws on the 422 rejection; keep a failure state so Import
      // stays disabled (the server would reject it anyway).
      setImportVerify({ ok: false, error: (e as Error).message });
      setImportError((e as Error).message);
    } finally {
      setImportBusy(false);
    }
  };

  const handleImport = async (repoId: string) => {
    if (!activeProject) return;
    setImportError(null);
    setImportBusy(true);
    try {
      await api.hfModelImports.create(activeProject, { repo_id: repoId.trim() });
      setImportRepo('');
      setImportVerify(null);
      await loadHfRoster();
    } catch (e) {
      setImportError((e as Error).message);
    } finally {
      setImportBusy(false);
    }
  };

  const handleImportDelete = async (importId: number) => {
    if (!window.confirm(
      'Delete this imported model from the Scaleway library? This frees quota and cannot be undone.\n\n'
      + 'Any task (in any project) assigned to this model will show "awaiting self-host" '
      + 'until it is re-imported.')) return;
    setImportBusy(true);
    try {
      await api.hfModelImports.remove(importId);
      await loadHfRoster();
    } catch (e) {
      alert('Error: ' + (e as Error).message);
    } finally {
      setImportBusy(false);
    }
  };

  const setDirty = () => { if (!settingsDirty) setSettingsDirty(true); };

  const handleSetAnthropicMode = async (mode: string) => {
    if (!readiness) {
      const rdy = await api.config.readiness();
      setReadiness(rdy);
    }
    const targetReady = mode === 'claude-code' ? readiness?.claude_code : readiness?.api;
    const label = mode === 'claude-code' ? 'Claude: Pro (CLI)' : 'Claude: API (PAYG)';
    if (!targetReady) {
      if (!window.confirm(`${label} mode is not ready. Switch anyway?`)) return;
    }
    await api.config.update({ anthropic_mode: mode });
    setAnthropicMode(mode);
    setDirty();
    const cfg = await api.config.get();
    setConfig(cfg);
  };

  const handleSetMistralMode = async (mode: string) => {
    if (!readiness) {
      const rdy = await api.config.readiness();
      setReadiness(rdy);
    }
    const targetReady = mode === 'vibe' ? readiness?.vibe : readiness?.mistral_api;
    const label = mode === 'vibe' ? 'Mistral: Vibe' : 'Mistral: API';
    if (!targetReady) {
      if (!window.confirm(`${label} mode is not ready. Switch anyway?`)) return;
    }
    await api.config.update({ mistral_mode: mode });
    setMistralMode(mode);
    setDirty();
    const cfg = await api.config.get();
    setConfig(cfg);
  };

  const handleSaveTokenBudget = async () => {
    const val = parseInt(tokenBudget, 10);
    if (!Number.isFinite(val) || val < 1000 || val > 10_000_000) {
      setBudgetStatus('must be 1,000–10,000,000');
      return;
    }
    setBudgetStatus('saving…');
    try {
      await api.config.update({ claude_pro_token_budget: val });
      setBudgetStatus('saved');
      setKanbanTokenBudget(val);
      setTimeout(() => setBudgetStatus(''), 2000);
      setDirty();
      const cfg = await api.config.get();
      setConfig(cfg);
    } catch {
      setBudgetStatus('network error');
    }
  };

  const saveProjectSettings = async () => {
    if (!activeProject || !canAdminProject) return;
    const current = buildProjectPayload({
      euOnly, monthlyBudget, budgetResetDay, aingelName, aingelModel,
      autopilot, aingelMode, useRag, ragCorpusId,
    });
    const changed = Object.fromEntries(Object.entries(current).filter(
      ([k, v]) => JSON.stringify(v) !== JSON.stringify(loadedPayload.current[k])));
    if (Object.keys(changed).length === 0) {
      setSettingsDirty(false);
      return;
    }
    setSaving(true);
    try {
      await api.projects.update(activeProject, changed);
      loadedPayload.current = current;
      setSettingsDirty(false);
    } catch (e) {
      alert('Error: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setSaving(false);
    }
  };

  const handleClose = async () => {
    if (settingsDirty) {
      const save = window.confirm('You have unsaved changes. Save them before closing?');
      if (save) {
        await saveProjectSettings();
      }
    }
    setSettingsDirty(false);
    setShowSettingsModal(false);
  };

  if (!showSettingsModal) return null;

  const dim = { opacity: 0.55 };
  const readyDot = (ready: boolean | undefined) => (
    <span
      className="inline-block w-2 h-2 rounded-full mr-1"
      style={{ background: ready ? '#56633f' : '#a3402f' }}
    />
  );

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={handleClose}>
      <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong" style={{ minWidth: 520, maxWidth: 620 }} onClick={e => e.stopPropagation()}>
        <h3 className="m-0 mb-4 text-base">Settings</h3>

        <div className="memory-tabs flex p-1 gap-0.5 mb-4">
          <button
            data-tip="Open general settings"
            className={cn(
              'flex-1 py-1 px-2 border-none bg-transparent text-xs cursor-pointer capitalize',
              tab === 'general' && 'active bg-surface-raised rounded-sm font-semibold',
            )}
            onClick={() => setTab('general')}
          >General</button>
          <button
            data-tip="Open project settings"
            className={cn(
              'flex-1 py-1 px-2 border-none bg-transparent text-xs cursor-pointer capitalize',
              tab === 'project' && 'active bg-surface-raised rounded-sm font-semibold',
            )}
            onClick={() => setTab('project')}
          >Project</button>
          <button
            data-tip="Open user accounts"
            className={cn(
              'flex-1 py-1 px-2 border-none bg-transparent text-xs cursor-pointer capitalize',
              tab === 'users' && 'active bg-surface-raised rounded-sm font-semibold',
            )}
            onClick={() => setTab('users')}
          >Users</button>
        </div>

        {tab === 'general' && (
          <div>
            <div className="form-group mb-3">
              <label className="block text-sm font-semibold text-text-soft mb-[3px]">Anthropic mode</label>
              <div className="flex gap-2 items-center">
                <button
                  data-tip={readiness?.claude_code ? 'Claude CLI authenticated' : 'Run `claude` in a terminal to authenticate'}
                  className={cn('btn flex-1 flex items-center justify-center gap-1 py-[7px] px-[18px] border rounded cursor-pointer text-md-', !canConfigGeneral && 'opacity-40 cursor-not-allowed')}
                  style={{
                    borderColor: anthropicMode === 'claude-code' ? '#56633f' : undefined,
                    ...(anthropicMode === 'claude-code' ? {} : dim),
                  }}
                  onClick={() => handleSetAnthropicMode('claude-code')}
                  disabled={!canConfigGeneral}
                >
                  {readyDot(readiness?.claude_code)}
                  Claude CLI (Pro)
                </button>
                <button
                  data-tip={readiness?.api ? 'ANTHROPIC_API_KEY is set' : 'Set ANTHROPIC_API_KEY in .env'}
                  className={cn('btn flex-1 flex items-center justify-center gap-1 py-[7px] px-[18px] border rounded cursor-pointer text-md-', !canConfigGeneral && 'opacity-40 cursor-not-allowed')}
                  style={{
                    borderColor: anthropicMode === 'api' ? '#56633f' : undefined,
                    ...(anthropicMode === 'api' ? {} : dim),
                  }}
                  onClick={() => handleSetAnthropicMode('api')}
                  disabled={!canConfigGeneral}
                >
                  {readyDot(readiness?.api)}
                  Claude API (PAYG)
                </button>
              </div>
            </div>

            <div className="form-group mb-3">
              <label className="block text-sm font-semibold text-text-soft mb-[3px]">Mistral mode</label>
              <div className="flex gap-2 items-center">
                <button
                  data-tip={readiness?.vibe ? 'Vibe CLI installed and MISTRAL_VIBE_KEY set' : 'Install vibe CLI and set MISTRAL_VIBE_KEY'}
                  className={cn('btn flex-1 flex items-center justify-center gap-1 py-[7px] px-[18px] border rounded cursor-pointer text-md-', !canConfigGeneral && 'opacity-40 cursor-not-allowed')}
                  style={{
                    borderColor: mistralMode === 'vibe' ? '#c67139' : undefined,
                    ...(mistralMode === 'vibe' ? {} : dim),
                  }}
                  onClick={() => handleSetMistralMode('vibe')}
                  disabled={!canConfigGeneral}
                >
                  {readyDot(readiness?.vibe)}
                  Vibe CLI
                </button>
                <button
                  data-tip={readiness?.mistral_api ? 'MISTRAL_VIBE_KEY is set' : 'Set MISTRAL_VIBE_KEY in .env'}
                  className={cn('btn flex-1 flex items-center justify-center gap-1 py-[7px] px-[18px] border rounded cursor-pointer text-md-', !canConfigGeneral && 'opacity-40 cursor-not-allowed')}
                  style={{
                    borderColor: mistralMode === 'api' ? '#c67139' : undefined,
                    ...(mistralMode === 'api' ? {} : dim),
                  }}
                  onClick={() => handleSetMistralMode('api')}
                  disabled={!canConfigGeneral}
                >
                  {readyDot(readiness?.mistral_api)}
                  Mistral API (PAYG)
                </button>
              </div>
            </div>

            <div className="form-group mb-3">
              <label className="block text-sm font-semibold text-text-soft mb-[3px]">Claude Pro token budget</label>
              <div className="flex gap-2 items-center">
                <input
                  data-tip="Set Claude Pro monthly token budget"
                  type="number"
                  className={cn('w-40 py-[7px] px-2.5 border border-default rounded text-base', !canConfigGeneral && 'opacity-40 cursor-not-allowed')}
                  value={tokenBudget}
                  onChange={e => { setTokenBudget(e.target.value); setDirty(); }}
                  min={1000}
                  max={10_000_000}
                  placeholder="e.g. 150000"
                  disabled={!canConfigGeneral}
                />
                <button data-tip="Save the token budget" className={cn('btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white', !canConfigGeneral && 'opacity-40 cursor-not-allowed')} onClick={handleSaveTokenBudget} disabled={!canConfigGeneral}>Save</button>
                {budgetStatus && (
                  <span className="text-sm+" style={{ color: budgetStatus === 'saved' ? '#56633f' : budgetStatus.includes('error') ? '#a3402f' : '#645c50' }}>
                    {budgetStatus}
                  </span>
                )}
              </div>
            </div>

            {readiness && (
              <div className="mt-4 p-3 bg-surface-subtle rounded-md text-sm+">
                <div className="font-semibold mb-1.5 text-text-soft">Provider readiness</div>
                {(['claude_code', 'api', 'vibe', 'mistral_api', 'scaleway', 'ollama'] as const).map(k => (
                  <div key={k} className="flex items-center gap-1.5 mb-0.5">
                    {readyDot(readiness[k])}
                    <span className="capitalize">{k.replace(/_/g, ' ')}</span>
                    <span className="text-xs" style={{ color: readiness[k] ? '#56633f' : '#a3402f' }}>
                      {readiness[k] ? 'ready' : 'not ready'}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === 'project' && (
          <div>
            {!activeProject ? (
              <div className="text-sm+ text-text-soft text-center p-5">
                Select a project to view its settings.
              </div>
            ) : (
              <>
                {ro && (
                  <div className="mb-3 p-3 rounded-md border border-border-muted text-sm text-text-soft">
                    You have read-only access to this project.
                  </div>
                )}
                <div className="form-group mb-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">Name</label>
                  <input data-tip="Project name (read-only)" type="text" className="w-full py-[7px] px-2.5 border border-default rounded text-base opacity-60" value={projects.find(p => p.id === activeProject)?.name || ''} disabled />
                </div>
                <div className="form-group mb-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">Project type <span className="font-normal text-text-faint">— template used at creation</span></label>
                  <input data-tip="Role template applied when the project was created; changing it later would not change the roles. A Legal project also consults the legal library on every task when the RAG library is on." type="text" className="w-full py-[7px] px-2.5 border border-default rounded text-base opacity-60" value={ptype || '— None —'} disabled />
                </div>
                <div className="form-group mb-3">
                  <label className="inline-flex items-center gap-2 cursor-pointer">
                    <input data-tip="Restrict this project to EU providers" type="checkbox" className={roClass} checked={euOnly} onChange={e => { setEuOnly(e.target.checked); setDirty(); }} disabled={ro} />
                    EU only (Scaleway + Mistral)
                  </label>
                </div>
                <div className="form-group mb-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">Monthly budget (USD) <span className="font-normal text-text-faint">— leave 0 to disable</span></label>
                  <input data-tip="Set monthly budget (USD, 0 disables)" type="number" className={cn('w-35 py-[7px] px-2.5 border border-default rounded text-base', roClass)} value={monthlyBudget} onChange={e => { setMonthlyBudget(e.target.value); setDirty(); }} min={0} step={0.01} style={{ width: 140 }} disabled={ro} />
                </div>
                <div className="form-group mb-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">Budget reset day <span className="font-normal text-text-faint">(1–28)</span></label>
                  <input data-tip="Set the budget reset day (1–28)" type="number" className={cn('py-[7px] px-2.5 border border-default rounded text-base', roClass)} value={budgetResetDay} onChange={e => { setBudgetResetDay(e.target.value); setDirty(); }} min={1} max={28} step={1} style={{ width: 80 }} disabled={ro} />
                </div>
                <div className="form-group mb-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">Guide name <span className="font-normal text-text-faint">— optional AI persona name</span></label>
                  <input data-tip="Set the AI persona name" type="text" className={cn('py-[7px] px-2.5 border border-default rounded text-base', roClass)} value={aingelName} onChange={e => { setAingelName(e.target.value); setDirty(); }} placeholder="e.g. Léa" style={{ width: 160 }} disabled={ro} />
                </div>
                <div className="form-group mb-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">Guide model <span className="font-normal text-text-faint">— chat & brief model</span></label>
                  <select data-tip="Set the chat & brief model" className={cn('w-full py-[7px] px-2.5 border border-default rounded text-base', roClass)} value={aingelModel} onChange={e => { setAingelModel(e.target.value); setDirty(); }} disabled={ro}>
                    <option value="">{euOnly ? '— Default (EU) —' : '— Default (Sonnet) —'}</option>
                    {aingelModelChoices.map(m => (
                      <option key={m.id} value={m.id} disabled={isFreeUser && m.free_allowed === false}>{m.label}{isFreeUser && m.free_allowed === false ? ' (Upgrade)' : ''}</option>
                    ))}
                  </select>
                </div>
                <div className="form-group mb-3 border-t border-border-muted pt-3 mt-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">Guide autopilot</label>
                  <label className="inline-flex items-center gap-2 cursor-pointer mt-1 font-normal">
                    <input data-tip="Enable guide autopilot oversight" type="checkbox" className={roClass} checked={autopilot} onChange={e => { setAutopilot(e.target.checked); setDirty(); }} disabled={ro} />
                    Enable autopilot oversight
                  </label>
                  <div className="mt-2">
                    <label className="block text-sm font-semibold text-text-soft mb-[3px]">Guide mode</label>
                    <select data-tip="Strict: a Guide hold stops the run. Advisory: a hold is only a warning." className={cn('w-full py-[7px] px-2.5 border border-default rounded text-base', roClass)} value={aingelMode} onChange={e => { setAingelMode(e.target.value); setDirty(); }} disabled={ro || !autopilot}>
                      <option value="strict">Strict — a Guide hold stops the run</option>
                      <option value="advisory">Advisory — a Guide hold is only a warning</option>
                    </select>
                  </div>
                </div>

                <div className="form-group mb-3 border-t border-border-muted pt-3 mt-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">RAG library</label>
                  <label className="inline-flex items-center gap-2 cursor-pointer mt-1 font-normal">
                    <input data-tip="Make the legal RAG library available" type="checkbox" className={roClass} checked={useRag} onChange={e => { setUseRag(e.target.checked); setDirty(); }} disabled={ro} />
                    Make the shared legal library available to this project
                  </label>
                  {useRag && (
                    <div className="mt-2">
                      <label className="block text-sm font-semibold text-text-soft mb-[3px]">Default corpus</label>
                      <select
                        data-tip="Choose the default RAG corpus"
                        className={cn('w-full py-[7px] px-2.5 border border-default rounded text-base', roClass)}
                        value={ragCorpusId}
                        onChange={e => { setRagCorpusId(e.target.value); setDirty(); }}
                        disabled={ro}
                      >
                        {(ragCorpora ?? [{ id: 'railway', label: 'railway — EU→PL railway law' }]).map((c) => (
                          <option key={c.id} value={c.id}>{c.label}</option>
                        ))}
                      </select>
                      <p className="text-2xs text-text-faint mt-1">
                        RAG is a shared library (factory /opt/RAG). Tasks can still opt in/out per task.
                      </p>
                    </div>
                  )}
                </div>

                {(isVault || euOnly || scwStatus?.enabled) && (
                  <div className="form-group mb-3 border-t border-border-muted pt-3 mt-3">
                    <label className="block text-sm font-semibold text-text-soft mb-[3px]">Scaleway Session</label>
                    {scwStatus?.enabled ? (
                      <div className="text-sm+ text-text-soft">
                        <div className="my-0.5">Bucket: <span className="font-mono text-xs">{scwStatus.bucket}</span></div>
                        <div className="my-0.5">KMS key: <span className="font-mono text-xs">{scwStatus.kms_key_id}</span></div>
                        <div className="my-0.5">Region: {scwStatus.region}</div>
                        {scwStatus.created_at && <div className="my-0.5">Created: {new Date(scwStatus.created_at).toLocaleString()}</div>}
                        {scwStatus.costs && scwStatus.costs.length > 0 && (
                          <div className="my-1">
                            <div className="text-xs uppercase text-text-faint mb-1">Session costs</div>
                            {scwStatus.costs.map((c, i) => (
                              <div key={i} className="text-xs flex justify-between">
                                <span>{c.day} · {c.category}</span>
                                <span>{fmtEur(c.eur)} / {fmtUsd(c.usd)}</span>
                              </div>
                            ))}
                          </div>
                        )}
                        <div className="my-2">
                          <div className="text-xs uppercase text-text-faint mb-1">Files in bucket (read-only)</div>
                          {sessionFiles.length === 0 ? (
                            <div className="text-xs text-text-faint">No files in bucket.</div>
                          ) : (
                            <div className="space-y-0.5 max-h-[120px] overflow-y-auto pr-1">
                              {sessionFiles.map((f, i) => (
                                <div key={i} className="text-xs flex justify-between">
                                  <span className="font-mono truncate pr-2">{f.key}</span>
                                  <span className="text-text-faint shrink-0">{(f.size / 1024).toFixed(1)} KB</span>
                                </div>
                              ))}
                            </div>
                          )}
                          <div className="mt-2 text-xs text-text-faint bg-surface-subtle border border-border-subtle rounded px-2 py-1.5">
                            Use the <span className="font-medium">Files tab</span> for uploads — bucket sync is automatic. Existing files were synced to <span className="font-mono">Working Documents/</span>.
                          </div>
                          {uploadProgress ? <div className="text-xs text-success mt-1">{uploadProgress}</div> : null}
                        </div>
                        {!ro && (
                          <div className="my-1 flex items-center gap-2">
                            <button
                              data-tip="Download bucket contents to Working Documents"
                              className="btn py-1 px-2.5 border border-default rounded cursor-pointer text-xs bg-surface-raised text-text-soft hover:bg-surface-muted"
                              onClick={handleSyncBucket}
                              disabled={scwBusy || uploading}
                            >{scwBusy ? 'Syncing…' : 'Sync bucket'}</button>
                            <span className="text-2xs text-text-faint">Bucket → <span className="font-mono">Working Documents/</span></span>
                          </div>
                        )}
                        {!ro && (
                          <button
                            data-tip="Close session and crypto-shred its data"
                            className="btn mt-2 py-1 px-3 border border-danger rounded cursor-pointer text-sm- text-danger"
                            onClick={handleCloseSession}
                            disabled={scwBusy}
                          >{scwBusy ? 'Closing…' : 'Close Session'}</button>
                        )}
                      </div>
                    ) : (
                      <div>
                        <div className="text-xs text-text-faint mb-2">Provisions a dedicated Scaleway Project with KMS-encrypted bucket. Data is crypto-shredded when closed.</div>
                        {!ro && (
                          <button
                            data-tip="Provision an encrypted Scaleway session"
                            className="btn py-1 px-3 border border-accent rounded cursor-pointer text-sm- bg-accent text-white"
                            onClick={handleEnableSession}
                            disabled={scwBusy}
                          >{scwBusy ? 'Provisioning…' : 'Enable Scaleway Session'}</button>
                        )}
                      </div>
                    )}
                  </div>
                )}

                <div className="form-group mb-3 border-t border-border-muted pt-3 mt-3">
                  <label className="block text-sm font-semibold text-text-soft mb-[3px]">Hugging Face Scout</label>
                  <div className="text-xs text-text-faint mb-2">
                    Discover specialised models (legal, finance, medical…), validate them, and adopt them for this project.
                  </div>
                  <div className="flex gap-2 items-center mb-2">
                    <input
                      data-tip="Search Hugging Face for models"
                      type="text"
                      className="flex-1 py-[7px] px-2.5 border border-default rounded text-base"
                      value={hfQuery}
                      onChange={e => setHfQuery(e.target.value)}
                      placeholder="e.g. Polish legal BERT"
                      onKeyDown={e => { if (e.key === 'Enter') handleHfSearch(); }}
                    />
                    <button data-tip="Search Hugging Face models" className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white" onClick={handleHfSearch} disabled={hfSearching}>
                      {hfSearching ? 'Searching…' : 'Search'}
                    </button>
                  </div>
                  {hfResults && (
                    <div className="space-y-1 max-h-48 overflow-y-auto">
                      {hfResults.length === 0 && <div className="text-xs text-text-faint">No models found.</div>}
                      {hfResults.map(r => (
                        <div key={r.repo_id} className="flex items-start justify-between gap-2 py-1 border-b border-border-subtle last:border-0">
                          <div className="min-w-0">
                            <a
                              href={r.hf_url}
                              target="_blank"
                              rel="noreferrer"
                              className="font-mono text-xs text-accent underline"
                            >
                              {r.repo_id} ↗
                            </a>
                            <span className="text-2xs px-1 rounded ml-1" style={{ background: r.validation.score >= 0.6 ? '#e1eecc' : '#fff2eb', color: '#000' }}>
                              {Math.round(r.validation.score * 100)}
                            </span>
                            {r.eu && <span className="text-2xs ml-1" title="EU-servable">🇪🇺</span>}
                            <div className="text-xs text-text-faint">{r.validation.reasons.slice(0, 3).join(' · ')}</div>
                          </div>
                          <div className="flex items-center gap-1 shrink-0">
                            {!ro && (
                              <>
                                <button data-tip="Adopt this model for the project" className="btn py-0.5 px-2 border border-accent rounded cursor-pointer text-xs text-accent" onClick={() => handleHfAdopt(r.repo_id, false)} disabled={hfBusy}>Adopt</button>
                                <button data-tip="Import into the Scaleway model library (usable by any project)" className="btn py-0.5 px-2 border border-default rounded cursor-pointer text-xs" onClick={() => { setImportRepo(r.repo_id); handleImportVerify(r.repo_id); }} disabled={importBusy}>Import</button>
                                {r.validation.servable && (
                                  <button data-tip="Adopt and set as default model" className="btn py-0.5 px-2 border border-accent rounded cursor-pointer text-xs bg-accent text-white" onClick={() => handleHfAdopt(r.repo_id, true)} disabled={hfBusy}>Adopt + default</button>
                                )}
                              </>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                  {importRepo && (
                    <div className="mt-2 p-2.5 bg-surface-subtle rounded-md">
                      <div className="text-xs text-text-soft mb-1">
                        Import <span className="font-mono">{importRepo}</span> to the Scaleway model library
                        <span className="text-text-faint"> — once ready, any project can deploy it on a GPU window.</span>
                      </div>
                      {importVerify?.ok && (
                        <div className="text-xs text-status-running mb-1">
                          Importable — nodes {importVerify.nodes?.join(', ') || '—'}
                          {importVerify.max_context_size != null && ` · up to ${importVerify.max_context_size.toLocaleString()} ctx`}
                          {importVerify.size_bytes != null && ` · ${(importVerify.size_bytes / 1e9).toFixed(1)} GB`}
                        </div>
                      )}
                      {importError && (
                        <div className="p-2 rounded text-xs bg-status-pending/20 text-status-pending mb-1">{importError}</div>
                      )}
                      <div className="flex gap-2">
                        <button
                          data-tip="Import this repo into the Scaleway model library"
                          className="btn btn-primary py-1 px-3 border border-accent rounded cursor-pointer text-sm- bg-accent text-white"
                          onClick={() => handleImport(importRepo)}
                          disabled={importBusy || (importVerify ? !importVerify.ok : false)}
                        >{importBusy ? 'Working…' : 'Import to Scaleway'}</button>
                        <button
                          data-tip="Cancel import"
                          className="btn py-1 px-3 border border-default rounded cursor-pointer text-sm-"
                          onClick={() => { setImportRepo(''); setImportVerify(null); setImportError(null); }}
                        >Cancel</button>
                      </div>
                    </div>
                  )}
                  {hfRoster.length > 0 && (
                    <div className="mt-2">
                      <div className="text-xs uppercase text-text-faint mb-1">Adopted for this project</div>
                      {hfRoster.map(m => (
                        <div key={m.repo_id} className="flex items-center justify-between gap-2 py-1 border-b border-border-subtle last:border-0">
                          <span className="min-w-0 truncate">
                            <a
                              href={m.hf_url}
                              target="_blank"
                              rel="noreferrer"
                              className="font-mono text-xs text-accent underline"
                            >
                              {m.repo_id} ↗
                            </a>
                            {m.is_default && <span className="text-2xs px-1 rounded ml-1" style={{ background: '#e1eecc', color: '#000' }}>Default</span>}
                            {m.import_status && (
                              <span
                                className={cn('text-2xs px-1 rounded ml-1',
                                  m.import_status === 'ready' ? 'bg-status-running/20 text-status-running'
                                    : m.import_status === 'error' || m.import_status === 'failed' ? 'bg-danger/20 text-danger'
                                    : 'bg-surface text-text-muted')}
                                title={m.import_error || ''}
                              >
                                {m.import_status === 'ready' ? 'imported' : `import: ${m.import_status}`}
                              </span>
                            )}
                            {!m.import_status && m.model_id === '' && (
                              <span className="text-2xs px-1 rounded ml-1 bg-surface text-text-muted" title="Fine-tune without a serverless mapping — can run on a GPU window">needs GPU window</span>
                            )}
                            {m.limitations && m.limitations.length > 0 && (
                              <div className="text-2xs text-text-faint">{m.limitations.join(' · ')}</div>
                            )}
                          </span>
                          <span className="flex items-center gap-1 shrink-0">
                            {!ro && (
                              <>
                                {!m.is_default && m.model_id && (
                                  <button data-tip="Set as the default model" className="btn py-0.5 px-2 border border-default rounded cursor-pointer text-xs" onClick={() => handleHfAdopt(m.repo_id, true)} disabled={hfBusy}>Make default</button>
                                )}
                                {!m.import_status && canAdminProject && (
                                  <button data-tip="Import into the Scaleway model library (usable by any project)" className="btn py-0.5 px-2 border border-default rounded cursor-pointer text-xs" onClick={() => { setImportRepo(m.repo_id); handleImportVerify(m.repo_id); }} disabled={importBusy}>Import</button>
                                )}
                                {m.import_id && (m.import_status === 'error' || m.import_status === 'failed') && user?.role === 'admin' && (
                                  <button data-tip="Delete the imported model from Scaleway" className="btn py-0.5 px-2 border border-danger rounded cursor-pointer text-xs text-danger" onClick={() => handleImportDelete(m.import_id as number)} disabled={importBusy}>Delete import</button>
                                )}
                                <button data-tip="Remove this model from the project" className="btn py-0.5 px-2 border border-danger rounded cursor-pointer text-xs text-danger" onClick={() => handleHfRemove(m.repo_id)} disabled={hfBusy}>Remove</button>
                              </>
                            )}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        )}

        {tab === 'users' && <SettingsUsersTab />}

        <div className="modal-actions flex justify-end items-center gap-2 mt-4">
          {/* AGPL-3.0 §13: network users must be offered the source. */}
          <span className="mr-auto text-xs text-text-faint">
            Cordée · <a href={`${SOURCE_URL}/blob/main/LICENSE`} target="_blank" rel="noreferrer">AGPL-3.0</a> · <a href={SOURCE_URL} target="_blank" rel="noreferrer">Source</a>
          </span>
          <button data-tip="Close settings" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={handleClose}>{settingsDirty ? 'Cancel (unsaved)' : 'Close'}</button>
          {tab === 'project' && activeProject && canAdminProject && (
            <button data-tip="Save project settings" className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white" onClick={saveProjectSettings} disabled={saving}>
              {saving ? 'Saving…' : 'Save Project'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default SettingsModal;
