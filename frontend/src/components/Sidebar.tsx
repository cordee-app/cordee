import { useStore } from '../store';
import { cn } from '../utils/cn';
import { useIsMobile } from '../hooks/useIsMobile';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import { MobileDrawer } from './MobileDrawer';
import { ChevronDown, ChevronRight, X, Plus } from 'lucide-react';

function projectColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
    hash = hash & hash;
  }
  const h = Math.abs(hash) % 360;
  return `hsl(${h}, 55%, 40%)`;
}

export const Sidebar = () => {
  const {
    projects, activeProject,
    expandedPhases, toggleExpandedPhase,
    tasks, showAddModal, setShowAddModal,
    sidebarCollapsed,
    setActivePhase, activePhase,
    setSelectedTaskId, selectedTaskId,
    setActiveMainTab,
    mobileSidebarOpen, setMobileSidebarOpen,
  } = useStore();

  const isMobile = useIsMobile();
  const perms = useProjectPermissions(activeProject);

  if (activeProject === null) return null;

  const project = projects.find(p => p.id === activeProject);
  if (!project) return null;

  const color = projectColor(project.name);

  const projectTasks = tasks.filter(t => t.project_id === activeProject);
  const norm = (s: string) => (s === 'cancelled' || s === 'skip') ? 'pending' : s;
  const running = projectTasks.filter(t => t.status === 'running').length;
  const pending = projectTasks.filter(t => norm(t.status) === 'pending' || t.status === 'confirmed').length;
  const done = projectTasks.filter(t => t.status === 'done').length;
  const failed = projectTasks.filter(t => t.status === 'failed').length;

  const STATUS_SORT: Record<string, number> = {
    running: 0, confirmed: 1, pending: 2, failed: 3, done: 4,
  };

  const STATUS_GROUPS: { key: string; label: string }[] = [
    { key: 'running',   label: 'Running'   },
    { key: 'confirmed', label: 'Confirmed' },
    { key: 'pending',   label: 'Pending'   },
    { key: 'failed',    label: 'Failed'    },
    { key: 'done',      label: 'Done'      },
  ];

  // Group tasks by phase, then sort numerically (Phase 1, Phase 2, ... Phase 10)
  const phases = new Map<string, typeof projectTasks>();
  for (const t of projectTasks) {
    const key = t.phase_name || 'Notebook';
    if (!phases.has(key)) phases.set(key, []);
    phases.get(key)!.push(t);
  }
  // Sort tasks within each phase by status priority, then by id
  for (const [, tasks] of phases) {
    tasks.sort((a, b) => (STATUS_SORT[norm(a.status)] ?? 9) - (STATUS_SORT[norm(b.status)] ?? 9) || a.id - b.id);
  }
  const sortedPhases = Array.from(phases.entries()).sort(([a], [b]) => {
    if (a === 'Notebook') return 1;
    if (b === 'Notebook') return -1;
    const numA = parseInt(a.match(/\d+/)?.[0] ?? '0', 10);
    const numB = parseInt(b.match(/\d+/)?.[0] ?? '0', 10);
    if (numA !== numB) return numA - numB;
    return a.localeCompare(b);
  });

  if (sidebarCollapsed && !isMobile) {
    return (
      <aside
        className="sidebar sidebar-collapsed bg-surface-panel dark:bg-surface-dark-panel border-r border-default dark:border-border-dark-default overflow-y-auto shrink-0 py-2 flex flex-col items-center"
        style={{ ['--project-color' as any]: color }}
      >
        <div
          className="sidebar-project-marker w-full py-3 px-1 flex items-center justify-center"
          style={{ borderLeft: `4px solid ${color}`, paddingLeft: 4 }}
          title={project.name}
        >
          <span className="text-base font-bold text-ink dark:text-text-dark-default">{project.name.slice(0, 1).toUpperCase()}</span>
        </div>

        {perms.canEdit && (
          <button
            data-tip="Add a new task"
            className="sidebar-add-task-btn-collapsed block mx-auto my-1 w-6 h-6 border-0 rounded-md bg-accent dark:bg-accent-dark-DEFAULT text-white text-md- cursor-pointer font-semibold flex items-center justify-center"
            onClick={() => setShowAddModal(!showAddModal)}
          >
            <Plus size={14} />
          </button>
        )}

        <div className="sidebar-phases-collapsed w-full flex flex-col items-center">
          {sortedPhases.map(([phaseName, phaseTasks]) => {
            const phaseKey = `${activeProject}::${phaseName}`;
            const isExpanded = expandedPhases.has(phaseKey);
            const phaseNum = phaseName.match(/\d+/)?.[0] ?? '';
            const phaseRunning = phaseTasks.filter(t => t.status === 'running').length;
            const phasePending = phaseTasks.filter(t => norm(t.status) === 'pending' || t.status === 'confirmed').length;
            const phaseDone = phaseTasks.filter(t => t.status === 'done').length;
            const counts: string[] = [];
            if (phaseRunning > 0) counts.push(`${phaseRunning}r`);
            if (phasePending > 0) counts.push(`${phasePending}p`);
            if (phaseDone > 0) counts.push(`${phaseDone}d`);
            const tooltip = `${phaseName}${counts.length ? ' — ' + counts.join(' ') : ''}`;
            const isActive = activePhase?.phaseName === phaseName;
            return (
              <div
                key={phaseKey}
                className={cn(
                  'sidebar-phase-mini w-full py-1.5 flex items-center justify-center cursor-pointer text-xs hover:bg-accent-soft dark:hover:bg-accent-dark-soft',
                  isExpanded && 'bg-accent-soft dark:bg-accent-dark-soft'
                )}
                title={tooltip}
                onClick={() => {
                  if (isActive) {
                    if (isExpanded) toggleExpandedPhase(phaseKey);
                    setActivePhase(null);
                  } else {
                    if (!isExpanded) toggleExpandedPhase(phaseKey);
                    setActivePhase({ projectId: activeProject!, projectName: project.name, phaseIdx: parseInt(phaseName.match(/\d+/)?.[0] ?? '0', 10) - 1, phaseName });
                  }
                }}
              >
                {isActive ? (
                  <button
                    data-tip="Clear the phase filter"
                    className="sidebar-unselect sidebar-unselect-mini"
                    onClick={(e) => { e.stopPropagation(); setActivePhase(null); toggleExpandedPhase(phaseKey); }}
                  >
                    <X size={8} />
                  </button>
                ) : (
                  <span className="toggle-icon inline-flex items-center w-3.5 text-[#645c50] dark:text-text-dark-muted mr-0.5">{isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>
                )}
                <span className="phase-num-mini font-medium text-ink dark:text-text-dark-default">{phaseNum ? `>${phaseNum}` : '·'}</span>
              </div>
            );
          })}
        </div>
      </aside>
    );
  }

  const sidebarContent = (
    <aside className="sidebar w-[260px] bg-surface-panel dark:bg-surface-dark-panel border-r border-default dark:border-border-dark-default overflow-y-auto shrink-0 py-2" style={{ ['--project-color' as any]: color }}>
      <div className="sidebar-project-header py-3 px-3 pb-2" style={{ borderLeft: `4px solid ${color}`, paddingLeft: 8 }}>
        <div className="sidebar-project-badge text-sm+ text-muted dark:text-text-dark-muted mt-0">{project.project_type || 'Project'}</div>
      </div>

      <div className="sidebar-stats flex gap-1 px-3 pb-2 flex-wrap">
        <span className="stat-chip running text-sm+ px-1.5 py-0.5 rounded-sm bg-status-running-bg dark:bg-status-running-dark-bg text-status-running dark:text-status-running-dark-DEFAULT">{running} running</span>
        <span className="stat-chip pending text-sm+ px-1.5 py-0.5 rounded-sm bg-status-pending-bg dark:bg-status-pending-dark-bg text-status-pending dark:text-status-pending-dark-DEFAULT">{pending} pending</span>
        <span className="stat-chip done text-sm+ px-1.5 py-0.5 rounded-sm bg-status-done-bg dark:bg-status-done-dark-bg text-status-done dark:text-status-done-dark-DEFAULT">{done} done</span>
        {failed > 0 && <span className="stat-chip failed text-sm+ px-1.5 py-0.5 rounded-sm bg-status-failed-bg dark:bg-status-failed-dark-bg text-status-failed dark:text-status-failed-dark-DEFAULT">{failed} failed</span>}
      </div>

      {perms.canEdit && (
        <button
          data-tip="Add a new task"
          className="sidebar-add-task-btn block w-[calc(100%-24px)] mx-3 mb-2.5 py-2 border-0 rounded-md bg-accent dark:bg-accent-dark-DEFAULT text-white text-md- cursor-pointer font-semibold gap-1.5"
          onClick={() => setShowAddModal(!showAddModal)}
        >
          <Plus size={14} /> Add Task
        </button>
      )}

      <div className="sidebar-phases p-0">
        {sortedPhases.map(([phaseName, phaseTasks]) => {
          const phaseKey = `${activeProject}::${phaseName}`;
          const isExpanded = expandedPhases.has(phaseKey);
          const phaseRunning = phaseTasks.filter(t => t.status === 'running').length;
          const phasePending = phaseTasks.filter(t => norm(t.status) === 'pending' || t.status === 'confirmed').length;
          const phaseDone = phaseTasks.filter(t => t.status === 'done').length;
          const isActive = activePhase?.phaseName === phaseName;

  return (
            <div key={phaseKey} className="sidebar-phase border-b border-muted dark:border-border-dark-muted">
              <div
                className={cn('sidebar-phase-header flex items-center px-3 py-[7px] cursor-pointer gap-1.5 text-md- font-medium hover:bg-accent-soft dark:hover:bg-accent-dark-soft', isExpanded && 'expanded')}
                    onClick={() => {
                      if (isActive) {
                        if (isExpanded) toggleExpandedPhase(phaseKey);
                        setActivePhase(null);
                      } else {
                        if (!isExpanded) toggleExpandedPhase(phaseKey);
                        setActivePhase({ projectId: activeProject!, projectName: project.name, phaseIdx: parseInt(phaseName.match(/\d+/)?.[0] ?? '0', 10) - 1, phaseName });
                      }
                    }}
              >
                {isActive ? (
                  <button
                    data-tip="Clear the phase filter"
                    className="sidebar-unselect"
                    onClick={(e) => { e.stopPropagation(); setActivePhase(null); toggleExpandedPhase(phaseKey); }}
                  >
                    <X size={10} />
                  </button>
                ) : (
                  <span
                    className="toggle-icon inline-flex items-center text-xs w-3.5 text-[#645c50] dark:text-text-dark-muted"
                    onClick={(e) => { e.stopPropagation(); toggleExpandedPhase(phaseKey); }}
                  >
                    {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  </span>
                )}
                <span className="phase-name flex-1">{phaseName}</span>
                <span className="phase-counts flex gap-[3px] text-xs">
                  {phaseRunning > 0 && <span className="pc r px-1 py-px rounded-sm bg-status-running-bg dark:bg-status-running-dark-bg text-status-running dark:text-status-running-dark-DEFAULT">{phaseRunning}</span>}
                  {phasePending > 0 && <span className="pc p px-1 py-px rounded-sm bg-status-pending-bg dark:bg-status-pending-dark-bg text-status-pending dark:text-status-pending-dark-DEFAULT">{phasePending}</span>}
                  {phaseDone > 0 && <span className="pc d px-1 py-px rounded-sm bg-status-done-bg dark:bg-status-done-dark-bg text-status-done dark:text-status-done-dark-DEFAULT">{phaseDone}</span>}
                </span>
              </div>
              {isExpanded && (
                <div className="sidebar-phase-tasks py-0.5 pl-[18px] pb-1.5">
                  {STATUS_GROUPS.map(group => {
                    const groupTasks = phaseTasks.filter(t =>
                      group.key === 'pending'
                        ? (norm(t.status) === 'pending')
                        : t.status === group.key
                    );
                    if (groupTasks.length === 0) return null;
                    return (
                      <div key={group.key} className="sidebar-status-group mt-1">
                        <div className="sidebar-status-label text-2xs uppercase tracking-wide text-faint dark:text-text-dark-faint px-1.5 pt-1 pb-0.5 font-semibold">{group.label} ({groupTasks.length})</div>
                        {groupTasks.map(t => (
                          <div
                            key={t.id}
                            className={cn('sidebar-task-item flex items-center gap-1.5 px-1.5 py-1 text-sm cursor-pointer rounded-sm hover:bg-accent-soft dark:hover:bg-accent-dark-soft text-text-default dark:text-text-dark-default', `status-${norm(t.status)}`, selectedTaskId === t.id && 'bg-accent-soft dark:bg-accent-dark-soft')}
                            title={`#${t.id}: ${t.title}`}
                            onClick={() => {
                              setSelectedTaskId(selectedTaskId === t.id ? null : t.id);
                              setActiveMainTab('board');
                            }}
                          >
                            {selectedTaskId === t.id ? (
                              <button
                                data-tip="Clear the task filter"
                                className="sidebar-unselect"
                                onClick={(e) => { e.stopPropagation(); setSelectedTaskId(null); }}
                              >
                                <X size={10} />
                              </button>
                            ) : (
                              <span className={cn('status-dot w-1.5 h-1.5 rounded-full shrink-0', `dot-${norm(t.status)}`)} />
                            )}
                            <span className="task-text overflow-hidden text-ellipsis whitespace-nowrap">{t.title}</span>
                          </div>
                        ))}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {projectTasks.length === 0 && (
        <div className="sidebar-empty px-4 py-4 text-sm text-muted dark:text-text-dark-muted text-center">No tasks yet. Click "+ Add Task" above to create one.</div>
      )}


    </aside>
  );

  if (isMobile) {
    return (
      <MobileDrawer open={mobileSidebarOpen} onClose={() => setMobileSidebarOpen(false)}>
        <div className="h-full overflow-y-auto">{sidebarContent}</div>
      </MobileDrawer>
    );
  }

  return sidebarContent;
};

export default Sidebar;