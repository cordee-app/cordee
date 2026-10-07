export interface Project {
  id: number;
  name: string;
  slug: string;
  path: string;
  project_type: string;
  eu_only?: boolean;
  done: number;
  running: number;
  pending: number;
  total: number;
  def_filename?: string;
  definition_file?: string;
  guide_file?: string;
  skills_file?: string;
  spec_file?: string;
  aingel_chat_id?: number;
  aingel_model?: string;
  aingel_autopilot?: number | boolean;
  aingel_mode?: string;
  scw_session_enabled?: boolean;
  scw_project_id?: string;
  scw_session_bucket?: string;
  scw_kms_key_id?: string;
  scw_session_region?: string;
  scw_session_created_at?: string;
  active_deployment_count?: number;
  active_deployment_cost_usd?: number;
  use_rag?: boolean;
  rag_corpus_id?: string;
  current_user_role?: 'viewer' | 'member' | 'admin' | 'owner' | null;
}

export interface ScwSessionStatus {
  enabled: boolean;
  project_id?: string;
  bucket?: string;
  kms_key_id?: string;
  region: string;
  created_at?: string;
  costs?: Array<{ day: string; category: string; eur: number; usd: number }>;
}

export interface Deployment {
  id: number;
  project_id: number | null;
  scw_deployment_id: string;
  model_name: string;
  node_type: string;
  endpoint_kind: string;
  endpoint_url: string;
  status: string;
  hourly_eur: number;
  idle_delete_minutes: number;
  accrued_cost_usd: number;
  created_at: string;
  deleted_at?: string;
  is_shared?: number;
}

export interface Model {
  id: string;
  label: string;
  provider: string;
  color: string;
  cost_input?: number;
  cost_output?: number;
  default?: boolean;
  free_allowed?: boolean;
}

export type TaskStatus = 'pending' | 'confirmed' | 'running' | 'done' | 'failed' | 'skip' | 'cancelled';

export interface Task {
  id: number;
  project_id: number;
  title: string;
  description: string;
  status: TaskStatus;
  model: string;
  priority: number;
  phase_name: string;
  estimated_tokens: number;
  estimated_cost?: number;
  role_id?: number;
  handoff_context?: string;
  archived?: number;
  work_session_slot?: number;
  gate_state?: string;
  gate_source?: string;
  gate_reason?: string;
  gate_report_h2_json?: string;
  gate_questions?: GateQuestion[];
  actual_tokens?: number;
  hf_repo_id?: string;
  awaiting_model?: number;
  corpus_id?: string;
  requires_rag?: boolean | number;
  context_refs?: string[];
  execution_type?: string | null;
}

export type ExecutionStatus = 'pending' | 'running' | 'done' | 'failed' | 'rejected' | 'skipped';

export interface Execution {
  id: number;
  task_id: number;
  chat_id?: number;
  status: ExecutionStatus;
  model: string;
  model_label?: string;
  tokens_input: number;
  tokens_output: number;
  cost_usd: number;
  started_at?: string;
  finished_at?: string;
  last_heartbeat_at?: string;
  instructions?: string;
  git_commit?: string;
  git_branch?: string;
  git_merge_commit?: string;
  git_diffstat?: string;
  aingel_review?: string;
  project_id?: number;
  project_name?: string;
  task_title?: string;
  task_ext_id?: string;
  error_message?: string;
  output_summary?: string;
  memory_status?: string;
  session_id?: number;
  has_output_file?: boolean;
  rag_label?: string;
  rag_provenance_json?: string;
}

export interface Chat {
  id: number;
  project_id: number;
  name: string;
  model: string;
  status: string;
  phase_name?: string;
  task_id?: number;
  attachments?: AttachItem[];
  auto_inject_defs?: boolean;
  transcript?: string;
  executions?: Execution[];
  project_name?: string;
  task_title?: string;
  message_count?: number;
  scaffold_draft?: string;
}

export interface ChatTranscriptEntry {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: string;
}

export interface AttachItem {
  kind?: string;
  ref?: string;
  label?: string;
  name?: string;
  type?: string;
  path?: string;
  size?: number;
  truncated?: boolean;
}

/** Shape returned by /api/projects/<pid>/context-options (definitions /
 *  memories / tasks / working_docs) and /api/tasks/attachable. */
