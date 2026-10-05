import { useStore } from '../store';
import { cn } from '../utils/cn';
import { useIsMobile } from '../hooks/useIsMobile';
import { useProjectPermissions, roleAtLeast } from '../hooks/useProjectPermissions';
import { Lock, Trash2, Users } from 'lucide-react';
import type { CSSProperties } from 'react';

function projectColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
    hash = hash & hash;
  }
  const h = Math.abs(hash) % 360;
  return `hsl(${h}, 55%, 40%)`;
}

function EUFlag({ className }: { className?: string }) {
  const angles = Array.from({ length: 12 }, (_, i) => i * 30);
  return (
    <svg className={className} viewBox="0 0 180 120" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
      <rect width="180" height="120" fill="#003399" />
      {angles.map((a) => (
        <g key={a} transform={`rotate(${a} 90 60)`}>
          <path
            d="M0 -8 L1.9 -2.5 L7.6 -2.5 L3.1 1.4 L4.7 6.9 L0 3.5 L-4.7 6.9 L-3.1 1.4 L-7.6 -2.5 L-1.9 -2.5 Z"
            fill="#FFCC00"
            transform="translate(90 24)"
          />
        </g>
      ))}
    </svg>
  );
}

function LockIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zm-6 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2zm3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1 1.71 0 3.1 1.39 3.1 3.1v2z"
        fill="currentColor"
      />
    </svg>
  );
}

interface GlobalStats {
  total_projects: number;
  total_tasks: number;
  total_done: number;
  total_running: number;
  total_pending: number;
  total_failed: number;
  hf_models_count: number;
  awaiting_model_tasks: number;
  week_total: number;
  week_done: number;
  week_running: number;
  week_pending: number;
  week_failed: number;
  overall_progress_pct: number;
  total_spent: number;
  today_spent: number;
  week_spent: number;
}

interface ProjectStat {
  id: number;
  name: string;
  slug: string;
  eu_only?: boolean;
  scw_session_enabled?: boolean;
  scw_session_bucket?: string;
  scw_kms_key_id?: string;
  total_tasks: number;
  done_tasks: number;
  running_tasks: number;
  pending_tasks: number;
  failed_tasks: number;
  hf_models_count?: number;
  awaiting_model_tasks?: number;
  week_done: number;
  week_running: number;
  week_pending: number;
  week_failed: number;
  progress_pct: number;
  current_month_spend: number;
  week_spent: number;
  budget_monthly: number;
  budget_pct: number;
  budget_exceeded: boolean;
  active_deployment_cost_usd?: number;
  current_user_role?: 'viewer' | 'member' | 'admin' | 'owner' | null;
  recent_executions: {
    id: number;
    task_title: string;
    model: string;
    status: string;
    cost_usd: number;
    started_at: string;
  }[];
}

interface DashboardData {
  global: GlobalStats;
  projects: ProjectStat[];
}

const Fmt = {
  cost: (n: number) => `$${(n || 0).toFixed(2)}`,
  pct: (n: number) => `${(n || 0).toFixed(1)}%`,
  date: (s: string) => {
    const d = new Date(s.replace(' ', 'T') + (s.includes('Z') ? '' : 'Z'));
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  },
};

function WeekDelta({ n }: { n: number }) {
  if (!n) return null;
  return <span className="dash-week-delta text-xs text-text-muted dark:text-text-dark-muted font-normal ml-1">+{n}/wk</span>;
}

