import type {
  Project, Model, Task, Execution, Chat, Phase, DependencyEdge, TaskDependencies,
  Role, WorkSession, CostSummary, DashboardData, Config, ProviderReadiness,
  ModelRecommendation, ChatMessageResponse, EstimateResult, BudgetInfo,
  MemoryView, ContextOptions, AttachableTask, TaskAttachment, GateAction, MemoryLevel, GitMode,
  ScwSessionStatus, Deployment, FilesCatalog, ProjectHfModel, HfSearchResult, HfCandidate,
  HfQueueGroup, GpuWindow, GpuWindowModel, GpuRunStatus,
  MeResponse, ProjectMember, MemberRole, AdminUser, DirectoryUser,
  ArchiveResult, ImportResult,
} from './types';

const BASE = '';

/**
 * API error carrying the server's structured response. `message` holds the
 * server's `error` string (for backward-compat callers); `status` and `body`
 * expose the raw HTTP status and parsed JSON so callers can surface structured
 * payloads (e.g. the `running`/`confirmed` task lists on a 409 delete block).
 */
export class ApiError extends Error {
  status: number;
  body: Record<string, unknown> | null;
  constructor(message: string, status: number, body: Record<string, unknown> | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/**
 * Turn a failed response into an Error carrying the server's own explanation.
 *
 * Every API error route returns `{"error": "..."}` with the actual reason —
 * "pitch required", the EU data-residency refusal, a budget ceiling. Throwing
 * `${status} ${statusText}` discarded all of it and showed the user
 * "400 Bad Request", which is indistinguishable from a bug in the app.
 */
async function fail(res: Response): Promise<never> {
  let detail = '';
  let body: Record<string, unknown> | null = null;
  try {
    body = await res.json();
    detail = typeof body?.error === 'string' ? body.error : '';
  } catch {
    // Non-JSON body (proxy error page, empty 500) — fall back to the status.
  }
  throw new ApiError(detail || `${res.status} ${res.statusText}`, res.status, body);
}

async function get<T>(url: string): Promise<T> {
  const res = await fetch(BASE + url);
  if (!res.ok) return fail(res);
  return res.json();
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE + url, {
    method: 'POST',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) return fail(res);
  return res.json();
}

async function patch<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) return fail(res);
  return res.json();
}

async function put<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(BASE + url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) return fail(res);
  return res.json();
}

async function del<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE + url, {
    method: 'DELETE',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) return fail(res);
  return res.json();
}