export interface ContextOptionItem {
  kind: string;
  ref: string;
  label?: string;
  name?: string;
  size?: number;
  status?: string;
  phase_name?: string;
  level?: string;
  phase_slug?: string;
  ext?: string;
  has_dependencies?: boolean;
  color?: string;
  id?: number;
}

export interface ContextOptions {
  definitions: ContextOptionItem[];
  memories: ContextOptionItem[];
  tasks: ContextOptionItem[];
  working_docs: ContextOptionItem[];
}

/** Attachment as sent to /api/improve-prompt and stored in chat.attachments. */
export interface TaskAttachment {
  kind: 'definition' | 'memory' | 'task' | 'working_doc';
  ref: string;
  label: string;
}

/** Item returned by /api/tasks/attachable — tasks with saved outputs in a project. */
export interface AttachableTask {
  kind: 'task';
  ref: number;
  id: number;
  name: string;
  label: string;
  has_dependencies: boolean;
  status: string;
  color?: string;
}

export interface Phase {
  id?: number;
  name: string;
  steps?: string[];
  project_id?: number;
}

export interface PhaseData {
  project_id: number;
  phases: Phase[];
}

/** A row from GET /api/tasks/<id>/dependencies.
 *
 * The endpoint returns full task rows (db.get_dependencies / get_dependents look
 * each one up with get_task) plus the join's spec keys — NOT the raw
 * task_dependencies row, so there is no `depends_on_id` here. `id` is the id of
 * the related task, in both the `depends_on` and `depended_on_by` directions.
 * The raw join rows, with task_id/depends_on_id, come from
 * GET /api/projects/<id>/dependencies (see DependencyGraph). */
export type Dependency = Task & { dep_spec_keys?: string };

/** A raw task_dependencies join row from GET /api/projects/<id>/dependencies,
 *  denormalised with both endpoints' titles/statuses (db.get_project_dependencies). */
export interface DependencyEdge {
  task_id: number;
  depends_on_id: number;
  spec_keys?: string | null;
  task_title?: string;
  task_status?: string;
  task_project_id?: number;
  task_project_name?: string;
  task_stale?: number;
  depends_on_title?: string;
  depends_on_status?: string;
  depends_on_project_id?: number;
  depends_on_project_name?: string;
  depends_on_stale?: number;
}

export interface TaskDependencies {
  depends_on: Dependency[];
  depended_on_by: Dependency[];
}

export interface Role {
  id: number;
  name: string;
  system_prompt: string;
  default_model?: string;
  context_scope: string;
  is_template: boolean;
  project_id?: number;
}

export interface WorkSession {
  slot: number;
  status: string;
  started_at?: string;
  ends_at?: string;
  seconds_remaining?: number;
  expired?: boolean;
  token_budget?: number;
  tokens_used?: number;
  paused_reason?: string;
  pause_reason?: string;
  next_run_at?: string;
  duration_seconds?: number;
  task_count?: number;
  pending_count?: number;
  running_count?: number;
}

export interface CostSummary {
  total_spent: number;
  today: number;
  week: number;
  by_model: { model: string; total_cost: number; exec_count: number }[];
}

export interface DashboardData {
  projects: Project[];
  global: {
    total_projects: number;
    total_tasks: number;
    done_tasks: number;
    running_tasks: number;
    pending_tasks: number;
    failed_tasks: number;
    overall_progress_pct: number;
  };
}

export interface Config {
  anthropic_mode?: string;
  mistral_mode?: string;
  claude_code_skip_permissions?: boolean;
  vibe_skip_permissions?: boolean;
  claude_pro_token_budget?: number;
  rag_corpora?: { id: string; label: string }[];
  instance_name?: string;
  is_vault?: boolean;
  ready?: ProviderReadiness;
}

export interface ProviderReadiness {
  claude_code?: boolean;
  vibe?: boolean;
  mistral_api?: boolean;
  codex?: boolean;
  openai_api?: boolean;
  scaleway?: boolean;
  ollama?: boolean;
  api?: boolean;
}

export interface User {
  id: number;
  email: string;
  name: string;
  role: 'user' | 'admin';
  plan: 'free' | 'paid';
  status: 'active' | 'suspended';
}

/** Project membership role, ranked viewer < member < admin < owner. */
export type MemberRole = 'viewer' | 'member' | 'admin' | 'owner';

