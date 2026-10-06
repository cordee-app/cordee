import { useState, useEffect, useMemo, useCallback } from 'react';
import type { ReactElement } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { cn } from '../utils/cn';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import type { Task, TaskDependencies, ModelRecommendation, TaskAttachment, HfCandidate } from '../types';
import { TaskAttachPicker } from './TaskAttachPicker';

interface PhaseDataItem {
  project: string;
  path: string;
  phases: { title: string }[];
}

const DESC_LIMIT = 10000;

// Counselor tier → badge colour. Keys match model_profiles.json tier names.
const TIER_COLORS: Record<string, string> = {
  frontier: '#7a5aa6',
  strong: '#3f628f',
  mid: '#0891b2',
  light: '#645c50',
};

// Counselor recommendation category → badge label + colour.
const CATEGORY_META: Record<string, { label: string; bg: string; fg: string }> = {
  best_outcome: { label: 'Best outcome', bg: '#e1eecc', fg: '#000' },
  balanced: { label: 'Balanced', bg: '#3f628f', fg: '#fff' },
  best_price: { label: 'Best price', bg: '#fff2eb', fg: '#000' },
};

export const AddTaskModal = () => {
  const storeRef = useStore;

  const showAddModal = useStore((s) => s.showAddModal);
  const setShowAddModal = useStore((s) => s.setShowAddModal);
  const modModalTaskId = useStore((s) => s.modModalTaskId);
  const setModModalTaskId = useStore((s) => s.setModModalTaskId);
  const projects = useStore((s) => s.projects);
  const activeProject = useStore((s) => s.activeProject);
  const models = useStore((s) => s.models);
  const roles = useStore((s) => s.roles);
  const phasesData = useStore((s) => s.phasesData);
  const tasks = useStore((s) => s.tasks);
  const addTaskPendingDeps = useStore((s) => s.addTaskPendingDeps);
  const setAddTaskPendingDeps = useStore((s) => s.setAddTaskPendingDeps);
  const addTaskSubmitting = useStore((s) => s.addTaskSubmitting);
  const setAddTaskSubmitting = useStore((s) => s.setAddTaskSubmitting);
  const addImprovedApplied = useStore((s) => s.addImprovedApplied);
  const setAddImprovedApplied = useStore((s) => s.setAddImprovedApplied);
  const addTaskNudgeShown = useStore((s) => s.addTaskNudgeShown);
  const setAddTaskNudgeShown = useStore((s) => s.setAddTaskNudgeShown);
  const setProjects = useStore((s) => s.setProjects);
  const setTasks = useStore((s) => s.setTasks);
  const setExecutions = useStore((s) => s.setExecutions);
  const setPhasesData = useStore((s) => s.setPhasesData);
  const setActiveProject = useStore((s) => s.setActiveProject);
  const setActivePhase = useStore((s) => s.setActivePhase);
  const setActiveStatus = useStore((s) => s.setActiveStatus);
  const user = useStore((s) => s.user);

  const isFreeUser = user?.plan === 'free' && user?.role !== 'admin';

  const isEdit = modModalTaskId !== null;
  const isOpen = showAddModal || isEdit;

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [model, setModel] = useState('');
  const [roleId, setRoleId] = useState<number | ''>('');
  const [phase, setPhase] = useState('');
  const [estimatedTokens, setEstimatedTokens] = useState(50000);
  const [projectId, setProjectId] = useState<number | null>(null);

  const [improvedTextLocal, setImprovedTextLocal] = useState('');
  const [improveLoading, setImproveLoading] = useState(false);
  const [editDescWasEmpty, setEditDescWasEmpty] = useState(false);
  // The user's own text before the first "Guide my prompt" was accepted —
  // saved as original_description so added steps can be traced.
  const [preImproveDescription, setPreImproveDescription] = useState('');
  const [attachments, setAttachments] = useState<TaskAttachment[]>([]);
  const [showAttachPicker, setShowAttachPicker] = useState(false);

  const [suggestLoading, setSuggestLoading] = useState(false);
  const [suggestResults, setSuggestResults] = useState<ModelRecommendation[] | null>(null);
  const [suggestTaskType, setSuggestTaskType] = useState('');
  const [suggestReviewed, setSuggestReviewed] = useState('');
  const [hfCandidates, setHfCandidates] = useState<HfCandidate[]>([]);
  const [hfRepoId, setHfRepoId] = useState('');
  const [awaitingModel, setAwaitingModel] = useState(false);
  const [adoptedHf, setAdoptedHf] = useState<HfCandidate[]>([]);

  const [editDeps, setEditDeps] = useState<TaskDependencies | null>(null);
  const [depsLoading, setDepsLoading] = useState(false);
  const [showDepsPicker, setShowDepsPicker] = useState(false);
  const [depsSearch, setDepsSearch] = useState('');
  const [depsError, setDepsError] = useState('');
  const [depsAddInput, setDepsAddInput] = useState('');

  const [submitting, setSubmitting] = useState(false);

  // Lane B: explicit context refs (file catalog picker)
  const [contextRefs, setContextRefs] = useState<string[]>([]);
  const [catalogFiles, setCatalogFiles] = useState<string[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [showContextPicker, setShowContextPicker] = useState(false);
  const [contextSearch, setContextSearch] = useState('');

  // RAG: per-task opt-in + corpus override (library is shared across projects).
  const [requiresRag, setRequiresRag] = useState(false);
  const [corpusId, setCorpusId] = useState('railway');
  const [ragCorpora, setRagCorpora] = useState<{ id: string; label: string }[] | null>(null);

  const task = isEdit ? tasks.find((t) => t.id === modModalTaskId) : undefined;
  const remaining = DESC_LIMIT - description.length;
  const phaseDataItems = phasesData as unknown as PhaseDataItem[];
  const project = projectId !== null ? projects.find((p) => p.id === projectId) : undefined;
  const perms = useProjectPermissions(task?.project_id ?? projectId ?? activeProject);

  // EU-only data residency: such projects may only run Mistral / Scaleway models.
  // Keep this in sync with agent_api._is_eu_model.
  const isEuModel = (id: string) =>
    id.startsWith('scw-') || id.startsWith('mistral-') || id.startsWith('open-mistral')
    || id.startsWith('codestral-') || id.startsWith('devstral-');
  const euOnly = Boolean(project?.eu_only);
  const visibleModels = useMemo(
    () => (euOnly ? models.filter((m) => isEuModel(m.id)) : models),
    [models, euOnly],
  );

  // If the project is EU-only and the currently-selected model isn't compliant,
  // snap the selection to the first allowed model so the form can't submit an
  // illegal choice (the backend also rejects it).
  useEffect(() => {
    if (euOnly && model && !isEuModel(model)) {
      setModel(visibleModels[0]?.id || '');
    }
  }, [euOnly, model, visibleModels]);

  const selectedPhaseData = phaseDataItems.find(
    (p) => project && p.project === project.name,
  );

  const initForm = useCallback(() => {
    const state = storeRef.getState();

    if (isEdit && modModalTaskId !== null) {
      const t = state.tasks.find((x) => x.id === modModalTaskId);
      if (!t) return;
      setTitle(t.title || '');
      setDescription(t.description || '');
      setEditDescWasEmpty((t.description || '').trim() === '');
      setModel(t.model || state.models[0]?.id || '');
      setRoleId(t.role_id ?? '');
      setPhase(t.phase_name || 'Notebook');
      setEstimatedTokens(t.estimated_tokens || 50000);
      setProjectId(t.project_id ?? null);
      setHfRepoId(t.hf_repo_id || '');
      setAwaitingModel(Boolean(t.awaiting_model));
      setRequiresRag(Boolean(t.requires_rag));
      setCorpusId(t.corpus_id || 'railway');
      setContextRefs((t as unknown as { context_refs?: string[] }).context_refs || []);
      // Auto-attach the project's definition file (READMEFIRST.md or legacy
      // CLAUDE.md) so Guide my prompt has project context out of the box.
      const _tp = state.projects.find((p) => p.id === t.project_id);
      const _tdf = (_tp?.def_filename as string | undefined) || (_tp?.definition_file as string | undefined) || 'READMEFIRST.md';
      setAttachments([{ kind: 'definition', ref: _tdf, label: _tdf }]);
    } else {
      setTitle('');
      setDescription('');
      setEditDescWasEmpty(false);
      const defaultModel = state.models.find((m) => m.default)?.id || state.models[0]?.id || 'claude-sonnet-4-6';
      setModel(defaultModel);
      setRoleId('');
      setPhase('');
      setEstimatedTokens(50000);
      const activePid = state.activeProject;
      let pid: number | null = null;
      if (activePid !== null && state.projects.some((p) => p.id === activePid)) {
        pid = activePid;
      } else {
        pid = state.projects[0]?.id ?? null;
      }
      setProjectId(pid);
      setHfRepoId('');
      setAwaitingModel(false);
      setRequiresRag(false);
      const _np = pid !== null ? state.projects.find((p) => p.id === pid) : undefined;
      // Default corpus from the project's RAG setting when the library is on.
      setCorpusId(_np?.rag_corpus_id || 'railway');
      setContextRefs([]);
      const _ndf = (_np?.def_filename as string | undefined) || (_np?.definition_file as string | undefined) || 'READMEFIRST.md';
      setAttachments([{ kind: 'definition', ref: _ndf, label: _ndf }]);
    }

    setImprovedTextLocal('');
    setImproveLoading(false);
    setSuggestResults(null);
    setHfCandidates([]);
    setSuggestTaskType('');
    setShowDepsPicker(false);
    setDepsSearch('');
    setDepsError('');
    setDepsAddInput('');
    setSubmitting(false);
    setAddImprovedApplied(false);
    setPreImproveDescription('');
    setAddTaskNudgeShown(false);
    setAddTaskSubmitting(false);
    setAddTaskPendingDeps([]);
    setEditDeps(null);
    setShowAttachPicker(false);
    setShowContextPicker(false);
    setContextSearch('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showAddModal, modModalTaskId]);

  useEffect(() => {
    if (isOpen) initForm();
  }, [isOpen, initForm]);

  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    api.hf.adopted()
      .then((r) => { if (!cancelled) setAdoptedHf(r.models || []); })
      .catch(() => { if (!cancelled) setAdoptedHf([]); });
    api.config.get()
      .then((cfg) => { if (!cancelled) setRagCorpora(cfg.rag_corpora ?? null); })
      .catch(() => { if (!cancelled) setRagCorpora(null); });
    return () => { cancelled = true; };
  }, [isOpen]);

  // Lane B: load file catalog for context_refs picker when project changes
  useEffect(() => {
    if (!isOpen || projectId === null) return;
    let cancelled = false;
    setCatalogLoading(true);
    api.files.catalog(projectId)
      .then((catalog) => {
        if (cancelled) return;
        const rels = (catalog.files || []).map((f) => f.rel).filter(Boolean) as string[];
        // fallback: also include folders? rel already includes path
        setCatalogFiles(rels);
      })
      .catch(() => { if (!cancelled) setCatalogFiles([]); })
      .finally(() => { if (!cancelled) setCatalogLoading(false); });
    return () => { cancelled = true; };
  }, [isOpen, projectId]);

  const loadEditDeps = useCallback(async () => {
    if (!isEdit || modModalTaskId === null) return;
    setDepsLoading(true);
    try {
      const result = await api.tasks.dependencies.list(modModalTaskId);
      setEditDeps(result);
    } catch {
      setEditDeps(null);
    } finally {
      setDepsLoading(false);
    }
  }, [isEdit, modModalTaskId]);

  useEffect(() => {
    if (isEdit && modModalTaskId !== null) {
      loadEditDeps();
    }
  }, [isEdit, modModalTaskId, loadEditDeps]);

  const refreshData = useCallback(async () => {
    const [proj, tsk, exe, pha] = await Promise.all([
      api.projects.list(),
      api.tasks.list(),
      api.executions.list(100),
      api.phases.list(),
    ]);
    setProjects(proj);
    setTasks(tsk);
    setExecutions(exe);
    setPhasesData(pha);
  }, [setProjects, setTasks, setExecutions, setPhasesData]);

  const handleImprove = async () => {
    if (!title.trim() && !description.trim()) {
      alert('Please enter a title or description first.');
      return;
    }
    setImproveLoading(true);
    try {
      const result = await api.improvePrompt({
        title: title.trim(),
        description: description.trim(),
        project_id: projectId ?? undefined,
        attachments,
        requires_rag: requiresRag ? 1 : 0,
        corpus_id: requiresRag ? corpusId : undefined,
      });
      setImprovedTextLocal(result.improved);
    } catch (e: unknown) {
      alert('Error: ' + (e as Error).message);
    } finally {
      setImproveLoading(false);
    }
  };

  const useImproved = () => {
    if (!preImproveDescription) setPreImproveDescription(description.trim());
    setDescription(improvedTextLocal);
    setAddImprovedApplied(true);
    setImprovedTextLocal('');
  };

  const cancelImprove = () => {
    setImprovedTextLocal('');
  };

  const handleSuggestModel = async () => {
    if (modModalTaskId === null) return;
    setSuggestLoading(true);
    setSuggestResults(null);
    setHfCandidates([]);
    try {
      const result = await api.tasks.recommendModel(modModalTaskId);
      setSuggestTaskType(result.task_type_label || result.task_type || '');
      setSuggestReviewed(result.profiles_reviewed || '');
      setSuggestResults(result.recommendations || []);
      setHfCandidates(result.hf_candidates || []);
    } catch (e: unknown) {
      alert('Error: ' + (e as Error).message);
    } finally {
      setSuggestLoading(false);
    }
  };

  const pickSuggestedModel = (modelId: string) => {
    setModel(modelId);
    setHfRepoId('');
    setAwaitingModel(false);
    setSuggestResults(null);
    setHfCandidates([]);
  };

  const pickHfCandidate = (cand: HfCandidate) => {
    if (cand.servable) {
      setModel(cand.provider_mapping);
      setHfRepoId(cand.repo_id);
      setAwaitingModel(false);
    } else {
      setHfRepoId(cand.repo_id);
      setAwaitingModel(true);
    }
    setSuggestResults(null);
    setHfCandidates([]);
  };

  const pickAdoptedHf = (cand: HfCandidate) => {
    if (cand.servable) {
      setModel(cand.provider_mapping);
      setHfRepoId(cand.repo_id);
      setAwaitingModel(false);
    } else {
      setHfRepoId(cand.repo_id);
      setAwaitingModel(true);
    }
  };

  const closeModal = () => {
    setShowAddModal(false);
    setModModalTaskId(null);
    setSuggestResults(null);
    setHfCandidates([]);
    setHfRepoId('');
    setAwaitingModel(false);
  };

  const handleSubmit = async () => {
    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      alert('Title is required.');
      return;
    }
    if (projectId === null) {
      alert('No project selected.');
      return;
    }

    if (isEdit && modModalTaskId !== null) {
      if (
        editDescWasEmpty
        && description.trim().length > 0
        && !addTaskNudgeShown
        && !addImprovedApplied
      ) {
        setAddTaskNudgeShown(true);
        const improve = window.confirm(
          'This task has a substantial description but you haven\'t used "Guide my prompt".\n\nThe Guide can attach context and rewrite your instructions to be specific and actionable.\n\nClick OK to guide now, Cancel to save as-is.',
        );
        if (improve) {
          handleImprove();
          return;
        }
      }
      setSubmitting(true);
      try {
        await api.tasks.update(modModalTaskId, {
          title: trimmedTitle,
          description: description.trim(),
          model: model || undefined,
          role_id: typeof roleId === 'number' ? roleId : undefined,
          phase_name: phase || 'Notebook',
          estimated_tokens: estimatedTokens || undefined,
          hf_repo_id: hfRepoId || undefined,
          awaiting_model: awaitingModel ? 1 : 0,
          requires_rag: requiresRag ? 1 : 0,
          corpus_id: requiresRag ? corpusId : undefined,
          context_refs: contextRefs,
          ...(preImproveDescription ? { original_description: preImproveDescription } : {}),
        } as unknown as Record<string, unknown>);
        closeModal();
        await refreshData();
      } catch (e: unknown) {
        alert('Error updating task: ' + (e as Error).message);
      } finally {
        setSubmitting(false);
      }
      return;
    }

    if (!addImprovedApplied && description.length > 80 && !addTaskNudgeShown) {
      setAddTaskNudgeShown(true);
      const improve = window.confirm(
        'This task has a substantial description but you haven\'t used "Guide my prompt".\n\nThe Guide can attach context and rewrite your instructions to be specific and actionable.\n\nClick OK to guide now, Cancel to save as-is.',
      );
      if (improve) {
        handleImprove();
        return;
      }
    }

    setAddTaskSubmitting(true);
    try {
      const r = await api.tasks.create({
        project_id: projectId,
        title: trimmedTitle,
        description: description.trim(),
        model: model || undefined,
        phase_name: phase || 'Notebook',
        estimated_tokens: estimatedTokens || undefined,
        role_id: typeof roleId === 'number' ? roleId : undefined,
        hf_repo_id: hfRepoId || undefined,
        awaiting_model: awaitingModel ? 1 : 0,
        requires_rag: requiresRag ? 1 : 0,
        corpus_id: requiresRag ? corpusId : undefined,
        context_refs: contextRefs,
        ...(preImproveDescription ? { original_description: preImproveDescription } : {}),
      });

      const depsToSave = [...addTaskPendingDeps];

      closeModal();
      await refreshData();

      setActiveProject(projectId);
      setActiveStatus('all');

      if (phase) {
        const proj = projects.find((p) => p.id === projectId);
        if (proj) {
          const pd = phaseDataItems.find((p) => p.project === proj.name);
          const idx = pd ? pd.phases.findIndex((ph) => ph.title === phase) : -1;
          if (idx >= 0) {
            setActivePhase({ projectId, projectName: proj.name, phaseIdx: idx, phaseName: phase });
          }
        }
      }

      for (const depId of depsToSave) {
        try {
          await api.tasks.dependencies.add(r.id, { depends_on_id: depId });
        } catch {
          /* ignore */
        }
      }
      if (depsToSave.length) {
        await refreshData();
      }

      setTimeout(() => {
        const el = document.getElementById(`tc-${r.id}`);
        if (el) {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }, 150);
    } catch (e: unknown) {
      alert('Error creating task: ' + (e as Error).message);
    } finally {
      setAddTaskSubmitting(false);
    }
  };

  const handleRemoveEditDep = async (depId: number) => {
    if (modModalTaskId === null) return;
    try {
      await api.tasks.dependencies.remove(modModalTaskId, depId);
      await loadEditDeps();
      const refreshed = await api.tasks.list();
      setTasks(refreshed);
    } catch (e: unknown) {
      alert('Error: ' + (e as Error).message);
    }
  };

  const handleAddEditDep = async (depId: number) => {
    if (modModalTaskId === null) return;
    if (depId === modModalTaskId) {
      setDepsError('A task cannot depend on itself');
      return;
    }
    setDepsError('');
    try {
      await api.tasks.dependencies.add(modModalTaskId, { depends_on_id: depId });
      setDepsAddInput('');
      await loadEditDeps();
      const refreshed = await api.tasks.list();
      setTasks(refreshed);
    } catch (e: unknown) {
      setDepsError((e as Error).message || 'Failed to add dependency');
    }
  };

  const handleEditDepToggle = (id: number) => {
    if (editDepIds.includes(id)) handleRemoveEditDep(id);
    else handleAddEditDep(id);
  };

  const depsProjectId = isEdit ? projectId : (typeof projectId === 'number' ? projectId : null);

  const siblingTasks = useMemo(() => {
    if (depsProjectId === null) return [];
    return tasks.filter(
      (t) =>
        t.project_id === depsProjectId &&
        t.id !== (isEdit ? modModalTaskId : undefined) &&
        (t.status as string) !== 'archived' &&
        (t.status as string) !== 'skip',
    );
  }, [tasks, depsProjectId, isEdit, modModalTaskId]);

  const candidateTasks = useMemo(() => {
    if (!depsSearch) return siblingTasks;
    return siblingTasks.filter(
      (t) =>
        t.title.toLowerCase().includes(depsSearch.toLowerCase()) ||
        String(t.id).includes(depsSearch),
    );
  }, [siblingTasks, depsSearch]);

  const tasksByPhase = useMemo(() => {
    const map: Record<string, Task[]> = {};
    for (const t of candidateTasks) {
      const ph = t.phase_name || '—';
      (map[ph] ??= []).push(t);
    }
    return map;
  }, [candidateTasks]);

  const toggleDep = (id: number) => {
    if (addTaskPendingDeps.includes(id)) {
      setAddTaskPendingDeps(addTaskPendingDeps.filter((x) => x !== id));
    } else {
      setAddTaskPendingDeps([...addTaskPendingDeps, id]);
    }
  };

  if (!isOpen) return null;
  if (isEdit && !task) return null;

  // The endpoint returns full task rows, so the dependency's own id IS the
  // depended-on task id — there is no depends_on_id field on the response.
  const editDepIds = editDeps?.depends_on?.map((d) => d.id) ?? [];
  const isSubmitting = isEdit ? submitting : addTaskSubmitting;

  const renderDepsPicker = ({
    selectedIds,
    onToggle,
  }: {
    selectedIds: number[];
    onToggle: (id: number) => void;
  }) => (
    <div className="border border-border-muted rounded-md p-2.5 max-h-[250px] overflow-y-auto">
      {Object.entries(tasksByPhase).map(([ph, phTasks]) => (
        <div key={ph} className="mb-2">
          <div className="text-xs uppercase text-text-faint font-semibold mb-1">
            {ph}
          </div>
          {phTasks.map((t) => (
            <label
              key={t.id}
              className="flex items-center gap-2 py-1 px-1.5 rounded cursor-pointer"
            >
              <input
                type="checkbox"
                data-tip="Add this task as a dependency"
                checked={selectedIds.includes(t.id)}
                onChange={() => onToggle(t.id)}
              />
              <span className="font-mono text-xs text-text-faint">
                #{t.id}
              </span>
              <span className="flex-1 text-sm overflow-hidden text-ellipsis whitespace-nowrap">
                {t.title}
              </span>
              <span
                className={cn(
                  'text-2xs py-px px-1 rounded-sm',
                  t.status === 'done'
                    ? 'bg-status-done-bg text-status-done'
                    : 'bg-status-pending-bg text-status-pending',
                )}
              >
                {t.status}
              </span>
            </label>
          ))}
        </div>
      ))}
      {candidateTasks.length === 0 && (
        <div className="text-text-faint text-sm italic py-2">
          No tasks found.
        </div>
      )}
    </div>
  );

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 dark:bg-black/75 flex items-center justify-center z-modal" onClick={(e) => { if (e.target === e.currentTarget) closeModal(); }}>
      <div
        className="modal-content bg-surface-raised dark:bg-surface-dark-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong"
        style={{ minWidth: 520, maxHeight: '90vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex justify-between items-center mb-4">
          <h3 className="m-0 text-base text-text-default dark:text-text-dark-default">
            {isEdit ? `Edit Task #${modModalTaskId}` : 'Add Task'}
          </h3>
          <button
            data-tip="Close without saving"
            className="btn py-0.5 px-2 border border-default dark:border-border-dark-default rounded cursor-pointer leading-none text-text-default dark:text-text-dark-default"
            onClick={closeModal}
          >
            ✕
          </button>
        </div>

        {!perms.canEdit && (
          <div
            className="mb-4 rounded-md px-3 py-2 text-sm"
            style={{ background: 'rgba(198,113,57,0.12)', border: '1px solid rgba(198,113,57,0.35)' }}
          >
            You have read-only access to this project — the task cannot be modified.
          </div>
        )}

        {/* Project (read-only) */}
        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft dark:text-text-dark-soft mb-[3px]">Project</label>
          <div className="text-text-faint dark:text-text-dark-faint text-md- py-1.5">
            Project: {project?.name ?? (projectId ? `#${projectId}` : '—')}
          </div>
        </div>

        {/* Title */}
        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft dark:text-text-dark-soft mb-[3px]">Title</label>
          <input
            data-tip="Enter a short title for this task"
            className="w-full py-[7px] px-2.5 border border-default dark:border-border-dark-default rounded text-base bg-surface-base dark:bg-surface-dark-base text-text-default dark:text-text-dark-default"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Task title"
            autoFocus
          />
        </div>

        {/* Description */}
        <div className="form-group mb-3">
          <div className="flex justify-between items-center mb-1.5">
            <label className="block text-sm font-semibold text-text-soft mb-[3px] m-0">Prompt / Description</label>
            <div className="flex gap-1.5">
              {perms.canEdit && (
                <>
                  <button
                    data-tip="Attach files, memories, tasks or working docs to use as context when improving"
                    className="btn text-sm py-1 px-2 border border-dashed border-default rounded cursor-pointer text-text-soft"
                    onClick={() => setShowAttachPicker(true)}
                  >
                    + Attach
                  </button>
                  <button
                    data-tip="Guide my prompt"
                    className="header-btn header-btn-primary px-3.5 py-1 border rounded text-white text-md- font-heading font-normal cursor-pointer"
                    onClick={handleImprove}
                    disabled={improveLoading}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" style={{ opacity: 1 }}><circle cx="12" cy="12" r="12" fill="#fbf6ee"/><circle cx="12" cy="12" r="7.5" fill="none" stroke="#c67139" strokeWidth="3"/><circle cx="12" cy="12" r="2.4" fill="#c67139"/></svg>
                    {improveLoading ? 'Guiding…' : 'Guide my prompt'}
                  </button>
                </>
              )}
            </div>
          </div>
          <textarea
            data-tip="Describe what this task should do"
            className="w-full py-[7px] px-2.5 border border-default rounded text-base"
            value={description}
            onChange={(e) => setDescription(e.target.value.slice(0, DESC_LIMIT))}
            rows={8}
            style={{ minHeight: 150 }}
            maxLength={DESC_LIMIT}
            placeholder="Task description"
          />
          <div
            className="mt-1 h-1.5 rounded-sm bg-[#eee7db] dark:bg-[#332f29] overflow-hidden"
          >
            <div
              className="h-full rounded-sm"
              style={{
                width: `${(description.length / DESC_LIMIT) * 100}%`,
                background:
                  remaining / DESC_LIMIT > 0.2
                    ? '#7a8a5e'
                    : remaining / DESC_LIMIT > 0.1
                      ? '#b2622d'
                      : '#a3402f',
                transition: 'width 0.15s ease, background 0.15s ease',
              }}
            />
          </div>
          <div
            className="text-sm+ text-right mt-0.5"
            style={{
              color:
                remaining <= DESC_LIMIT * 0.1
                  ? '#a3402f'
                  : remaining <= DESC_LIMIT * 0.2
                    ? '#b2622d'
                    : '#82796a',
            }}
          >
            {remaining} characters remaining
          </div>
          {attachments.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1 items-center">
              <span className="text-xs text-text-soft font-semibold">Attached:</span>
              {attachments.map((a, i) => (
                <span
                  key={`${a.kind}::${a.ref}`}
                  className="inline-flex items-center gap-1 rounded-[4px] px-1.5 py-px text-xs"
                  style={{ background: 'rgba(100,92,80,.12)', color: '#645c50' }}
                >
                  {a.label}
                  {perms.canEdit && (
                    <span
                      className="cursor-pointer text-danger text-[10px] leading-none"
                      onClick={() => setAttachments(attachments.filter((_, j) => j !== i))}
                      title="Remove"
                    >
                      ✕
                    </span>
                  )}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Improved text preview */}
        {improvedTextLocal && (
          <div
            className="mb-3 rounded-md p-3 border"
            style={{ background: '#fbf6ee', borderColor: '#e6dccb' }}
          >
            <div className="text-sm mb-1.5 font-semibold" style={{ color: '#3f628f' }}>
              AI-improved description:
            </div>
            <pre className="whitespace-pre-wrap text-sm m-0 mb-2 max-h-[200px] overflow-y-auto">
              {improvedTextLocal}
            </pre>
            <div className="flex gap-2">
              {perms.canEdit && (
                <>
                  <button data-tip="Replace the description with the AI-improved version" className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white" onClick={useImproved}>
                    Use this
                  </button>
                  <button data-tip="Discard the AI-improved suggestion" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={cancelImprove}>
                    Cancel
                  </button>
                </>
              )}
            </div>
          </div>
        )}

        {/* Model & Role */}
        <div className="flex gap-3">
          <div className="form-group mb-3 flex-1">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">
              Model
              {euOnly && (
                <span className="ml-1.5 text-2xs font-normal text-text-faint" title="EU-only project: only Mistral & Scaleway models are available">
                  🇪🇺 EU-only
                </span>
              )}
            </label>
            <select data-tip="Choose the model to run this task" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={model} onChange={(e) => setModel(e.target.value)}>
              {(() => {
                const dedicated = visibleModels.filter((m) => m.id.startsWith('scw-dep-'));
                const serverless = visibleModels.filter((m) => m.id.startsWith('scw-') && !m.id.startsWith('scw-dep-'));
                const rest = visibleModels.filter((m) => !m.id.startsWith('scw-'));
                const out: ReactElement[] = [];
                if (dedicated.length) {
                  out.push(<optgroup key="ded" label="Dedicated (this project)">
                    {dedicated.map((m) => <option key={m.id} value={m.id} disabled={isFreeUser && m.free_allowed === false}>{m.label} (GPU){isFreeUser && m.free_allowed === false ? ' (Upgrade)' : ''}</option>)}
                  </optgroup>);
                }
                if (serverless.length) {
                  out.push(<optgroup key="sl" label="Serverless EU">
                    {serverless.map((m) => <option key={m.id} value={m.id} disabled={isFreeUser && m.free_allowed === false}>{m.label}{isFreeUser && m.free_allowed === false ? ' (Upgrade)' : ''}</option>)}
                  </optgroup>);
                }
                if (rest.length) {
                  out.push(<optgroup key="rest" label="Models">
                    {rest.map((m) => <option key={m.id} value={m.id} disabled={isFreeUser && m.free_allowed === false}>{m.label}{isFreeUser && m.free_allowed === false ? ' (Upgrade)' : ''}</option>)}
                  </optgroup>);
                }
                return out;
              })()}
            </select>
            {hfRepoId && (
              <div
                className="mt-1.5 px-2 py-1.5 rounded text-xs flex items-center gap-2"
                style={{
                  background: 'rgba(198,113,57,0.12)',
                  border: '1px solid rgba(198,113,57,0.35)',
                }}
              >
                <span className="text-status-pending font-semibold flex-1 truncate" title={hfRepoId}>
                  HF model: {hfRepoId}
                  {awaitingModel && (
                    adoptedHf.find((c) => c.repo_id === hfRepoId)?.import_ready
                      ? ' · imported — deploy via GPU window'
                      : ' · awaiting self-host'
                  )}
                </span>
                {perms.canEdit && (
                  <button
                    data-tip="Revert to the catalogue model"
                    className="btn py-0.5 px-2 border border-default rounded cursor-pointer text-xs"
                    onClick={() => { setHfRepoId(''); setAwaitingModel(false); }}
                  >
                    Clear
                  </button>
                )}
              </div>
            )}
          </div>
          <div className="form-group mb-3 flex-1">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">Role</label>
            <select
              data-tip="Assign a role to this task"
              className="w-full py-[7px] px-2.5 border border-default rounded text-base"
              value={roleId}
              onChange={(e) => setRoleId(e.target.value ? Number(e.target.value) : '')}
            >
              <option value="">— None —</option>
              {roles.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Hugging Face models (cross-project) — click to assign, no re-adoption needed */}
        {adoptedHf.length > 0 && (
          <div className="form-group mb-3">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">
              Hugging Face models
              <span className="ml-1.5 text-2xs font-normal text-text-faint">
                (already imported — click to use)
              </span>
            </label>
            <div className="flex flex-wrap gap-1.5">
              {adoptedHf.map((cand) => (
                <button
                  key={cand.repo_id}
                  type="button"
                  data-tip={
                    cand.servable ? `Serve as ${cand.provider_mapping}`
                      : cand.import_ready ? 'Imported — deploy on a GPU window to run'
                      : cand.import_status ? `Import ${cand.import_status}`
                      : 'Awaiting self-host'
                  }
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-[10px] px-2 py-1 text-xs border cursor-pointer',
                    hfRepoId === cand.repo_id
                      ? 'border-accent bg-accent/10 text-accent'
                      : 'border-default',
                  )}
                  onClick={() => pickAdoptedHf(cand)}
                >
                  <span className="max-w-[200px] truncate">{cand.label}</span>
                  {cand.servable ? (
                    <span className="text-2xs px-1 rounded bg-status-running/20 text-status-running">servable</span>
                  ) : cand.import_ready ? (
                    <span className="text-2xs px-1 rounded bg-status-running/20 text-status-running">imported</span>
                  ) : cand.import_status ? (
                    <span className="text-2xs px-1 rounded bg-surface text-text-muted">{cand.import_status}</span>
                  ) : (
                    <span className="text-2xs px-1 rounded bg-status-pending/20 text-status-pending">self-host</span>
                  )}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Phase & Estimated Tokens */}
        <div className="flex gap-3">
          <div className="form-group mb-3 flex-1">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">Phase</label>
            <select data-tip="Choose a phase for this task" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={phase} onChange={(e) => setPhase(e.target.value)}>
              {!selectedPhaseData?.phases.some(ph => ph.title === 'Notebook') && (
                <option value="Notebook">— Notebook —</option>
              )}
              {selectedPhaseData?.phases.map((ph) => (
                <option key={ph.title} value={ph.title}>
                  {ph.title}
                </option>
              ))}
            </select>
          </div>
          <div className="form-group mb-3 flex-1">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">Estimated Tokens</label>
            <input
              type="number"
              data-tip="Estimated token budget for this task"
              className="w-full py-[7px] px-2.5 border border-default rounded text-base"
              value={estimatedTokens}
              onChange={(e) => setEstimatedTokens(Number(e.target.value) || 0)}
              min={0}
            />
          </div>
        </div>

        {/* RAG library — only when the project has RAG enabled */}
        {Boolean(project?.use_rag) && (
          <div className="form-group mb-3 rounded-lg border border-default p-3 bg-bg-inset/40">
            <div className="flex items-center gap-2">
              <input
                id="rag-requires"
                type="checkbox"
                data-tip="Ground the answer in the shared RAG library"
                checked={requiresRag}
                onChange={(e) => setRequiresRag(e.target.checked)}
                className="h-4 w-4"
              />
              <label htmlFor="rag-requires" className="text-sm font-semibold text-text-soft cursor-pointer">
                Requires RAG library
                <span className="ml-1.5 text-2xs font-normal text-text-faint">
                  (ground the answer in the shared legal library, not model memory)
                </span>
              </label>
            </div>
            {requiresRag && (
              <div className="mt-2">
                <label className="block text-sm font-semibold text-text-soft mb-[3px]">Corpus</label>
                <select
                  data-tip="Choose the RAG corpus to search"
                  className="w-full py-[7px] px-2.5 border border-default rounded text-base"
                  value={corpusId}
                  onChange={(e) => setCorpusId(e.target.value)}
                >
                  {(ragCorpora ?? [{ id: 'railway', label: 'railway — EU→PL railway law' }]).map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </select>
                <p className="text-2xs text-text-faint mt-1">
                  RAG is a shared library (factory /opt/RAG), not per-project.
                </p>
              </div>
            )}
          </div>
        )}

        {/* Lane B: context_refs picker */}
        <div className="form-group mb-3 rounded-lg border border-default p-3 bg-bg-inset/30">
          <div className="flex items-center justify-between">
            <label className="block text-sm font-semibold text-text-soft">Context files (explicit)</label>
            {perms.canEdit && (
              <button
                data-tip={showContextPicker ? 'Close the context file picker' : 'Pick files to inject into the prompt'}
                className="btn py-1 px-2 border border-default rounded cursor-pointer text-xs"
                onClick={() => setShowContextPicker(!showContextPicker)}
              >
                {showContextPicker ? 'Close picker' : 'Pick files'}
              </button>
            )}
          </div>
          <p className="text-2xs text-text-faint mt-1">
            Select Working Docs / outputs files to inject into the prompt. Empty = capped index fallback (≤5k tokens) with review flag.
          </p>
          {contextRefs.length > 0 ? (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {contextRefs.map((rel) => (
                <span
                  key={rel}
                  className="inline-flex items-center gap-1 rounded-[4px] px-1.5 py-px text-xs"
                  style={{ background: 'rgba(122,90,166,.12)', color: '#7a5aa6', border: '1px solid rgba(122,90,166,.3)' }}
                >
                  {rel}
                  {perms.canEdit && (
                    <span
                      className="cursor-pointer text-danger text-[10px] leading-none ml-1"
                      onClick={() => setContextRefs(contextRefs.filter((r) => r !== rel))}
                      title="Remove"
                    >
                      ✕
                    </span>
                  )}
                </span>
              ))}
            </div>
          ) : (
            <div className="text-xs text-text-faint italic mt-2">No explicit context — fallback index will be used.</div>
          )}
          {showContextPicker && (
            <div className="mt-3 border border-border-muted rounded-md p-2 max-h-[220px] overflow-y-auto">
              <input
                data-tip="Search the file catalog"
                className="w-full py-1 px-2 border border-default rounded text-sm mb-2"
                placeholder="Search files..."
                value={contextSearch}
                onChange={(e) => setContextSearch(e.target.value)}
              />
              {catalogLoading ? (
                <div className="text-xs text-text-faint">Loading catalog…</div>
              ) : (
                <>
                  {catalogFiles
                    .filter((rel) => !contextSearch || rel.toLowerCase().includes(contextSearch.toLowerCase()))
                    .slice(0, 100)
                    .map((rel) => {
                      const selected = contextRefs.includes(rel);
                      return (
                        <label key={rel} className="flex items-center gap-2 py-1 px-1.5 rounded cursor-pointer hover:bg-surface-subtle">
                          <input
                            type="checkbox"
                            data-tip="Include this file in the prompt context"
                            checked={selected}
                            onChange={() => {
                              if (selected) setContextRefs(contextRefs.filter((r) => r !== rel));
                              else setContextRefs([...contextRefs, rel]);
                            }}
                          />
                          <span className="text-xs flex-1 truncate" title={rel}>{rel}</span>
                        </label>
                      );
                    })}
                  {catalogFiles.filter((rel) => !contextSearch || rel.toLowerCase().includes(contextSearch.toLowerCase())).length === 0 && (
                    <div className="text-xs text-text-faint italic py-2">No files found.</div>
                  )}
                </>
              )}
            </div>
          )}
        </div>

        {/* Suggest model (edit only) */}
        {isEdit && perms.canEdit && (
          <div className="mb-3">
            <button
              data-tip="Ask the counselor to recommend a model"
              className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-sm+"
              onClick={handleSuggestModel}
              disabled={suggestLoading}
            >
              {suggestLoading ? '🤖 Thinking…' : '🤖 Suggest Model'}
            </button>
          </div>
        )}

        {/* Suggest results */}
        {suggestResults !== null && (
          <div
            className="mb-3 rounded-md p-2.5"
            style={{ border: '1px solid #7a5aa6', background: '#f1ecf6' }}
          >
            <div className="flex justify-between items-center mb-2">
              <span className="text-sm+ text-text-soft">
                Detected task type:{' '}
                <strong className="text-text-DEFAULT">
                  {(suggestTaskType || 'general').replace(/_/g, ' ')}
                </strong>
              </span>
              <button
                data-tip="Dismiss these suggestions"
                className="btn py-px px-1.5 text-xs border border-default rounded cursor-pointer"
                onClick={() => setSuggestResults(null)}
              >
                ✕
              </button>
            </div>
            {suggestResults.map((rec) => {
              const cat = rec.category ? CATEGORY_META[rec.category] : undefined;
              return (
                <div
                  key={rec.model}
                  className="flex items-center justify-between py-[5px] px-2 mb-1 bg-surface-raised rounded-md cursor-pointer border"
                  style={{ borderColor: '#e6dccb' }}
                  onClick={() => pickSuggestedModel(rec.model)}
                  onMouseOver={(e) => {
                    (e.currentTarget as HTMLDivElement).style.borderColor = '#7a5aa6';
                  }}
                  onMouseOut={(e) => {
                    (e.currentTarget as HTMLDivElement).style.borderColor = '#e6dccb';
                  }}
                >
                  <div className="min-w-0">
                    <span className="font-semibold">{rec.label}</span>
                    {cat && (
                      <span
                        className="text-2xs py-px px-1 rounded ml-1"
                        style={{ background: cat.bg, color: cat.fg }}
                      >
                        {cat.label}
                      </span>
                    )}
                    {rec.tier && (
                      <span
                        className="text-2xs py-px px-1 rounded ml-1 uppercase tracking-wide"
                        style={{ background: TIER_COLORS[rec.tier] || '#645c50', color: '#fff' }}
                      >
                        {rec.tier}
                      </span>
                    )}
                    {rec.eu && (
                      <span className="text-2xs ml-1" title="EU-compliant (Mistral / Scaleway)">🇪🇺</span>
                    )}
                    <br />
                    <span className="text-text-faint text-xs">{rec.reason}</span>
                    {rec.justification && (
                      <div className="text-xs mt-0.5" style={{ color: '#7a5aa6' }}>
                        {rec.justification}
                      </div>
                    )}
                    {rec.strengths && rec.strengths.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {rec.strengths.map((s) => (
                          <span
                            key={s}
                            className="text-2xs py-px px-1 rounded"
                            style={{ background: '#f1ecf6', color: '#7a5aa6' }}
                          >
                            {s.replace(/_/g, ' ')}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <span className="text-text-faint text-sm+ whitespace-nowrap ml-2">
                    {rec.estimated_cost > 0 ? `~$${rec.estimated_cost.toFixed(4)}` : 'free'}
                  </span>
                </div>
              );
            })}
            {hfCandidates.length > 0 && (
              <div className="mt-2 pt-2" style={{ borderTop: '1px dashed #7a5aa6' }}>
                <div className="text-xs uppercase tracking-wide text-text-faint mb-1">
                  Specialised models (Hugging Face)
                </div>
                {hfCandidates.map((cand) => (
                  <div
                    key={cand.repo_id}
                    className="flex items-start justify-between py-[5px] px-2 mb-1 bg-surface-raised rounded-md cursor-pointer border"
                    style={{ borderColor: '#e6dccb' }}
                    onClick={() => pickHfCandidate(cand)}
                    onMouseOver={(e) => {
                      (e.currentTarget as HTMLDivElement).style.borderColor = '#7a5aa6';
                    }}
                    onMouseOut={(e) => {
                      (e.currentTarget as HTMLDivElement).style.borderColor = '#e6dccb';
                    }}
                  >
                    <div className="min-w-0">
                      <a
                        href={cand.hf_url}
                        target="_blank"
                        rel="noreferrer"
                        className="font-semibold text-accent underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {cand.label} ↗
                      </a>
                      <span
                        className="text-2xs px-1 rounded ml-1"
                        style={{ background: cand.validation.score >= 0.6 ? '#e1eecc' : '#fff2eb', color: '#000' }}
                      >
                        {Math.round(cand.validation.score * 100)}
                      </span>
                      {cand.servable ? (
                        <span className="text-2xs px-1 rounded ml-1" style={{ background: '#e1eecc', color: '#000' }}>
                          servable
                        </span>
                      ) : (
                        <span className="text-2xs px-1 rounded ml-1" style={{ background: '#fff2eb', color: '#000' }}>
                          assign (awaiting self-host)
                        </span>
                      )}
                      {cand.limitations.length > 0 && (
                        <div className="text-xs text-text-faint">{cand.limitations.join(' · ')}</div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {suggestReviewed && (
              <div className="text-2xs text-text-faint mt-1.5 text-right">
                Guidance last reviewed {suggestReviewed}
              </div>
            )}
          </div>
        )}

        {/* Dependencies section */}
        <div className="form-group mb-3">
          <label className="block text-sm font-semibold text-text-soft mb-[3px]">Dependencies</label>

          {/* Edit mode deps */}
          {isEdit && (
            <>
              {depsLoading ? (
                <div className="text-text-faint text-sm py-2">Loading...</div>
              ) : editDeps ? (
                <>
                  <div className="mb-2">
                    <div className="text-sm+ text-text-soft mb-1 font-semibold">
                      Depends on
                    </div>
                    {editDeps.depends_on.length === 0 ? (
                      <div className="text-text-faint text-sm+ italic">
                        No dependencies.
                      </div>
                    ) : (
                      editDeps.depends_on.map((dep) => {
                        const depId = dep.id;
                        const depTask = tasks.find((t) => t.id === depId) ?? dep;
                        return (
                          <div
                            key={depId}
                            className="flex items-center justify-between py-1 px-2 bg-surface-subtle rounded mb-1"
                          >
                            <div className="flex items-center gap-2">
                              <span className="font-mono text-xs text-text-faint">
                                #{depId}
                              </span>
                              <span className="text-sm+">
                                {depTask?.title ?? `Task #${depId}`}
                              </span>
                              {depTask && (
                                <span
                                  className={cn(
                                    'text-2xs py-px px-1 rounded-sm',
                                    depTask.status === 'done'
                                      ? 'bg-status-done-bg text-status-done'
                                      : 'bg-status-pending-bg text-status-pending',
                                  )}
                                >
                                  {depTask.status}
                                </span>
                              )}
                            </div>
                            {perms.canEdit && (
                              <button
                                data-tip="Remove this dependency"
                                className="btn py-px px-1.5 text-xs border border-default rounded cursor-pointer"
                                onClick={() => handleRemoveEditDep(depId)}
                              >
                                ✕
                              </button>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>

                  <div className="mb-2">
                    <div className="text-sm+ text-text-soft mb-1 font-semibold">
                      Depended on by
                    </div>
                    {editDeps.depended_on_by.length === 0 ? (
                      <div className="text-text-faint text-sm+ italic">
                        No tasks depend on this one.
                      </div>
                    ) : (
                      editDeps.depended_on_by.map((dep) => {
                        const depTaskId = dep.id;
                        const depTask = tasks.find((t) => t.id === depTaskId) ?? dep;
                        return (
                          <div
                            key={depTaskId}
                            className="flex items-center py-1 px-2 bg-surface-subtle rounded mb-1"
                          >
                            <span className="font-mono text-sm+ text-text-faint">
                              #{depTaskId}
                            </span>
                            <span className="text-sm+ ml-2">
                              {depTask?.title ?? `Task #${depTaskId}`}
                            </span>
                            {depTask && (
                              <span
                                className={cn(
                                  'text-2xs py-px px-1 rounded-sm ml-2',
                                  depTask.status === 'done'
                                    ? 'bg-status-done-bg text-status-done'
                                    : 'bg-status-pending-bg text-status-pending',
                                )}
                              >
                                {depTask.status}
                              </span>
                            )}
                          </div>
                        );
                      })
                    )}
                  </div>

                  {perms.canEdit && (
                    <div className="flex gap-2 items-center mb-2">
                      <button data-tip={showDepsPicker ? 'Close the dependency picker' : 'Pick tasks this task depends on'} className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={() => setShowDepsPicker(!showDepsPicker)}>
                        {showDepsPicker ? 'Close Picker' : 'Pick Dependencies'}
                      </button>
                      {editDepIds.length > 0 && (
                        <span className="text-sm+ text-text-soft">
                          {editDepIds.length} selected
                        </span>
                      )}
                      {showDepsPicker && (
                        <input
                          data-tip="Search tasks to add as dependencies"
                          className="flex-1 py-1 px-2 border border-default rounded text-sm"
                          placeholder="Search tasks..."
                          value={depsSearch}
                          onChange={(e) => setDepsSearch(e.target.value)}
                        />
                      )}
                    </div>
                  )}

                  {perms.canEdit && showDepsPicker && renderDepsPicker({ selectedIds: editDepIds, onToggle: handleEditDepToggle })}

                  {perms.canEdit && (
                    <details className="mb-2">
                      <summary className="text-sm+ text-text-faint cursor-pointer select-none">
                        Add by ID
                      </summary>
                      <div className="flex gap-2 items-start mt-1.5">
                        <div className="flex-1">
                          <input
                            type="number"
                            data-tip="Enter the ID of the task to depend on"
                            className={cn(
                              'w-full py-1 px-2 rounded text-sm',
                              depsError ? 'border border-danger' : 'border border-default',
                            )}
                            value={depsAddInput}
                            onChange={(e) => {
                              setDepsAddInput(e.target.value);
                              setDepsError('');
                            }}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') {
                                const id = Number(depsAddInput.trim());
                                if (id && Number.isInteger(id)) handleAddEditDep(id);
                              }
                            }}
                            placeholder="Task ID"
                          />
                          {depsError && (
                            <div className="text-sm+ text-danger mt-1">
                              {depsError}
                            </div>
                          )}
                        </div>
                        <button
                          data-tip="Add the entered task as a dependency"
                          className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-sm+ bg-accent text-white shrink-0"
                          onClick={() => {
                            const id = Number(depsAddInput.trim());
                            if (id && Number.isInteger(id)) handleAddEditDep(id);
                          }}
                        >
                          Add
                        </button>
                      </div>
                    </details>
                  )}
                </>
              ) : null}
            </>
          )}

          {/* Create mode deps */}
          {!isEdit && perms.canEdit && (
            <>
              <div className="flex gap-2 items-center mb-2">
                <button data-tip={showDepsPicker ? 'Close the dependency picker' : 'Pick tasks this task depends on'} className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={() => setShowDepsPicker(!showDepsPicker)}>
                  {showDepsPicker ? 'Close Picker' : 'Pick Dependencies'}
                </button>
                {addTaskPendingDeps.length > 0 && (
                  <span className="text-sm+ text-text-soft">
                    {addTaskPendingDeps.length} selected
                  </span>
                )}
                {showDepsPicker && (
                  <input
                    data-tip="Search tasks to add as dependencies"
                    className="flex-1 py-1 px-2 border border-default rounded text-sm"
                    placeholder="Search tasks..."
                    value={depsSearch}
                    onChange={(e) => setDepsSearch(e.target.value)}
                  />
                )}
              </div>

              {addTaskPendingDeps.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-2">
                  {addTaskPendingDeps.map((id) => {
                    const t = tasks.find((x) => x.id === id);
                    return (
                      <span
                        key={id}
                        className="inline-flex items-center gap-1 rounded-[10px] py-0.5 px-2 text-sm+"
                        style={{
                          background: 'rgba(63,98,143,.15)',
                          border: '1px solid rgba(63,98,143,.3)',
                        }}
                      >
                        <span className="font-mono text-2xs text-text-faint">
                          #{id}
                        </span>
                        <span className="max-w-[140px] overflow-hidden text-ellipsis whitespace-nowrap" title={t?.title ?? `Task #${id}`}>
                          {t?.title ?? `Task #${id}`}
                        </span>
                        <button
                          data-tip="Remove this pending dependency"
                          className="bg-none border-none text-text-faint cursor-pointer p-0 text-sm+ leading-none"
                          onClick={() =>
                            setAddTaskPendingDeps(addTaskPendingDeps.filter((x) => x !== id))
                          }
                        >
                          ✕
                        </button>
                      </span>
                    );
                  })}
                </div>
              )}

              {showDepsPicker && renderDepsPicker({ selectedIds: addTaskPendingDeps, onToggle: toggleDep })}
            </>
          )}
        </div>

        {/* Actions */}
        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button data-tip="Close without saving" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={closeModal}>
            Cancel
          </button>
          {perms.canEdit && (
            <button
              data-tip={isEdit ? 'Save changes to this task' : 'Create this task'}
              className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white"
              onClick={handleSubmit}
              disabled={isSubmitting}
            >
              {isSubmitting
                ? (isEdit ? 'Saving…' : 'Creating…')
                : (isEdit ? 'Save' : 'Create Task')}
            </button>
          )}
        </div>
      </div>

      <TaskAttachPicker
        open={showAttachPicker}
        projectId={projectId}
        picked={attachments}
        onChange={setAttachments}
        onClose={() => setShowAttachPicker(false)}
      />
    </div>
  );
};

export default AddTaskModal;