export const api = {
  // ── Auth ───────────────────────────────────────────────────────────
  auth: {
    me: () => get<MeResponse>('/api/me'),
    login: () => { window.location.href = '/api/auth/login'; },
    logout: () => post<{ ok: boolean; end_session_url?: string | null }>('/api/auth/logout'),
    listUsers: () => get<{ users: AdminUser[] }>('/api/admin/users'),
    directory: () => get<{ users: DirectoryUser[] }>('/api/users'),
    updateUser: (userId: number, data: Partial<Pick<AdminUser, 'plan' | 'status' | 'role'>>) =>
      put<{ user: AdminUser }>(`/api/admin/users/${userId}`, data),
    updatePlan: (userId: number, plan: string) => put<{ user: AdminUser }>(`/api/admin/users/${userId}`, { plan }),
    deleteUser: (userId: number) => del<{ ok: boolean; orphaned_projects: number }>(`/api/admin/users/${userId}`),
  },
  // ── Project members ────────────────────────────────────────────────
  members: {
    list: (pid: number) => get<{ members: ProjectMember[] }>(`/api/projects/${pid}/members`),
    add: (pid: number, userId: number, role: MemberRole) =>
      post<{ ok: boolean; members: ProjectMember[] }>(`/api/projects/${pid}/members`, { user_id: userId, role }),
    update: (pid: number, userId: number, role: MemberRole) =>
      put<{ ok: boolean; members: ProjectMember[] }>(`/api/projects/${pid}/members/${userId}`, { role }),
    remove: (pid: number, userId: number) =>
      del<{ ok: boolean; members: ProjectMember[] }>(`/api/projects/${pid}/members/${userId}`),
  },
  // ── Config ──────────────────────────────────────────────────────────
  config: {
    get: () => get<Config>('/api/config'),
    update: (data: Partial<Config>) => post<Config>('/api/config', data),
    readiness: () => get<ProviderReadiness>('/api/config/readiness'),
  },

  // ── Models ──────────────────────────────────────────────────────────
  models: {
    list: () => get<Model[]>('/api/models'),
  },

  // ── Hugging Face Scout ───────────────────────────────────────────────
  hf: {
    search: (params: { q?: string; pipeline_tag?: string; language?: string; limit?: number }) => {
      const qs = new URLSearchParams();
      if (params.q) qs.set('q', params.q);
      if (params.pipeline_tag) qs.set('pipeline_tag', params.pipeline_tag);
      if (params.language) qs.set('language', params.language);
      if (params.limit) qs.set('limit', String(params.limit));
      const suffix = qs.toString() ? '?' + qs.toString() : '';
      return get<{ results: HfSearchResult[] }>('/api/hf/search' + suffix);
    },
    register: (data: { project_id: number; repo_id: string; set_default?: boolean }) =>
      post<{ ok: boolean; repo_id: string; model_id: string; is_default: boolean }>('/api/hf/register', data),
    adopted: () => get<{ models: HfCandidate[] }>('/api/hf/adopted'),
  },

  // ── Projects ────────────────────────────────────────────────────────
  projects: {
    list: () => get<Project[]>('/api/projects'),
    create: (data: {
      name: string; pitch: string; stack_hints?: string;
      scaffolding_model?: string; project_type?: string;
      eu_only?: boolean; llm_mode?: string; execution_type?: string;
      aingel_name?: string; aingel_model?: string;
    }) => post<{ project: Project; chat: Chat; reply: string }>('/api/projects', data),
    update: (pid: number, data: Record<string, unknown>) => patch<{ ok: true }>(`/api/projects/${pid}`, data),
    delete: (pid: number) => del<{ ok: true; hard_deleted?: boolean; archived_until?: string | null }>(`/api/projects/${pid}`),
    archive: (pid: number) => post<ArchiveResult>(`/api/projects/${pid}/archive`),
    importProject: async (
      file: File,
      onProgress?: (msg: string) => void,
    ): Promise<ImportResult> => {
      const form = new FormData();
      form.append('file', file, file.name);
      onProgress?.(`Uploading ${file.name}…`);
      const res = await fetch(BASE + '/api/projects/import', { method: 'POST', body: form });
      if (!res.ok) return fail(res);
      onProgress?.(`Importing ${file.name}…`);
      return res.json();
    },
    // Fetch the archive as a blob so completion is reliable, then trigger a
    // browser download. Resolves once the blob has been handed to the browser
    // — NOT once the file is on disk — so callers must treat this as the
    // download *handoff*, never as proof the zip was saved.
    downloadArchive: async (pid: number, filename: string): Promise<void> => {
      const res = await fetch(BASE + `/api/projects/${pid}/archive/${encodeURIComponent(filename)}`);
      if (!res.ok) return fail(res);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      // Keep the object URL alive long enough for a large download to finish;
      // revoking too early aborts an in-flight save. Also revoke on unload so
      // a cancelled/navigated-away download does not pin the blob.
      const revoke = () => URL.revokeObjectURL(url);
      window.addEventListener('beforeunload', revoke, { once: true });
      try {
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        window.setTimeout(revoke, 60000);
      }
    },

    budget: {
      get: (pid: number) => get<BudgetInfo>(`/api/projects/${pid}/budget`),
      set: (pid: number, data: { monthly_budget?: number; budget_reset_day?: number }) =>
        put<BudgetInfo>(`/api/projects/${pid}/budget`, data),
    },

    dependencies: (pid: number) => get<DependencyEdge[]>(`/api/projects/${pid}/dependencies`),
    git: (pid: number, mode: GitMode) => post<{ ok: boolean; mode: string; enabled: boolean; is_repo: boolean; init?: boolean }>(
      `/api/projects/${pid}/git`, { mode }),
    applyGuide: (pid: number, chatId: number) =>
      post<{ tasks_imported: number }>(`/api/projects/${pid}/apply-guide`, { chat_id: chatId }),
    runAllClaude: (pid: number) => post<{
      ok?: boolean; slot: number; queued: number;
      skipped_by_deps?: { task_id: number; title: string; unmet: { id: number; title: string; status: string }[] }[];
      message?: string;
    }>(`/api/projects/${pid}/run-all-claude`),
    runAllMistral: (pid: number) => post<{
      ok?: boolean; slot: number; queued: number;
      skipped_by_deps?: { task_id: number; title: string; unmet: { id: number; title: string; status: string }[] }[];
      message?: string;
    }>(`/api/projects/${pid}/run-all-mistral`),
    runAllScaleway: (pid: number) => post<{
      ok?: boolean; slot: number; queued: number;
      skipped_by_deps?: { task_id: number; title: string; unmet: { id: number; title: string; status: string }[] }[];
      message?: string;
    }>(`/api/projects/${pid}/run-all-scaleway`),

    skills: {
      list: (pid: number) => get<{ project_id: number; skills: unknown[]; source: string }>(`/api/projects/${pid}/skills`),
      refresh: (pid: number) => post<{ ok: boolean; skills_md_path: string; detected: number }>(`/api/projects/${pid}/skills/refresh`),
      addManual: (pid: number, data: { category: string; name: string }) =>
        post<{ ok: true }>(`/api/projects/${pid}/skills/manual`, data),
      deleteManual: (pid: number, data: { category: string; name: string }) =>
        del<{ ok: true }>(`/api/projects/${pid}/skills/manual`, data),
    },

    permissions: {
      get: (pid: number) => get<{ project_id: number; groups: { key: string; label: string; rules: { rule: string; enabled: boolean }[] }[] }>(
        `/api/projects/${pid}/permissions`),
      refresh: (pid: number) => post<{ ok: boolean; settings_path: string }>(`/api/projects/${pid}/permissions/refresh`),
      toggle: (pid: number, data: { rule: string; enabled: boolean }) =>
        post<{ ok: true }>(`/api/projects/${pid}/permissions/toggle`, data),
      addCustom: (pid: number, data: { group_key: string; rule: string }) =>
        post<{ ok: true }>(`/api/projects/${pid}/permissions/custom`, data),
      deleteCustom: (pid: number, data: { rule: string }) =>
        del<{ ok: true }>(`/api/projects/${pid}/permissions/custom`, data),
      test: (pid: number, data: { command: string }) =>
        post<{ allowed: boolean; matched_rule?: string; reason?: string }>(`/api/projects/${pid}/permissions/test`, data),
    },

    contextOptions: (pid: number) => get<ContextOptions>(`/api/projects/${pid}/context-options`),

    spec: {
      get: (pid: number) => get<{ exists: boolean; content?: unknown; path?: string }>(`/api/projects/${pid}/spec`),
      put: (pid: number, content: unknown) =>
        put<{ ok: boolean; spec: unknown; path: string }>(`/api/projects/${pid}/spec`, { content }),
      merge: (pid: number, delta: unknown) =>
        post<{ ok: boolean; spec: unknown; changed_keys: string[] }>(`/api/projects/${pid}/spec/merge`, { delta }),
    },

    roles: {
      list: (pid: number) => get<Role[]>(`/api/projects/${pid}/roles`),
      create: (pid: number, data: Partial<Role>) => post<Role>(`/api/projects/${pid}/roles`, data),
      delete: (pid: number, rid: number) => del<{ ok: true }>(`/api/projects/${pid}/roles/${rid}`),
      fromTemplate: (pid: number, templateId: number) =>
        post<{ created: Role[]; count: number }>(`/api/projects/${pid}/roles/from-template`, { template_id: templateId }),
    },

    aingel: {
      init: (pid: number, model?: string) => post<{ ok: boolean; chat_id: number }>(`/api/projects/${pid}/aingel/init`, model ? { model } : undefined),
      overview: (pid: number) => get<{ markdown: string; path: string; updated_at?: string }>(`/api/projects/${pid}/aingel-overview`),
      refreshOverview: (pid: number) => post<{ ok: boolean; note: string }>(`/api/projects/${pid}/aingel-overview/refresh`),
    },

    scwSession: {
      create: (pid: number) => post<{ ok: boolean; session: Record<string, unknown> }>(`/api/projects/${pid}/scw-session`),
      close: (pid: number) => del<{ ok: boolean }>(`/api/projects/${pid}/scw-session`),
      status: (pid: number) => get<ScwSessionStatus>(`/api/projects/${pid}/scw-session`),
      upload: (pid: number, file: File, key?: string) => {
        const form = new FormData();
        form.append('file', file);
        if (key) form.append('key', key);
        return fetch(`/api/projects/${pid}/scw-session/upload`, { method: 'POST', body: form })
          .then(async r => {
            if (!r.ok) {
              let msg = 'upload failed';
              try { const j = await r.json(); if (j?.error) msg = j.error; } catch {}
              throw new Error(msg);
            }
            return r.json() as Promise<{ ok: boolean; key: string; size: number; synced?: number; sync_warning?: string }>;
          });
      },
      files: (pid: number) => get<{ files: Array<{ key: string; size: number; last_modified: string; etag: string }> }>(`/api/projects/${pid}/scw-session/files`),
      sync: (pid: number) => post<{ ok: boolean; synced: number }>(`/api/projects/${pid}/scw-session/sync`),
    },

    deployments: {
      list: (pid: number) => get<{ deployments: Deployment[] }>(`/api/projects/${pid}/deployments`).then(r => r.deployments || []),
      create: (pid: number, data: { model_name: string; node_type?: string; idle_delete_minutes?: number; endpoint_kind?: string }) =>
        post<{ ok: boolean; deployment: Deployment }>(`/api/projects/${pid}/deployments`, data),
      close: (pid: number, depId: number) => del<{ ok: boolean; final_cost_usd: number }>(`/api/projects/${pid}/deployments/${depId}`),
      models: (pid: number) => get<{ models: Array<{ id: string; name: string; node_types: string[]; eula_required: boolean }> }>(`/api/projects/${pid}/deployments/models`),
    },

    hfModels: {
      list: (pid: number) => get<{ models: ProjectHfModel[]; default_model: string }>(`/api/projects/${pid}/hf-models`),
      remove: (pid: number, repoId: string) =>
        del<{ ok: true }>(`/api/projects/${pid}/hf-models/${repoId}`),
    },
  },

  // ── Tasks ───────────────────────────────────────────────────────────
  tasks: {
    list: (params?: { status?: string; project_id?: number; include_archived?: boolean; work_session_slot?: number }) => {
      const qs = new URLSearchParams();
      if (params?.status) qs.set('status', params.status);
      if (params?.project_id) qs.set('project_id', String(params.project_id));
      if (params?.include_archived !== undefined) qs.set('include_archived', String(params.include_archived));
      if (params?.work_session_slot !== undefined) qs.set('work_session_slot', String(params.work_session_slot));
      const suffix = qs.toString() ? '?' + qs.toString() : '';
      return get<Task[]>('/api/tasks' + suffix);
    },
    create: (data: {
      project_id: number; title: string; description?: string; model?: string;
      priority?: number; phase_name?: string; estimated_tokens?: number; role_id?: number;
      hf_repo_id?: string; awaiting_model?: number;
      requires_rag?: number; corpus_id?: string;
      context_refs?: string[];
      original_description?: string;
    }) => post<{ id: number; ok: true }>('/api/tasks', data),
    get: (tid: number) => get<Task>(`/api/tasks/${tid}`),
    update: (tid: number, data: Partial<Task & { handoff_context?: string }>) =>
      patch<{ ok: boolean; auto_promoted?: string }>(`/api/tasks/${tid}`, data),
    delete: (tid: number) => del<{ ok: true }>(`/api/tasks/${tid}`),
    archive: (tid: number) => post<{ ok: true }>(`/api/tasks/${tid}/archive`),
    redo: (tid: number) => post<Task>(`/api/tasks/${tid}/redo`),
    copy: (tid: number, data: { title?: string; copy_deps?: boolean }) =>
      post<Task>(`/api/tasks/${tid}/copy`, data),
    import: () => post<{ project_name: string; tasks_imported: number }[]>('/api/tasks/import'),

    estimate: (tid: number) => get<EstimateResult & { task_id: number; model: string; budget?: BudgetInfo }>(`/api/tasks/${tid}/estimate`),
    recommendModel: (tid: number) =>
      post<{ task_type: string; task_type_label?: string; eu_only?: boolean; budget_remaining: number; profiles_reviewed?: string; recommendations: ModelRecommendation[]; hf_candidates?: HfCandidate[] }>(`/api/tasks/${tid}/recommend-model`),

    dependencies: {
      list: (tid: number) => get<TaskDependencies>(`/api/tasks/${tid}/dependencies`),
      add: (tid: number, data: { depends_on_id: number; spec_keys?: string[] }) =>
        post<{ ok: true }>(`/api/tasks/${tid}/dependencies`, data),
      remove: (tid: number, did: number) => del<{ ok: true }>(`/api/tasks/${tid}/dependencies/${did}`),
    },

    attachable: (projectId: number) => get<AttachableTask[]>(`/api/tasks/attachable?project_id=${projectId}`),

    gate: (tid: number, action: GateAction, answers?: Record<string, string>) =>
      post<{ ok: boolean; gate_state: string; chat_id?: number }>(`/api/tasks/${tid}/gate`, answers ? { action, answers } : { action }),
  },

  // ── Execute ─────────────────────────────────────────────────────────
  execute: (taskId: number) => post<{
    status: string; task_id: number; model: string;
    tokens_in: number; tokens_out: number; cost_usd: number;
    error?: string; quota?: string; limit?: number;
  }>('/api/execute', { task_id: taskId }),

  // ── Executions ──────────────────────────────────────────────────────
  executions: {
    list: (limit?: number) => get<Execution[]>(`/api/executions?limit=${limit || 50}`),
    get: (eid: number) => get<Execution>(`/api/executions/${eid}`),
    output: (eid: number) => get<{ content: string; source?: string }>(`/api/execution/output/${eid}`),
    diff: (eid: number) => get<{ diff: string; base_branch: string; task_branch: string }>(`/api/executions/${eid}/diff`),
    cancel: (eid: number) => post<{ ok: boolean; exec_id: number; status: string }>(`/api/executions/${eid}/cancel`),
    progressUrl: (eid: number) => `/api/executions/${eid}/progress`,
  },

  // ── Chats ───────────────────────────────────────────────────────────
  chats: {
    list: (params?: { project_id?: number; phase_name?: string; task_id?: number; status?: string }) => {
      const qs = new URLSearchParams();
      if (params?.project_id) qs.set('project_id', String(params.project_id));
      if (params?.phase_name) qs.set('phase_name', params.phase_name);
      if (params?.task_id) qs.set('task_id', String(params.task_id));
      if (params?.status) qs.set('status', params.status);
      const suffix = qs.toString() ? '?' + qs.toString() : '';
      return get<Chat[]>('/api/chats' + suffix);
    },
    create: (data: { project_id: number; name: string; phase_name?: string; task_id?: number; model?: string; attachments?: TaskAttachment[] }) =>
      post<Chat>('/api/chats', data),
    get: (cid: number) => get<Chat>(`/api/chats/${cid}`),
    update: (cid: number, data: Record<string, unknown>) => patch<{ ok: true }>(`/api/chats/${cid}`, data),
    delete: (cid: number) => del<{ ok: true }>(`/api/chats/${cid}`),
    status: () => get<{ busy: boolean; active_chat_id: number | null; started_at?: string }>('/api/chats/status'),
    sendMessage: (cid: number, data: { text: string; model?: string }) =>
      post<ChatMessageResponse>(`/api/chats/${cid}/message`, data),
    promoteMemory: (cid: number, data: {
      target_scope: MemoryLevel; mode?: string; selection?: string;
      phase_name?: string; model?: string; workflow_id?: number;
    }) => post<{ ok: boolean; memory_file: string; level: string; mode?: string; compaction?: string }>(
      `/api/chats/${cid}/promote-memory`, data),
    transcript: (cid: number) => get<{ content: string; lines: number }>(`/api/chats/${cid}/transcript`),
    streamUrl: (cid: number) => `/api/chats/${cid}/stream`,
    replyStatus: (cid: number) => get<{
      exec_id: number; status: string; tokens_in: number; tokens_out: number;
      cost_usd: number; started_at?: string; finished_at?: string;
      error?: string; done: boolean;
    }>(`/api/chats/${cid}/reply-status`),
  },

  // ── Memory ──────────────────────────────────────────────────────────
  memory: {
    get: (pid: number) => get<MemoryView>(`/api/memory/${pid}`),
    files: (pid: number) => get<FilesCatalog>(`/api/projects/${pid}/files`),
    approve: (data: { exec_id: number; level?: MemoryLevel; mode?: string; model?: string }) =>
      post<{ ok: boolean; memory_file: string; level: string; mode?: string; compaction?: string; git_merge?: string; stale_flagged?: number }>(
        '/api/memory/approve', data),
    revert: (execId: number) =>
      post<{ ok: boolean; level: string; memory_file?: string; memory_status: string; memory_removed?: boolean; git_revert?: string }>(
        '/api/memory/revert', { exec_id: execId }),
    reject: (data: { exec_id: number; feedback?: string }) =>
      post<{ ok: boolean; new_task_id: number; message: string; git_discard?: string }>('/api/memory/reject', data),
    skip: (data: { exec_id: number; reason?: string }) =>
      post<{ ok: boolean; task_id: number; memory_status: string; task_status: string }>('/api/memory/skip', data),
    fork: (data: { exec_id: number; feedback?: string; topic?: string }) =>
      post<{ ok: boolean; new_task_id: number; new_chat_id: number; phase_name?: string }>('/api/memory/fork', data),
  },

  // ── Phases ──────────────────────────────────────────────────────────
  phases: {
    list: () => get<Phase[]>('/api/phases'),
  },

  // ── Improve Prompt ──────────────────────────────────────────────────
  improvePrompt: (data: {
    title: string; description: string; attachments?: TaskAttachment[];
    project_id?: number; stack?: string; files?: unknown[];
    requires_rag?: number; corpus_id?: string;
  }) => post<{ improved: string; cost_usd: number }>('/api/improve-prompt', data),

  // ── Estimate ────────────────────────────────────────────────────────
  estimate: (data: { chat_id?: number; message?: string; task_id?: number }) =>
    post<EstimateResult>('/api/estimate', data),

  // ── Work Sessions & Columns ─────────────────────────────────────────
  workSessions: {
    list: () => get<WorkSession[]>('/api/work-sessions'),
    start: (slot: number, force?: boolean) => post<WorkSession>('/api/work-sessions/start', { slot, force }),
    status: () => get<WorkSession[]>('/api/work-sessions/status'),
  },

  columns: {
    run: (slot: number, taskIds: number[]) => post<{ ok: boolean; slot: number; queued: number }>(
      `/api/columns/${slot}/run`, { task_ids: taskIds }),
  },

  // ── Roles ───────────────────────────────────────────────────────────
  roles: {
    list: () => get<Role[]>('/api/roles'),
    create: (data: Partial<Role>) => post<Role>('/api/roles', data),
    update: (rid: number, data: Partial<Role>) => put<{ ok: true }>(`/api/roles/${rid}`, data),
    delete: (rid: number) => del<{ ok: true }>(`/api/roles/${rid}`),
    templates: () => get<Role[]>('/api/roles/templates'),
  },

  // ── Cost & Dashboard ────────────────────────────────────────────────
  costSummary: () => get<CostSummary>('/api/cost-summary'),
  dashboard: () => get<DashboardData>('/api/dashboard'),

  // ── Cross-project GPU run window ─────────────────────────────────────
  hfQueue: () => get<{ groups: HfQueueGroup[] }>('/api/hf-queue'),
  gpuWindow: {
    list: () => get<{ windows: GpuWindow[] }>('/api/gpu-window'),
    models: () => get<{ models: GpuWindowModel[] }>('/api/gpu-window/models'),
    open: (data: { model_name: string; node_type?: string; idle_delete_minutes?: number; endpoint_kind?: string }) =>
      post<{ ok: boolean; deployment: Deployment }>('/api/gpu-window', data),
    run: (depId: number, taskIds: number[]) =>
      post<{ ok: boolean; queued: number; model_id: string; warnings?: string[] }>(`/api/gpu-window/${depId}/run`, { task_ids: taskIds }),
    runStatus: (depId: number) =>
      get<{ run: GpuRunStatus | null }>(`/api/gpu-window/${depId}/run-status`),
    close: (depId: number) =>
      del<{ ok: boolean; final_cost_usd: number; cost_by_project: Record<string, number> }>(`/api/gpu-window/${depId}`),
  },

  // ── Unified File Manager (Phase 1 + F7 chunked) ────────────────────────
  files: {
    catalog: (pid: number) => get<FilesCatalog>(`/api/projects/${pid}/files`),
    uploadChunk: async (pid: number, opts: { upload_id: string; chunk_index: number; total_chunks: number; key: string; blob: Blob }): Promise<{ ok: boolean; chunk: number; received: number }> => {
      const form = new FormData();
      form.append('file', opts.blob);
      form.append('upload_id', opts.upload_id);
      form.append('chunk_index', String(opts.chunk_index));
      form.append('total_chunks', String(opts.total_chunks));
      form.append('key', opts.key);
      const res = await fetch(BASE + `/api/projects/${pid}/files/upload-chunk`, { method: 'POST', body: form });
      if (!res.ok) return fail(res);
      return res.json();
    },
    uploadComplete: async (pid: number, opts: { upload_id: string; key: string; total_chunks: number; total_size?: number }): Promise<{ ok: boolean; rel: string; key: string; size: number; synced?: boolean; sync_warning?: string }> => {
      return post<{ ok: boolean; rel: string; key: string; size: number; synced?: boolean; sync_warning?: string }>(`/api/projects/${pid}/files/upload-complete`, opts);
    },
    uploadFiles: async (
      pid: number,
      filesWithKeys: Array<{ file: File; key: string }>,
      onProgress?: (msg: string) => void,
    ): Promise<{ ok: boolean; files: Array<{ rel: string; key: string; size: number; synced?: boolean; sync_warning?: string }> }> => {
      const CHUNK_SIZE = 20 * 1024 * 1024;
      const genUploadId = (): string => {
        try {
          const c: unknown = (globalThis as unknown as { crypto?: { randomUUID?: () => string } }).crypto;
          if (c && typeof (c as { randomUUID?: () => string }).randomUUID === 'function') {
            return (c as { randomUUID: () => string }).randomUUID().toLowerCase();
          }
        } catch { /* ignore */ }
        // fallback: hex with dashes
        const hex = () => Math.floor(Math.random() * 0xffffffff).toString(16).padStart(8, '0');
        return `${hex()}-${hex().slice(0, 4)}-${hex().slice(0, 4)}-${hex().slice(0, 4)}-${hex()}${hex()}`.toLowerCase();
      };
      const smallFiles: Array<{ file: File; key: string }> = [];
      const largeFiles: Array<{ file: File; key: string }> = [];
      for (const item of filesWithKeys) {
        if (item.file.size > CHUNK_SIZE) largeFiles.push(item);
        else smallFiles.push(item);
      }
      const out: Array<{ rel: string; key: string; size: number; synced?: boolean; sync_warning?: string }> = [];
      let smallError: string | null = null;
      // Batch-upload small files in one request
      if (smallFiles.length) {
        try {
          const form = new FormData();
          const keys: string[] = [];
          for (const { file, key } of smallFiles) {
            form.append('file', file);
            keys.push(key || (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name);
          }
          form.append('keys', JSON.stringify(keys));
          const res = await fetch(BASE + `/api/projects/${pid}/files/upload`, { method: 'POST', body: form });
          if (!res.ok) {
            const err = await res.text().catch(() => res.statusText);
            throw new Error(err || `upload failed (${res.status})`);
          }
          const json = await res.json() as { ok: boolean; files: Array<{ rel: string; key: string; size: number; synced?: boolean; sync_warning?: string }> };
          if (json.files) out.push(...json.files);
        } catch (e) {
          smallError = e instanceof Error ? e.message : String(e);
        }
      }
      // Chunked upload for large files — per-file isolation so one failure doesn't abort the rest
      const largeErrors: string[] = [];
      for (const { file, key } of largeFiles) {
        const rawKey = key || (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
        try {
          const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
          const upload_id = genUploadId();
          for (let i = 0; i < totalChunks; i++) {
            const blob = file.slice(i * CHUNK_SIZE, (i + 1) * CHUNK_SIZE);
            onProgress?.(`Uploading large file ${rawKey} chunk ${i + 1}/${totalChunks}…`);
            await (api.files as unknown as { uploadChunk: typeof api.files.uploadChunk }).uploadChunk(pid, {
              upload_id,
              chunk_index: i,
              total_chunks: totalChunks,
              key: rawKey,
              blob,
            });
          }
          onProgress?.(`Finalizing ${rawKey}…`);
          const complete = await (api.files as unknown as { uploadComplete: typeof api.files.uploadComplete }).uploadComplete(pid, {
            upload_id,
            key: rawKey,
            total_chunks: totalChunks,
            total_size: file.size,
          });
          if (!complete || !(complete as unknown as { rel?: string }).rel) {
            throw new Error(`complete missing rel for ${rawKey}`);
          }
          const c = complete as unknown as { rel: string; key: string; size: number; synced?: boolean; sync_warning?: string };
          out.push({ rel: c.rel, key: rawKey, size: c.size, synced: c.synced, sync_warning: c.sync_warning });
        } catch (e) {
          largeErrors.push(`${rawKey}: ${e instanceof Error ? e.message : String(e)}`);
        }
      }
      if (out.length === 0 && (smallError || largeErrors.length)) {
        throw new Error([smallError, ...largeErrors].filter(Boolean).join('; '));
      }
      if (largeErrors.length || smallError) {
        const failed = [smallError ? `small batch: ${smallError}` : null, ...largeErrors].filter(Boolean).join('; ');
        // surface partial failure — caller will show error instead of false success
        if (out.length === 0) throw new Error(failed);
        throw new Error(`Some files failed — ${failed} — ${out.length} file(s) uploaded successfully`);
      }
      return { ok: true, files: out };
    },
    mkdir: (pid: number, path: string) => post<{ ok: boolean; path: string }>(`/api/projects/${pid}/files/mkdir`, { path }),
    move: (pid: number, src: string, dst: string) => post<{ ok: boolean; src: string; dst: string }>(`/api/projects/${pid}/files/move`, { src, dst }),
    copy: (pid: number, src: string, dst: string) => post<{ ok: boolean; src: string; dst: string }>(`/api/projects/${pid}/files/copy`, { src, dst }),
    deleteFiles: (pid: number, paths: string[]) => post<{ ok: boolean; trashed: string[] }>(`/api/projects/${pid}/files/delete`, { paths }),
    getTags: (pid: number) => get<{ tags: Record<string, { tags: string[]; note: string; updated_at?: string }> }>(`/api/projects/${pid}/files/tags`),
    setTags: (pid: number, path: string, tags: string[], note?: string) =>
      post<{ ok: boolean; path: string; tags: string[]; note: string }>(`/api/projects/${pid}/files/tags`, { path, tags, note }),
  },

  // ── Search ──────────────────────────────────────────────────────────
  searchChats: (q: string) => get<{ results: { chat_id: number; chat_name: string; project_name: string; snippet: string }[] }>(
    `/api/search/chats?q=${encodeURIComponent(q)}`),
};