/** Row from GET /api/projects/<pid>/members — joins users for email/name. */
export interface ProjectMember {
  project_id: number;
  user_id: number;
  role: MemberRole;
  email?: string;
  name?: string;
}

/** Row from GET /api/admin/users (full user row incl. oidc_sub). */
export interface AdminUser extends User {
  oidc_sub?: string;
  created_at?: string;
}

/** Minimal user row from GET /api/users — enough to pick a member by name. */
export interface DirectoryUser {
  id: number;
  name: string;
  email: string;
  role: 'user' | 'admin';
}

export interface QuotaStatus {
  plan: 'free';
  limits: {
    max_projects: number;
    max_runs: number;
    max_storage: number;
    max_tokens: number;
  };
  usage: {
    runs: number;
    tokens_in: number;
    tokens_out: number;
    storage_bytes: number;
    tokens_total: number;
  };
}

export interface MeResponse {
  user: User;
  month: string;
  usage: {
    user_id: number;
    month: string;
    runs: number;
    storage_bytes: number;
    tokens_in: number;
    tokens_out: number;
    gpu_minutes: number;
  };
  quotas: QuotaStatus | null;
}

export interface ModelRecommendation {
  model: string;
  label: string;
  task_type: string;
  reason: string;
  estimated_cost: number;
  quality_score: number;
  balanced_score?: number;
  gate_penalty?: number;
  category?: 'best_outcome' | 'balanced' | 'best_price';
  justification?: string;
  tier?: string;
  strengths?: string[];
  eu?: boolean;
  provider?: string;
  context_window?: number | null;
  source?: 'catalogue' | 'hf';
  repo_id?: string;
  load_status?: string;
  load_cost?: number;
}

export interface ProjectHfModel {
  repo_id: string;
  model_id: string;
  label: string;
  provider: string;
  validation_score: number;
  pipeline_tag?: string;
  limitations?: string[];
  hf_url?: string;
  is_default: boolean;
  import_status?: string | null;
  import_model_name?: string | null;
  import_error?: string | null;
  import_ready?: boolean;
  import_id?: number | null;
}

export interface HfCandidate {
  repo_id: string;
  label: string;
  pipeline_tag: string;
  language: string[];
  tags: string[];
  license: string;
  provider_mapping: string;
  validation: {
    score: number;
    reasons: string[];
    servable: boolean;
    task_model?: boolean;
  };
  servable: boolean;
  task_model?: boolean;
  limitations: string[];
  hf_url: string;
  import_status?: string | null;
  import_model_name?: string | null;
  import_error?: string | null;
  import_ready?: boolean;
}

export interface HfSearchResult {
  repo_id: string;
  label: string;
  pipeline_tag: string;
  tags: string[];
  language: string[];
  license: string;
  downloads: number;
  likes: number;
  provider_mapping: string;
  eu?: boolean;
  hf_url?: string;
  validation: {
    score: number;
    reasons: string[];
    servable: boolean;
    task_model?: boolean;
  };
}

export interface HfQueueTask {
  id: number;
  title: string;
  status: string;
  model: string;
  awaiting_model: boolean;
  project_id: number;
  project_name: string;
}

export interface HfQueueGroup {
  repo_id: string;
  label: string;
  provider_mapping: string;
  servable: boolean;
  import_status?: string | null;
  import_model_name?: string | null;
  import_ready?: boolean;
  tasks: HfQueueTask[];
}

export interface GpuWindow {
  id: number;
  project_id: number | null;
  scw_deployment_id: string;
  model_name: string;
  node_type: string;
  endpoint_kind: string;
  endpoint_url: string;
  status: string;
  provider_status?: string;
  max_context_size?: number;
  hourly_eur: number;
  idle_delete_minutes: number;
  accrued_cost_usd: number;
  created_at: string;
  deleted_at?: string;
  is_shared?: number;
  calls?: Array<{ id: number; project_id: number | null; task_id: number | null; exec_id: number | null; tokens_in: number; tokens_out: number; cost_usd: number }>;
  cost_by_project?: Record<string, number>;
}

export interface GpuDeployableModel {
  id: string;
  name: string;
  node_types: string[];
  eula_required: boolean;
  size_bytes?: number;
  parameter_size_bits?: number;
  status?: string;
  max_context_size?: number;
  stock_status?: Record<string, string>;
  hourly_eur?: Record<string, number>;
}