function ProjectDashboard({ p }: { p: ProjectStat }) {
  const isMobile = useIsMobile();
  const setActiveMainTab = useStore((s) => s.setActiveMainTab);

  return (
    <div className="dashboard p-4 max-w-full box-border">
      {isMobile && (
        <button
          data-tip="Open the task board"
          className="w-full mb-4 py-3 rounded-lg bg-accent dark:bg-accent-dark-DEFAULT text-white font-semibold text-base cursor-pointer border-0"
          onClick={() => setActiveMainTab('board')}
        >
          Open Board
        </button>
      )}
      <div className="dash-summary-row grid grid-cols-[repeat(auto-fit,minmax(140px,1fr))] gap-3 mb-4">
        <div className="dash-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-ink dark:text-text-dark-DEFAULT">{p.total_tasks}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Total Tasks <WeekDelta n={p.week_done + p.week_running + p.week_pending + p.week_failed} /></div>
        </div>
        <div className="dash-card accent-green bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-status-done dark:text-status-done-dark-DEFAULT">{p.done_tasks}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Done <WeekDelta n={p.week_done} /></div>
        </div>
        <div className="dash-card accent-blue bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-status-running dark:text-status-running-dark-DEFAULT">{p.running_tasks}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Running <WeekDelta n={p.week_running} /></div>
        </div>
        <div className="dash-card accent-orange bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-status-pending dark:text-status-pending-dark-DEFAULT">{p.pending_tasks}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Pending <WeekDelta n={p.week_pending} /></div>
        </div>
        <div className="dash-card accent-red bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-status-failed dark:text-status-failed-dark-DEFAULT">{p.failed_tasks}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Failed <WeekDelta n={p.week_failed} /></div>
        </div>
      </div>

      <div className="dash-mid-row grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3 mb-4">
        <div className="dash-card wide text-left bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4">
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Progress</div>
          <div className="progress-bar-wrap bg-border-muted dark:bg-border-dark-muted rounded-md h-2.5 my-1.5 overflow-hidden">
            <div
              className={cn('progress-bar h-full rounded-md transition-[width] duration-500', p.budget_exceeded ? 'danger bg-danger' : 'bg-accent dark:bg-accent-dark-DEFAULT')}
              style={{ width: `${Math.min(100, p.progress_pct)}%` }}
            />
          </div>
          <div className="progress-label text-sm font-semibold text-accent dark:text-accent-dark-DEFAULT">{Fmt.pct(p.progress_pct)}</div>
        </div>
        <div className="dash-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Monthly Budget</div>
          <div className="dash-card-value cost text-[24px] font-bold text-ink dark:text-text-dark-DEFAULT">{Fmt.cost(p.current_month_spend)}</div>
          <div className="dash-card-sub text-sm+ text-text-muted dark:text-text-dark-muted">
            {Fmt.cost(p.budget_monthly)} limit
            <span className={cn(p.budget_exceeded && 'accent-red text-status-failed dark:text-status-failed-dark-DEFAULT')}> ({Fmt.pct(p.budget_pct)})</span>
          </div>
        </div>
      </div>

      <div className="dash-project-grid grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-3.5">
        <div className="dash-project-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4" style={{ borderLeft: `4px solid ${projectColor(p.name)}` }}>
          <div className="dash-project-header flex justify-between items-start mb-2">
            <span className="dash-project-name text-base font-semibold leading-snug flex-1 dark:text-text-dark-DEFAULT">{p.name}</span>
            <span className="dash-project-stats text-sm+ text-text-muted dark:text-text-dark-muted whitespace-nowrap ml-2">
              {Fmt.pct(p.progress_pct)} done — {p.running_tasks} running, {p.pending_tasks} pending
              {p.failed_tasks > 0 && `, ${p.failed_tasks} failed`}
            </span>
          </div>

          <div className="progress-bar-wrap small bg-border-muted dark:bg-border-dark-muted rounded-md h-1.5 my-1.5 overflow-hidden">
            <div
              className={cn('progress-bar h-full rounded-md transition-[width] duration-500', p.budget_exceeded ? 'danger bg-danger' : 'bg-accent dark:bg-accent-dark-DEFAULT')}
              style={{ width: `${Math.min(100, p.progress_pct)}%` }}
            />
          </div>

          <div className="dash-project-budget text-sm+ text-text-muted dark:text-text-dark-muted my-1 flex gap-1.5">
            <span>Budget: {Fmt.cost(p.current_month_spend)} / {Fmt.cost(p.budget_monthly)}</span>
            <span className={cn(p.budget_exceeded && 'accent-red text-status-failed dark:text-status-failed-dark-DEFAULT')}>({Fmt.pct(p.budget_pct)})</span>
          </div>

          <div className="dash-project-cost text-sm+ text-text-muted dark:text-text-dark-muted my-1 pt-1 border-t border-border-subtle dark:border-border-dark-subtle">
            This month: {Fmt.cost(p.current_month_spend)} | This week: {Fmt.cost(p.week_spent)}
          </div>

          {p.recent_executions.length > 0 && (
            <div className="dash-recent-execs border-t border-border-subtle dark:border-border-dark-subtle mt-2.5">
              <div className="dash-sub-label text-xs uppercase text-text-faint dark:text-text-dark-faint my-2.5 mx-0 tracking-wide">Recent Executions</div>
              {p.recent_executions.slice(0, 5).map((e) => (
                <div key={e.id} className="dash-exec-row flex items-center gap-2 py-0.5 text-sm+">
                  <span className={cn('dash-exec-dot w-1.5 h-1.5 rounded-full shrink-0', `status-${e.status}`)} />
                  <span className="dash-exec-title flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-ink-soft dark:text-text-dark-soft">{e.task_title}</span>
                  <span className="dash-exec-cost text-text-muted dark:text-text-dark-muted font-mono text-xs">{Fmt.cost(e.cost_usd)}</span>
                  <span className="dash-exec-date text-text-faint dark:text-text-dark-faint text-xs">{Fmt.date(e.started_at)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export const Dashboard = () => {
  const { dashboardData, activeProject, setActiveProject, setActiveMainTab, setShowNewProjectModal, setShowGpuWindowModal, setShowDeleteProjectModal, setDeleteProjectTarget, setShowMembersModal, setMembersProjectId, quotas } = useStore();
  const isMobile = useIsMobile();
  const perms = useProjectPermissions(activeProject);
  if (!dashboardData) return <div className="p-4">Loading dashboard...</div>;

  const d = dashboardData as DashboardData;
  const g = d.global;

  // Project creation is account-level and quota-gated server-side. Hide the
  // card only when the caller cannot create: admins/auth-off always can; a
  // free user can until they reach max_projects. A viewer at their limit (whose
  // membership already consumes the single free project) sees nothing rather
  // than a card that returns 429.
  const canCreateProject = perms.authOff || perms.isAdmin
    || (quotas ? d.projects.length < quotas.limits.max_projects : true);

  if (activeProject !== null) {
    const p = d.projects.find(pr => pr.id === activeProject);
    if (p) return <ProjectDashboard p={p} />;
  }

  return (
    <div className="dashboard p-4 max-w-full box-border">
      <div className="dash-summary-row grid grid-cols-[repeat(auto-fit,minmax(140px,1fr))] gap-3 mb-4">
        <div className="dash-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-ink dark:text-text-dark-DEFAULT">{g.total_projects}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Projects</div>
        </div>
        <div className="dash-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-ink dark:text-text-dark-DEFAULT">{g.total_tasks}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Total Tasks <WeekDelta n={g.week_total} /></div>
        </div>
        <div className="dash-card accent-green bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-status-done dark:text-status-done-dark-DEFAULT">{g.total_done}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Done <WeekDelta n={g.week_done} /></div>
        </div>
        <div className="dash-card accent-blue bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-status-running dark:text-status-running-dark-DEFAULT">{g.total_running}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Running <WeekDelta n={g.week_running} /></div>
        </div>
        <div className="dash-card accent-orange bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-status-pending dark:text-status-pending-dark-DEFAULT">{g.total_pending}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Pending <WeekDelta n={g.week_pending} /></div>
        </div>
        <div className="dash-card accent-red bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-status-failed dark:text-status-failed-dark-DEFAULT">{g.total_failed}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Failed <WeekDelta n={g.week_failed} /></div>
        </div>
        <div className="dash-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-value text-[28px] font-bold text-accent dark:text-accent-dark-DEFAULT">{g.hf_models_count}</div>
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">HF Models</div>
          {g.awaiting_model_tasks > 0 && (
            <div className="text-xs text-status-pending dark:text-status-pending-dark-DEFAULT font-semibold">{g.awaiting_model_tasks} awaiting self-host</div>
          )}
        </div>
        {perms.canAdminister && (
          <div
            className="dash-card clickable bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center cursor-pointer transition-[box-shadow,border-color] hover:shadow-medium hover:border-accent dark:hover:border-accent-dark-DEFAULT"
            onClick={() => setShowGpuWindowModal(true)}
            title="Open the cross-project GPU run window"
          >
            <div className="dash-card-value text-[28px] font-bold text-accent dark:text-accent-dark-DEFAULT">GPU</div>
            <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Run Window</div>
          </div>
        )}
      </div>

      <div className="dash-mid-row grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3 mb-4">
        <div className="dash-card wide text-left bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4">
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Overall Progress</div>
          <div className="progress-bar-wrap bg-border-muted dark:bg-border-dark-muted rounded-md h-2.5 my-1.5 overflow-hidden">
            <div className="progress-bar h-full rounded-md transition-[width] duration-500 bg-accent dark:bg-accent-dark-DEFAULT" style={{ width: `${Math.min(100, g.overall_progress_pct)}%` }} />
          </div>
          <div className="progress-label text-sm font-semibold text-accent dark:text-accent-dark-DEFAULT">{Fmt.pct(g.overall_progress_pct)}</div>
        </div>
        <div className="dash-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Total Spent</div>
          <div className="dash-card-value cost text-[24px] font-bold text-ink dark:text-text-dark-DEFAULT">{Fmt.cost(g.total_spent)}</div>
        </div>
        <div className="dash-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">Today</div>
          <div className="dash-card-value text-[28px] font-bold text-ink dark:text-text-dark-DEFAULT">{Fmt.cost(g.today_spent)}</div>
        </div>
        <div className="dash-card bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 text-center">
          <div className="dash-card-label text-sm uppercase text-text-muted dark:text-text-dark-muted mb-1 tracking-wide">This Week</div>
          <div className="dash-card-value text-[28px] font-bold text-ink dark:text-text-dark-DEFAULT">{Fmt.cost(g.week_spent)}</div>
        </div>
      </div>

      <div className="dash-project-grid grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-3.5">
        {canCreateProject && (
          <div
            className="dash-project-card clickable new-project-card bg-surface-raised dark:bg-surface-dark-raised border-2 border-dashed border-border-strong dark:border-border-dark-strong rounded-lg p-4 cursor-pointer transition-[box-shadow,border-color,background] hover:shadow-medium hover:border-accent dark:hover:border-accent-dark-DEFAULT hover:bg-accent-soft dark:hover:bg-accent-dark-soft flex flex-col items-center justify-center min-h-[140px]"
            onClick={() => setShowNewProjectModal(true)}
            title="Start a new project"
          >
            <div className="new-project-plus text-3xl font-light text-accent dark:text-accent-dark-DEFAULT mb-1 leading-none">+</div>
            <div className="new-project-label text-base font-semibold text-accent dark:text-accent-dark-DEFAULT uppercase tracking-wide">Start New Project</div>
          </div>
        )}
        {d.projects.map((p) => {
          const accent = p.eu_only ? '#003399' : projectColor(p.name);
          const isRunning = p.running_tasks > 0;
          return (
          <div
            key={p.id}
            className={cn(
              'dash-project-card clickable relative overflow-hidden flex flex-col bg-surface-raised dark:bg-surface-dark-raised border border-border-muted dark:border-border-dark-muted rounded-lg p-4 cursor-pointer transition-[box-shadow,border-color] hover:shadow-medium hover:border-border-strong dark:hover:border-border-dark-strong group',
              isRunning && 'is-running'
            )}
            style={{ borderLeft: `4px solid ${accent}`, ...(isRunning ? { '--card-accent': accent } : {}) } as CSSProperties}
            onClick={() => { setActiveProject(p.id); setActiveMainTab(isMobile ? 'dashboard' : 'board'); }}
          >
            {p.eu_only && (
              <div className="absolute inset-0 pointer-events-none opacity-10">
                <EUFlag className="w-full h-full" />
              </div>
            )}
            <div className="relative z-10 flex flex-1 flex-col">
            <div className="dash-project-header flex justify-between items-start mb-2">
              <span className="dash-project-name text-base font-semibold leading-snug flex-1 dark:text-text-dark-DEFAULT">
                {p.name}
                {p.scw_session_enabled && (
                  <span className="ml-1.5 inline-flex items-center gap-1 text-[10px] uppercase tracking-wide text-accent dark:text-accent-dark-DEFAULT px-1 py-px rounded border border-accent/25 dark:border-accent-dark-DEFAULT/40" style={{ background: 'rgba(99,102,241,.12)' }}>
                    <Lock size={11} />
                    EU Session
                  </span>
                )}
                {!!p.active_deployment_cost_usd && p.active_deployment_cost_usd > 0 && (
                  <span className="ml-1.5 inline-flex items-center gap-1 text-[10px] uppercase tracking-wide text-status-failed dark:text-status-failed-dark-DEFAULT px-1 py-px rounded border border-status-failed/25 dark:border-status-failed-dark-DEFAULT/40" style={{ background: 'rgba(248,81,73,.12)' }}>
                    <span className="w-1.5 h-1.5 rounded-full bg-status-failed dark:bg-status-failed-dark-DEFAULT inline-block" />
                    GPU: ${p.active_deployment_cost_usd.toFixed(2)} accrued
                  </span>
                )}
              </span>
              <span className="dash-project-stats text-sm+ text-text-muted dark:text-text-dark-muted whitespace-nowrap ml-2">
                {p.total_tasks} tasks
              </span>
              {(p.current_user_role === 'owner' || p.current_user_role === 'admin') && (
                <button
                  className="dash-project-members ml-1.5 text-text-faint dark:text-text-dark-faint opacity-0 group-hover:opacity-100 hover:text-accent dark:hover:text-accent-dark-DEFAULT cursor-pointer inline-flex items-center p-0.5 rounded transition-opacity"
                  data-tip="Manage project members"
                  onClick={(e) => { e.stopPropagation(); setMembersProjectId(p.id); setShowMembersModal(true); }}
                >
                  <Users size={14} />
                </button>
              )}
              {(perms.authOff || perms.isAdmin || roleAtLeast(p.current_user_role, 'owner')) && (
                <button
                  className="dash-project-delete ml-1.5 text-text-faint dark:text-text-dark-faint opacity-0 group-hover:opacity-100 hover:text-danger dark:hover:text-danger-dark-DEFAULT cursor-pointer inline-flex items-center p-0.5 rounded transition-opacity"
                  data-tip="Delete this project"
                  onClick={(e) => { e.stopPropagation(); setDeleteProjectTarget(p.id); setShowDeleteProjectModal(true); }}
                >
                  <Trash2 size={14} />
                </button>
              )}
            </div>

            <div className="progress-bar-wrap small bg-border-muted dark:bg-border-dark-muted rounded-md h-1.5 my-1.5 overflow-hidden">
              <div
                className={cn('progress-bar h-full rounded-md transition-[width] duration-500', p.budget_exceeded ? 'danger bg-danger' : 'bg-accent dark:bg-accent-dark-DEFAULT')}
                style={{ width: `${Math.min(100, p.progress_pct)}%` }}
              />
            </div>
            <div className="dash-project-meta text-sm+ text-text-soft dark:text-text-dark-soft my-1">
              <span>{Fmt.pct(p.progress_pct)} done</span>
              <span className="sep mx-1.5 text-default">|</span>
              <span className={cn(p.running_tasks && 'accent-blue text-status-running dark:text-status-running-dark-DEFAULT')}>{p.running_tasks} running</span>
              <span className="sep mx-1.5 text-default">|</span>
              <span>{p.pending_tasks} pending</span>
            </div>

            <div className="dash-project-budget text-sm+ text-text-muted dark:text-text-dark-muted my-1 flex gap-1.5">
              <span>Budget: {Fmt.cost(p.current_month_spend)} / {Fmt.cost(p.budget_monthly)}</span>
              <span className={cn(p.budget_exceeded && 'accent-red text-status-failed dark:text-status-failed-dark-DEFAULT')}>({Fmt.pct(p.budget_pct)})</span>
            </div>

            {(p.hf_models_count ?? 0) > 0 && (
              <div className="dash-project-hf text-sm+ text-text-muted dark:text-text-dark-muted my-1 flex gap-1.5">
                <span className="text-accent dark:text-accent-dark-DEFAULT font-semibold">{p.hf_models_count} HF model{(p.hf_models_count ?? 0) === 1 ? '' : 's'}</span>
                {(p.awaiting_model_tasks ?? 0) > 0 && (
                  <span className="text-status-pending dark:text-status-pending-dark-DEFAULT">· {p.awaiting_model_tasks} awaiting self-host</span>
                )}
              </div>
            )}

            <div className="dash-project-cost text-sm+ text-text-muted dark:text-text-dark-muted my-1 pt-1 border-t border-border-subtle dark:border-border-dark-subtle">
              This month: {Fmt.cost(p.current_month_spend)} | This week: {Fmt.cost(p.week_spent)}
            </div>

            {p.recent_executions.length > 0 && (
              <div className="dash-recent-execs border-t border-border-subtle dark:border-border-dark-subtle !mt-auto pt-2.5">
                <div className="dash-sub-label text-xs uppercase text-text-faint dark:text-text-dark-faint my-2.5 mx-0 tracking-wide">Recent</div>
                {p.recent_executions.slice(0, 3).map((e) => (
                  <div key={e.id} className="dash-exec-row flex items-center gap-2 py-0.5 text-sm+">
                    <span className={cn('dash-exec-dot w-1.5 h-1.5 rounded-full shrink-0', `status-${e.status}`)} />
                    <span className="dash-exec-title flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-ink-soft dark:text-text-dark-soft">{e.task_title}</span>
                    <span className="dash-exec-cost text-text-muted dark:text-text-dark-muted font-mono text-xs">{Fmt.cost(e.cost_usd)}</span>
                    <span className="dash-exec-date text-text-faint dark:text-text-dark-faint text-xs">{Fmt.date(e.started_at)}</span>
                  </div>
                ))}
              </div>
            )}
            </div>
            {p.scw_session_enabled && p.scw_session_bucket && p.scw_kms_key_id && (
              <div
                className="absolute bottom-1.5 right-1.5 z-10 pointer-events-none text-amber-400/70"
                title="KMS-encrypted Scaleway session active"
              >
                <LockIcon className="w-5 h-5" />
              </div>
            )}
          </div>
          );
        })}
      </div>
    </div>
  );
};

export default Dashboard;
