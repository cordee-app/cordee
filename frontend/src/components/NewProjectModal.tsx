import { useState, useEffect, useCallback, useRef } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import type { Model } from '../types';

const isVault = Boolean((typeof window !== 'undefined' && (window as unknown as { __AINGEL_VAULT__?: boolean }).__AINGEL_VAULT__) || false);

export const NewProjectModal = () => {
  const {
    showNewProjectModal, setShowNewProjectModal,
    models, roleTemplates,
    npFilesPicked, setNpFilesPicked,
    npImprovedText, setNpImprovedText,
    setProjects, setTasks, setPhasesData, setChats,
    setActiveProject,
    setScaffoldPid, setScaffoldChatId, setScaffoldName,
  } = useStore();

  const [name, setName] = useState('');
  const [pitch, setPitch] = useState('');
  const [stackHints, setStackHints] = useState('');
  const [scaffoldingModel, setScaffoldingModel] = useState('');
  const [projectType, setProjectType] = useState('');
  const [euOnly, setEuOnly] = useState(false);
  const [aingelNameVal, setAingelNameVal] = useState('');
  const [aingelModelVal, setAingelModelVal] = useState('');
  const [scwSession, setScwSession] = useState(false);
  const [provisioning, setProvisioning] = useState(false);
  const [improving, setImproving] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [showImproved, setShowImproved] = useState(false);

  const filteredScaffoldModels = useCallback(() => {
    if (euOnly) {
      return models.filter(m => m.id.startsWith('scw-') || m.id.startsWith('mistral-') || m.id.startsWith('open-mistral'));
    }
    // Non‑EU: expose all models (including Mistral Medium) for scaffolding.
    return models;
  }, [models, euOnly]);

  const filteredAingelModels = useCallback(() => {
    if (euOnly) {
      return models.filter(m => m.id.startsWith('scw-') || m.id.startsWith('mistral-') || m.id.startsWith('open-mistral'));
    }
    // Non‑EU: expose Claude families and both Mistral Large and Medium for AIngel.
    const ok = (id: string) =>
      id.startsWith('claude-sonnet') ||
      id.startsWith('claude-opus') ||
      id.startsWith('claude-fable') ||
      id.startsWith('mistral-large') ||
      id.startsWith('mistral-medium');
    return models.filter(m => ok(m.id));
  }, [models, euOnly]);

  const reset = useCallback(() => {
    setName('');
    setPitch('');
    setStackHints('');
    setScaffoldingModel('');
    setProjectType('');
    setEuOnly(false);
    setAingelNameVal('');
    setAingelModelVal('');
    setScwSession(false);
    setProvisioning(false);
    setShowImproved(false);
    setNpImprovedText('');
    setNpFilesPicked([]);
    setImproving(false);
    setSubmitting(false);
  }, [setNpImprovedText, setNpFilesPicked]);

  const wasOpen = useRef(false);

  useEffect(() => {
    if (showNewProjectModal && !wasOpen.current) {
      reset();
    }
    wasOpen.current = showNewProjectModal;
  }, [showNewProjectModal, reset]);

  // Pre-select the flagged default model, but never one the user cannot see.
  // The picker is filtered to EU providers when `euOnly` is set, and choosing
  // from the unfiltered list there selected claude-sonnet-4-6 while the <select>
  // rendered "— Default —" (its id matches no visible option) — so creation was
  // refused by the EU guard with no visible cause. `handleEuChange` clearing the
  // selection did not help: this effect re-set it on the very next tick.
  // Anything not in the visible list falls back to "— Default —", letting the
  // server resolve it through agent_config.default_model_for().
  useEffect(() => {
    if (!showNewProjectModal || models.length === 0) return;
    const visible = filteredScaffoldModels();
    if (scaffoldingModel) {
      if (!visible.some(m => m.id === scaffoldingModel)) setScaffoldingModel('');
      return;
    }
    const def = visible.find(m => m.default);
    if (def) setScaffoldingModel(def.id);
  }, [showNewProjectModal, models, scaffoldingModel, filteredScaffoldModels]);

  if (!showNewProjectModal) return null;

  const canSubmit = name.trim() && pitch.trim();
  const template = roleTemplates.find(t => String(t.id) === projectType);

  const handleEuChange = (checked: boolean) => {
    setEuOnly(checked);
    setScaffoldingModel('');
    setAingelModelVal('');
    if (!checked) setScwSession(false);
  };

  const handleScwSessionChange = (checked: boolean) => {
    setScwSession(checked);
    if (checked) {
      setEuOnly(true);
    }
  };

  const openFilePicker = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.multiple = true;
    input.accept = '.md,.txt,.json,.yaml,.yml,.toml,.csv,.html,.css,.js,.ts,.py,.java,.c,.cpp,.h,.hpp,.rs,.go,.rb,.php,.sql,.xml,.sh,.bat,.ps1,.cfg,.ini,.env,.proto,.graphql,.tsx,.jsx,.vue,.svelte,.dart,.swift,.kt,.scala';
    input.onchange = async () => {
      const files = input.files;
      if (!files) return;
      const picked = [...npFilesPicked];
      for (let i = 0; i < files.length; i++) {
        try {
          const content = await files[i].text();
          picked.push({ name: files[i].name, content });
        } catch { /* skip unreadable */ }
      }
      setNpFilesPicked(picked);
    };
    input.click();
  };

  const removeFile = (idx: number) => {
    const next = [...npFilesPicked];
    next.splice(idx, 1);
    setNpFilesPicked(next);
  };

  const handleImprove = async () => {
    setImproving(true);
    try {
      const result = await api.improvePrompt({
        title: name || 'Project',
        description: pitch || '(none provided)',
        stack: stackHints || '(none provided)',
        project_id: undefined,
        files: npFilesPicked,
      });
      setNpImprovedText(result.improved);
      setShowImproved(true);
    } catch (e) {
      alert('Failed to improve: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setImproving(false);
    }
  };

  const useImproved = () => {
    if (npImprovedText) setPitch(npImprovedText);
    setShowImproved(false);
    setNpImprovedText('');
  };

  const cancelImproved = () => {
    setShowImproved(false);
    setNpImprovedText('');
  };

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const result = await api.projects.create({
        name: name.trim(),
        pitch: pitch.trim(),
        stack_hints: stackHints.trim() || undefined,
        scaffolding_model: scaffoldingModel || undefined,
        project_type: template?.name || undefined,
        eu_only: scwSession ? true : euOnly,
        aingel_name: aingelNameVal.trim() || undefined,
        aingel_model: aingelModelVal || undefined,
      });

      if (projectType && template && result.project?.id) {
        try {
          await api.projects.roles.fromTemplate(result.project.id, template.id);
        } catch { /* non-fatal */ }
      }

      if (scwSession && result.project?.id) {
        setProvisioning(true);
        try {
          await api.projects.scwSession.create(result.project.id);
        } catch (e) {
          alert('Scaleway session provisioning failed: ' + (e instanceof Error ? e.message : 'unknown'));
        } finally {
          setProvisioning(false);
        }
      }

      setShowNewProjectModal(false);
      reset();

      // Keep the scaffolding chat visible in the Chats sidebar. Do NOT open the
      // AIngel panel here — it sits at z-1100 and would hide the ScaffoldProgress
      // modal (z-1000), making the draft logs and "Apply Guide" button invisible.
      // The user can open the AIngel panel or the Chats sidebar after the
      // scaffolding modal is dismissed.
      if (result.project?.id) {
        setActiveProject(result.project.id);
        setScaffoldPid(result.project.id);
        setScaffoldChatId(result.chat?.id ?? null);
        setScaffoldName(result.project.name || name.trim());
      }

      // Load the rest of the data in the background – this can take a while but the UI is already responsive.
      const [projs, t, chatsList, phases] = await Promise.all([
        api.projects.list(),
        api.tasks.list(),
        api.chats.list({ status: 'active' }),
        api.phases.list(),
      ]);
      setProjects(projs);
      setTasks(t);
      setChats(chatsList);
      setPhasesData(phases);
    } catch (e) {
      alert('Error: ' + (e instanceof Error ? e.message : 'unknown'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={(e) => { if (e.target === e.currentTarget) setShowNewProjectModal(false); }}>
      <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong" style={{ minWidth: 560, maxWidth: 680 }} onClick={e => e.stopPropagation()}>
        <h3 className="m-0 mb-4 text-base">New Project</h3>

        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Project name</label>
          <input type="text" data-tip="Enter the project name" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. My SaaS App" />
        </div>

        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Pitch <span className="font-normal text-text-faint">— one paragraph describing the project</span></label>
          <textarea data-tip="Describe what you want to build" className="w-full py-[7px] px-2.5 border border-default rounded text-base" style={{ resize: 'vertical' }} value={pitch} onChange={e => setPitch(e.target.value)} rows={3} placeholder="Describe what you want to build…" />
        </div>

        <div className="flex gap-2 mb-3">
          <button data-tip="Improve my prompt with AI" className="header-btn header-btn-primary px-3.5 py-1 border rounded text-white text-md- font-heading font-normal cursor-pointer" onClick={handleImprove} disabled={improving || !pitch.trim()}>
            <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" style={{ opacity: 1 }}><circle cx="12" cy="12" r="12" fill="#fbf6ee"/><circle cx="12" cy="12" r="7.5" fill="none" stroke="#c67139" strokeWidth="3"/><circle cx="12" cy="12" r="2.4" fill="#c67139"/></svg>
            {improving ? 'Guiding…' : 'Guide my prompt'}
          </button>
          <span className="text-sm+ text-text-faint self-center">
            AI will improve your pitch with clearer specs
          </span>
        </div>

        {showImproved && npImprovedText && (
          <div className="mb-3 p-2.5 bg-accent-tint rounded-md text-sm whitespace-pre-wrap max-h-40 overflow-y-auto">
            {npImprovedText}
            <div className="mt-2 flex gap-2">
              <button data-tip="Replace pitch with the improved version" className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white" onClick={useImproved}>Use this</button>
              <button data-tip="Keep my original pitch" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={cancelImproved}>Discard</button>
            </div>
          </div>
        )}

        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Stack hints <span className="font-normal text-text-faint">— optional: languages, frameworks, tools</span></label>
          <input type="text" data-tip="Optional languages and frameworks" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={stackHints} onChange={e => setStackHints(e.target.value)} placeholder="e.g. React, FastAPI, PostgreSQL" />
        </div>

        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Scaffolding model</label>
          <select data-tip="Choose the model that scaffolds the project" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={scaffoldingModel} onChange={e => setScaffoldingModel(e.target.value)}>
            <option value="">— Default —</option>
            {filteredScaffoldModels().map((m: Model) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
        </div>

        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Project type</label>
          <select data-tip="Apply a role template to the project" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={projectType} onChange={e => setProjectType(e.target.value)}>
            <option value="">— None —</option>
            {roleTemplates.map(t => (
              <option key={t.id} value={t.id}>{t.name}</option>
            ))}
          </select>
          {template && (() => {
            const tmpl = template as unknown as Record<string, unknown>;
            const roleNames = tmpl.role_names;
            return (
            <div className="text-xs text-text-soft mt-1">
              Roles: {Array.isArray(roleNames) ? roleNames.join(', ') : '(none)'}
            </div>
            );
          })()}
        </div>

        <div className="form-group mb-3">
          <label className="inline-flex items-center gap-2 cursor-pointer">
            <input type="checkbox" data-tip="Restrict the project to EU providers" checked={euOnly} onChange={e => handleEuChange(e.target.checked)} />
            EU only (Scaleway + Mistral)
          </label>
        </div>

        {(isVault || euOnly) && (
          <div className="form-group mb-3">
            <label className="inline-flex items-center gap-2 cursor-pointer">
              <input type="checkbox" data-tip="Use an EU Scaleway session for confidential data" checked={scwSession} onChange={e => handleScwSessionChange(e.target.checked)} />
              Confidential — EU Scaleway session
            </label>
            <div className="text-xs text-text-faint mt-1">
              Provisions a dedicated Scaleway Project with KMS-encrypted bucket. Data is crypto-shredded when the session is closed.
            </div>
          </div>
        )}

        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Guide name <span className="font-normal text-text-faint">— optional AI persona name</span></label>
          <input type="text" data-tip="Name your AI guide persona" className="py-[7px] px-2.5 border border-default rounded text-base" value={aingelNameVal} onChange={e => setAingelNameVal(e.target.value)} placeholder="e.g. Léa" style={{ width: 160 }} />
        </div>

        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Guide model</label>
          <select data-tip="Choose the AI model driving the guide" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={aingelModelVal} onChange={e => setAingelModelVal(e.target.value)}>
            <option value="">— None for now —</option>
            {filteredAingelModels().map((m: Model) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
        </div>

        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Reference files</label>
          <div className="border border-dashed border-border-strong rounded-md p-3 text-center">
            <button data-tip="Pick reference files to guide scaffolding" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={openFilePicker}>Choose files</button>
            <div className="text-xs text-text-faint mt-1">
              Upload spec files, design docs, or examples to improve the scaffolding
            </div>
          </div>
          {npFilesPicked.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {npFilesPicked.map((f, i) => (
                <span key={i} className="inline-flex items-center gap-[3px] rounded text-sm+ text-text-soft px-1.5 py-px" style={{ background: 'rgba(100,120,200,.12)' }}>
                  {f.name}
                  <span className="cursor-pointer text-danger text-xs ml-0.5" onClick={() => removeFile(i)} title="Remove">{'\u2715'}</span>
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button data-tip="Close without creating a project" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={() => setShowNewProjectModal(false)}>Cancel</button>
          <button data-tip="Create the project and start scaffolding" className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white" onClick={handleSubmit} disabled={!canSubmit || submitting || provisioning}>
            {provisioning ? 'Provisioning Scaleway session…' : submitting ? 'Creating project…' : 'Create Project'}
          </button>
        </div>
      </div>

    </div>
  );
};

export default NewProjectModal;