export interface GpuRunStatus {
  dep_id: number;
  started_at: string;
  tasks: Record<string, { status: string; error: string }>;
}

export interface HfImportVerify {
  ok: boolean;
  repo_id?: string;
  nodes?: string[];
  quantizations?: Record<string, number[]>;
  max_context_size?: number | null;
  size_bytes?: number;
  hourly_eur?: Record<string, number>;
  error?: string;
  error_code?: string;
}

export interface HfModelImport {
  id: number;
  project_id: number | null;
  repo_id: string;
  model_name: string;
  scw_model_id: string | null;
  status: string;
  error_message: string | null;
  size_bytes: number | null;
  created_at: string;
  updated_at: string;
}

export interface GpuWindowModel {
  repo_id: string;
  label: string;
  servable: boolean;
  provider_mapping: string;
  task_count: number;
  params_b: number;
  estimated_node: string;
  estimated_hourly_eur: number;
  options: GpuDeployableModel[];
}

export interface ChatMessageResponse {
  accepted?: boolean;
  chat_id?: number;
  reply?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  exec_id?: number;
  stream_url?: string;
  poll_url?: string;
}

export interface EstimateResult {
  model: string;
  model_label: string;
  total_input_tokens: number;
  estimated_output_tokens: number;
  output_tokens_min: number;
  output_tokens_max: number;
  cost_min_usd: number | null;
  cost_max_usd: number | null;
  breakdown: Record<string, number>;
  historical_samples: number;
}

export interface BudgetInfo {
  project_id: number;
  project_name: string;
  monthly_budget: number;
  current_month_spend: number;
  budget_reset_day: number;
  remaining: number;
  percent_used: number;
  exceeded: boolean;
}

export type FileCategory =
  | 'system' | 'definition' | 'memory' | 'chat' | 'output'
  | 'reference' | 'code' | 'deliverable' | 'archive';

export interface FileEntry {
  name: string;
  path: string;
  rel: string;
  ext: string;
  size: number;
  modified: string;
  category: FileCategory;
  hidden: boolean;
  task_id?: number | null;
  exec_id?: number | null;
  tags?: string[];
  note?: string;
  tags_updated_at?: string;
}

export interface TaskFileGroup {
  task_id: number | null;
  task_title: string | null;
  exec_id?: number | null;
  finished_at?: string | null;
  files: FileEntry[];
}

export interface FilesCatalog {
  files: FileEntry[];
  count: number;
  by_category: Record<string, number>;
  by_task: TaskFileGroup[];
  tags?: Record<string, { tags: string[]; note: string; updated_at?: string }>;
  folders?: string[];
}

export interface MemoryView {
  project?: Project;
  files?: { name: string; path: string; size: number; type?: string; file_type?: string; modified?: string }[];
  working_docs?: { name: string; path: string; size: number; modified?: string; ext?: string }[];
  deliverables?: { name: string; path: string; rel?: string; size: number; modified?: string; ext?: string; category?: FileCategory; task_id?: number | null; exec_id?: number | null }[];
  by_task?: TaskFileGroup[];
  files_catalog?: FilesCatalog;
  definitions?: { name: string; path: string; size: number; modified?: string }[];
  permissions?: { groups?: { key: string; label: string; rules: { rule: string; enabled: boolean }[] }[] };
  roles?: Role[];
  spec?: { exists: boolean; content?: unknown; path?: string };
  git?: { setting: number | null; enabled: boolean; is_repo: boolean; is_software: boolean };
}

export type GitMode = 'auto' | 'on' | 'off';

export interface ArchiveResult {
  ok: true;
  filename: string;
  size: number;
  download_url: string;
  /** Server cap (bytes) on an uploaded archive. A zip larger than this can
   *  never be re-imported through the app UI. */
  import_limit: number;
  /** Set when the pre-archive bucket sync ran but skipped some objects. */
  sync_warning?: string;
}

export interface ImportResult {
  ok: true;
  /** Raw project row returned by the importer — no task stats joined. */
  project: Pick<Project, 'id' | 'name' | 'slug' | 'path'> & Record<string, unknown>;
}

export interface GateQuestion {
  id: string;
  question: string;
  why?: string;
  options?: string[];
}

export type GateAction = 'acknowledge' | 'override' | 'discuss' | 'answer';

export type MemoryLevel = 'phase' | 'project' | 'workflow';
