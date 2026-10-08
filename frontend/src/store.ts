import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type {
  Project, Task, Model, Execution, Phase, Chat, Role,
  WorkSession, CostSummary, Config, ProviderReadiness, ModelRecommendation,
  User, QuotaStatus,
} from './types';
import { api } from './api';

export type MainTab = 'dashboard' | 'board' | 'graph';
export type SettingsTab = 'general' | 'project' | 'users';
export type MemTab = 'files' | 'defs' | 'artifacts' | 'docs' | 'perms' | 'git' | 'roles' | 'spec';
export type NpFileEntry = { name: string; content: string };

interface Store {
  user: User | null;
  quotas: QuotaStatus | null;
  projects: Project[];
  tasks: Task[];
  models: Model[];
  executions: Execution[];
  phasesData: Phase[];
  chats: Chat[];
  roles: Role[];
  roleTemplates: Role[];
  workSessions: WorkSession[];
  config: Config | null;
  readiness: ProviderReadiness | null;
  costSummary: CostSummary | null;
  dashboardData: unknown;
  aingelChatIds: Set<number>;

  activeProject: number | null;
  activePhase: { projectId: number; projectName: string; phaseIdx: number; phaseName: string } | null;
  activeStep: { projectId: number; phaseIdx: number; stepIdx: number; stepText: string } | null;
  activeStatus: string;
  activeMainTab: MainTab;
  activeChat: Chat | null;
  chatSending: boolean;
  chatSendingId: number | null;
  showChatModal: boolean;
  showAttachPicker: boolean;
  showSearch: boolean;

  expandedProjects: Set<number>;
  expandedPhases: Set<string>;
  expandedTaskGroups: Set<string>;
  doneExpanded: Set<number>;

  selectionMode: boolean;
  selectedTaskId: number | null;
  setSelectedTaskId: (id: number | null) => void;
  selectedTasks: Set<number>;
  showArchived: boolean;

  counselorRecs: Record<number, ModelRecommendation>;
  counselorBusyProject: number | null;
  boardScrollProgrammatic: boolean;

  memPanelOpen: boolean;
  memActiveTab: MemTab;
  aingelPanelOpen: boolean;
  chatsPanelOpen: boolean;
  showBudgetModal: boolean;
  sidebarCollapsed: boolean;
  mobileSidebarOpen: boolean;

  rightPanelWidth: number;

  improvedText: string;
  addImprovedText: string;
  addImprovedApplied: boolean;
  addTaskNudgeShown: boolean;

  depCache: Record<number, { byTask: Record<number, unknown>; rows: unknown[] }>;
  depCacheVersion: Record<number, number>;

  kanbanTokenBudget: number;
  costToday: number;
  costWeek: number;
  costByModel: Record<string, { totalCost: number; execCount: number }>;

  notifOpen: boolean;
  notifItems: unknown[];
  attachOptions: unknown | null;
  attachPicked: unknown[];
  taskAttachPicked: unknown[];
  addTaskAttachPicked: unknown[];
  npFilesPicked: NpFileEntry[];
  npImprovedText: string;

  dagTransform: { x: number; y: number; scale: number } | null;
  miniCardNode: number | null;
  depGraph: unknown | null;
  activeGraphDepsId: number | null;
  showAddModal: boolean;
  modModalTaskId: number | null;
  depsModalTaskId: number | null;
  addTaskPendingDeps: number[];
  addTaskSubmitting: boolean;

  showSettingsModal: boolean;
  showNewProjectModal: boolean;
  showGpuWindowModal: boolean;
  showDeleteProjectModal: boolean;
  deleteProjectTarget: number | null;
  showArchiveProjectModal: boolean;
  archiveProjectTarget: number | null;
  showImportProjectModal: boolean;
  showMembersModal: boolean;
  membersProjectId: number | null;
  settingsTab: SettingsTab;
  settingsDirty: boolean;
  lastCfg: Config | null;
  lastReady: ProviderReadiness | null;

  scaffoldPid: number | null;
  scaffoldChatId: number | null;
  scaffoldName: string;
  setScaffoldPid: (pid: number | null) => void;
  setScaffoldChatId: (chatId: number | null) => void;
  setScaffoldName: (name: string) => void;

  setProjects: (projects: Project[]) => void;
  setTasks: (tasks: Task[] | ((prev: Task[]) => Task[])) => void;
  setModels: (models: Model[]) => void;
  setExecutions: (executions: Execution[]) => void;
  setPhasesData: (phases: Phase[]) => void;
  setChats: (chats: Chat[]) => void;
  setRoles: (roles: Role[]) => void;
  setRoleTemplates: (templates: Role[]) => void;
  setWorkSessions: (sessions: WorkSession[]) => void;
  setUser: (user: User | null) => void;
  setQuotas: (quotas: QuotaStatus | null) => void;
  setConfig: (config: Config) => void;
  setReadiness: (readiness: ProviderReadiness) => void;
  setCostSummary: (s: CostSummary) => void;
  setDashboardData: (d: unknown) => void;
  addAingelChatId: (id: number) => void;

  setActiveProject: (id: number | null) => void;
  setActivePhase: (p: Store['activePhase']) => void;
  setActiveStep: (s: Store['activeStep']) => void;
  setActiveStatus: (status: string) => void;
  setActiveMainTab: (tab: MainTab) => void;
  setActiveChat: (chat: Chat | null) => void;
  setChatSending: (v: boolean) => void;
  setChatSendingId: (id: number | null) => void;
  setShowChatModal: (v: boolean) => void;
  setShowAttachPicker: (v: boolean) => void;
  setShowSearch: (v: boolean) => void;

  toggleExpandedProject: (id: number) => void;
  toggleExpandedPhase: (key: string) => void;
  toggleExpandedTaskGroup: (key: string) => void;
  toggleDoneExpanded: (id: number) => void;

  toggleSelectionMode: () => void;
  exitSelectionMode: () => void;
  toggleTaskSelect: (id: number) => void;
  deselectAll: () => void;
  setShowArchived: (show: boolean) => void;

  setCounselorRecs: (recs: Record<number, ModelRecommendation>) => void;
  setCounselorBusyProject: (id: number | null) => void;
  setBoardScrollProgrammatic: (v: boolean) => void;

  toggleMemPanel: () => void;
  setMemPanelOpen: (v: boolean) => void;
  setMemActiveTab: (tab: MemTab) => void;
  setAingelPanelOpen: (v: boolean) => void;
  setChatsPanelOpen: (v: boolean) => void;
  setShowBudgetModal: (v: boolean) => void;
  toggleSidebar: () => void;
  setSidebarCollapsed: (v: boolean) => void;
  setMobileSidebarOpen: (v: boolean) => void;

  setRightPanelWidth: (w: number) => void;
  openRightPanel: (which: 'aingel' | 'chats' | 'memory' | 'chat' | null) => void;
  toggleRightPanel: (which: 'aingel' | 'chats' | 'memory') => void;

  setImprovedText: (text: string) => void;
  setAddImprovedText: (text: string) => void;
  setAddImprovedApplied: (v: boolean) => void;
  setAddTaskNudgeShown: (v: boolean) => void;

  setDepCache: (projectId: number, data: { byTask: Record<number, unknown>; rows: unknown[] }) => void;
  invalidateDepCache: (projectId: number) => void;

  setKanbanTokenBudget: (v: number) => void;
  setCostToday: (v: number) => void;
  setCostWeek: (v: number) => void;
  setCostByModel: (v: Record<string, { totalCost: number; execCount: number }>) => void;

  setNotifOpen: (v: boolean) => void;
  setNotifItems: (items: unknown[]) => void;
  setAttachOptions: (o: unknown | null) => void;
  setAttachPicked: (p: unknown[]) => void;
  setTaskAttachPicked: (p: unknown[]) => void;
  setAddTaskAttachPicked: (p: unknown[]) => void;
  setNpFilesPicked: (f: NpFileEntry[]) => void;
  setNpImprovedText: (t: string) => void;

  setDagTransform: (t: { x: number; y: number; scale: number } | null) => void;
  setMiniCardNode: (id: number | null) => void;
  setDepGraph: (g: unknown | null) => void;
  setActiveGraphDepsId: (id: number | null) => void;
  setShowAddModal: (v: boolean) => void;
  setModModalTaskId: (id: number | null) => void;
  setDepsModalTaskId: (id: number | null) => void;
  setAddTaskPendingDeps: (ids: number[]) => void;
  setAddTaskSubmitting: (v: boolean) => void;

  setShowSettingsModal: (v: boolean) => void;
  setShowNewProjectModal: (v: boolean) => void;
  setShowGpuWindowModal: (v: boolean) => void;
  setShowDeleteProjectModal: (v: boolean) => void;
  setDeleteProjectTarget: (id: number | null) => void;
  setShowArchiveProjectModal: (v: boolean) => void;
  setArchiveProjectTarget: (id: number | null) => void;
  setShowImportProjectModal: (v: boolean) => void;
  setShowMembersModal: (v: boolean) => void;
  setMembersProjectId: (id: number | null) => void;
  setSettingsTab: (tab: SettingsTab) => void;
  setSettingsDirty: (v: boolean) => void;
  setLastCfg: (c: Config | null) => void;
  setLastReady: (r: ProviderReadiness | null) => void;

  handleSSEEvent: (evt: { type: string; task_id?: number; exec_id?: number; chat_id?: number; slot?: number; error?: string; quota?: string; limit?: number }) => void;
  _lastChangedChatId: number | null;
  // Last background run failure (e.g. free-tier quota block). Transient —
  // consumed by the board to toast the error and clear Running indicators.
  runError: { msg: string; slot: number | null; at: number } | null;
  clearRunError: () => void;
}

// Quota refresh throttle. Execution SSE events can arrive in bursts (a task
// run emits several), so refetch /api/me at most once per interval.
const QUOTA_REFRESH_MS = 4000;
let _lastQuotaRefresh = 0;

/**
 * Refresh the user row and quota counters from /api/me. Unthrottled — call
 * after a mutation that changes account state (project import or removal) so
 * the Dashboard create/import cards and the QuotaBar update immediately.
 * Swallows errors: the 30s loadAll poll is the backstop.
 */
export function refreshQuotas(): Promise<void> {
  _lastQuotaRefresh = Date.now();
  return api.auth.me()
    .then((me) => {
      const store = useStore.getState();
      store.setUser(me.user);
      store.setQuotas(me.quotas);
    })
    .catch(() => { /* transient; the 30s loadAll poll catches up */ });
}

function _refreshQuotasThrottled() {
  const now = Date.now();
  if (now - _lastQuotaRefresh < QUOTA_REFRESH_MS) return;
  refreshQuotas();
}

export const useStore = create<Store>()(
  persist(
    (set, get) => ({
      user: null,
      quotas: null,
      projects: [],
      tasks: [],
      models: [],
      executions: [],
      phasesData: [],
      chats: [],
      roles: [],
      roleTemplates: [],
      workSessions: [],
      config: null,
      readiness: null,
      costSummary: null,
      dashboardData: null,
      aingelChatIds: new Set(),

      activeProject: null,
      activePhase: null,
      activeStep: null,
      activeStatus: 'all',
      activeMainTab: 'board',
      activeChat: null,
      chatSending: false,
      chatSendingId: null,
      showChatModal: false,
      showAttachPicker: false,
      showSearch: false,

      expandedProjects: new Set(),
      expandedPhases: new Set(),
      expandedTaskGroups: new Set(),
      doneExpanded: new Set(),

      selectionMode: false,
      selectedTasks: new Set(),
      selectedTaskId: null,
      showArchived: false,

      counselorRecs: {},
      counselorBusyProject: null,
      boardScrollProgrammatic: false,

      memPanelOpen: false,
      memActiveTab: 'defs',
      aingelPanelOpen: false,
      chatsPanelOpen: false,
      showBudgetModal: false,
      sidebarCollapsed: false,
      mobileSidebarOpen: false,

      rightPanelWidth: 400,

      improvedText: '',
      addImprovedText: '',
      addImprovedApplied: false,
      addTaskNudgeShown: false,

      depCache: {},
      depCacheVersion: {},

      kanbanTokenBudget: 150000,
      costToday: 0,
      costWeek: 0,
      costByModel: {},

      notifOpen: false,
      notifItems: [],
      attachOptions: null,
      attachPicked: [],
      taskAttachPicked: [],
      addTaskAttachPicked: [],
      npFilesPicked: [],
      npImprovedText: '',

      dagTransform: null,
      miniCardNode: null,
      depGraph: null,
      activeGraphDepsId: null,
      showAddModal: false,
      modModalTaskId: null,
      depsModalTaskId: null,
      addTaskPendingDeps: [],
      addTaskSubmitting: false,

      showSettingsModal: false,
      showNewProjectModal: false,
      showGpuWindowModal: false,
      showDeleteProjectModal: false,
      deleteProjectTarget: null,
      showArchiveProjectModal: false,
      archiveProjectTarget: null,
      showImportProjectModal: false,
      showMembersModal: false,
      membersProjectId: null,
      settingsTab: 'general',
      settingsDirty: false,
      lastCfg: null,
      lastReady: null,
      _lastChangedChatId: null,
      runError: null,
      scaffoldPid: null,
      scaffoldChatId: null,
      scaffoldName: '',

      setProjects: (projects) => set({ projects }),
      setTasks: (tasks) => set((s) => ({ tasks: typeof tasks === 'function' ? tasks(s.tasks) : tasks })),
      setModels: (models) => set({ models }),
      setExecutions: (executions) => set({ executions }),
      setPhasesData: (phasesData) => set({ phasesData }),
      setChats: (chats) => set({ chats }),
      setRoles: (roles) => set({ roles }),
      setRoleTemplates: (roleTemplates) => set({ roleTemplates }),
      setWorkSessions: (workSessions) => set({ workSessions }),
      setUser: (user) => set({ user }),
      setQuotas: (quotas) => set({ quotas }),
      setConfig: (config) => set({ config }),
      setReadiness: (readiness) => set({ readiness }),
      setCostSummary: (costSummary) => set({ costSummary }),
      setDashboardData: (dashboardData) => set({ dashboardData }),
      addAingelChatId: (id) => set((s) => {
        const next = new Set(s.aingelChatIds);
        next.add(id);
        return { aingelChatIds: next };
      }),

      setActiveProject: (activeProject) => set({ activeProject }),
      setActivePhase: (activePhase) => set({ activePhase }),
      setActiveStep: (activeStep) => set({ activeStep }),
      setActiveStatus: (activeStatus) => set({ activeStatus }),
      setActiveMainTab: (activeMainTab) => set({ activeMainTab }),
      setActiveChat: (activeChat) =>
        set((s) => ({
          activeChat,
          aingelPanelOpen: activeChat ? false : s.aingelPanelOpen,
          chatsPanelOpen: activeChat ? false : s.chatsPanelOpen,
          memPanelOpen: activeChat ? false : s.memPanelOpen,
        })),
      setChatSending: (chatSending) => set({ chatSending }),
      setChatSendingId: (chatSendingId) => set({ chatSendingId }),
      setShowChatModal: (showChatModal) => set({ showChatModal }),
      setShowAttachPicker: (showAttachPicker) => set({ showAttachPicker }),
      setShowSearch: (showSearch) => set({ showSearch }),

      toggleExpandedProject: (id) => set((s) => {
        const next = new Set(s.expandedProjects);
        next.has(id) ? next.delete(id) : next.add(id);
        return { expandedProjects: next };
      }),
      toggleExpandedPhase: (key) => set((s) => {
        const next = new Set(s.expandedPhases);
        if (next.has(key)) {
          next.delete(key);
        } else {
          // Close other phases from the same project (single-open accordion)
          const projectPrefix = key.split('::')[0] + '::';
          for (const existing of next) {
            if (existing.startsWith(projectPrefix)) next.delete(existing);
          }
          next.add(key);
        }
        return { expandedPhases: next };
      }),
      toggleExpandedTaskGroup: (key) => set((s) => {
        const next = new Set(s.expandedTaskGroups);
        next.has(key) ? next.delete(key) : next.add(key);
        return { expandedTaskGroups: next };
      }),
      toggleDoneExpanded: (id) => set((s) => {
        const next = new Set(s.doneExpanded);
        next.has(id) ? next.delete(id) : next.add(id);
        return { doneExpanded: next };
      }),

      toggleSelectionMode: () => set((s) => {
        if (s.selectionMode) return { selectionMode: false, selectedTasks: new Set() };
        return { selectionMode: true };
      }),
      exitSelectionMode: () => set({ selectionMode: false, selectedTasks: new Set() }),
      toggleTaskSelect: (id) => set((s) => {
        const next = new Set(s.selectedTasks);
        next.has(id) ? next.delete(id) : next.add(id);
        return { selectedTasks: next };
      }),
      deselectAll: () => set({ selectedTasks: new Set() }),
      setSelectedTaskId: (selectedTaskId) => set({ selectedTaskId }),
      setShowArchived: (showArchived) => set({ showArchived }),

      setCounselorRecs: (counselorRecs) => set({ counselorRecs }),
      setCounselorBusyProject: (counselorBusyProject) => set({ counselorBusyProject }),
      setBoardScrollProgrammatic: (boardScrollProgrammatic) => set({ boardScrollProgrammatic }),

      toggleMemPanel: () => set((s) => ({ memPanelOpen: !s.memPanelOpen })),
      setMemPanelOpen: (memPanelOpen) =>
        set((s) => ({
          memPanelOpen,
          aingelPanelOpen: memPanelOpen ? false : s.aingelPanelOpen,
          chatsPanelOpen: memPanelOpen ? false : s.chatsPanelOpen,
          activeChat: memPanelOpen ? null : s.activeChat,
        })),
      setMemActiveTab: (memActiveTab) => set({ memActiveTab }),
      setAingelPanelOpen: (aingelPanelOpen) =>
        set((s) => ({
          aingelPanelOpen,
          memPanelOpen: aingelPanelOpen ? false : s.memPanelOpen,
          chatsPanelOpen: aingelPanelOpen ? false : s.chatsPanelOpen,
          activeChat: aingelPanelOpen ? null : s.activeChat,
        })),
      setChatsPanelOpen: (chatsPanelOpen) =>
        set((s) => ({
          chatsPanelOpen,
          aingelPanelOpen: chatsPanelOpen ? false : s.aingelPanelOpen,
          memPanelOpen: chatsPanelOpen ? false : s.memPanelOpen,
          activeChat: chatsPanelOpen ? null : s.activeChat,
        })),
      setShowBudgetModal: (showBudgetModal) => set({ showBudgetModal }),
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),
      setMobileSidebarOpen: (mobileSidebarOpen) => set({ mobileSidebarOpen }),

      setRightPanelWidth: (rightPanelWidth) => set({ rightPanelWidth }),
      openRightPanel: (which) =>
        set((s) => {
          if (which === 'aingel') {
            return { aingelPanelOpen: true, chatsPanelOpen: false, memPanelOpen: false, activeChat: null };
          }
          if (which === 'chats') {
            return { chatsPanelOpen: true, aingelPanelOpen: false, memPanelOpen: false, activeChat: null };
          }
          if (which === 'memory') {
            return { memPanelOpen: true, aingelPanelOpen: false, chatsPanelOpen: false, activeChat: null };
          }
          if (which === 'chat') {
            return { activeChat: s.activeChat, aingelPanelOpen: false, chatsPanelOpen: false, memPanelOpen: false };
          }
          return { aingelPanelOpen: false, chatsPanelOpen: false, memPanelOpen: false, activeChat: null };
        }),
      toggleRightPanel: (which) =>
        set((s) => {
          const isOpen =
            (which === 'aingel' && s.aingelPanelOpen) ||
            (which === 'chats' && s.chatsPanelOpen) ||
            (which === 'memory' && s.memPanelOpen);
          if (isOpen) {
            return { aingelPanelOpen: false, chatsPanelOpen: false, memPanelOpen: false, activeChat: null };
          }
          if (which === 'aingel') return { aingelPanelOpen: true, chatsPanelOpen: false, memPanelOpen: false, activeChat: null };
          if (which === 'chats') return { chatsPanelOpen: true, aingelPanelOpen: false, memPanelOpen: false, activeChat: null };
          return { memPanelOpen: true, aingelPanelOpen: false, chatsPanelOpen: false, activeChat: null };
        }),

      setImprovedText: (improvedText) => set({ improvedText }),
      setAddImprovedText: (addImprovedText) => set({ addImprovedText }),
      setAddImprovedApplied: (addImprovedApplied) => set({ addImprovedApplied }),
      setAddTaskNudgeShown: (addTaskNudgeShown) => set({ addTaskNudgeShown }),

      setDepCache: (projectId, data) => set((s) => ({
        depCache: { ...s.depCache, [projectId]: data },
        depCacheVersion: { ...s.depCacheVersion, [projectId]: (s.depCacheVersion[projectId] || 0) + 1 },
      })),
      invalidateDepCache: (projectId) => set((s) => ({
        depCacheVersion: { ...s.depCacheVersion, [projectId]: (s.depCacheVersion[projectId] || 0) + 1 },
      })),

      setKanbanTokenBudget: (kanbanTokenBudget) => set({ kanbanTokenBudget }),
      setCostToday: (costToday) => set({ costToday }),
      setCostWeek: (costWeek) => set({ costWeek }),
      setCostByModel: (costByModel) => set({ costByModel }),

      setNotifOpen: (notifOpen) => set({ notifOpen }),
      setNotifItems: (notifItems) => set({ notifItems }),
      setAttachOptions: (attachOptions) => set({ attachOptions }),
      setAttachPicked: (attachPicked) => set({ attachPicked }),
      setTaskAttachPicked: (taskAttachPicked) => set({ taskAttachPicked }),
      setAddTaskAttachPicked: (addTaskAttachPicked) => set({ addTaskAttachPicked }),
      setNpFilesPicked: (npFilesPicked) => set({ npFilesPicked }),
      setNpImprovedText: (npImprovedText) => set({ npImprovedText }),

      setDagTransform: (dagTransform) => set({ dagTransform }),
      setMiniCardNode: (miniCardNode) => set({ miniCardNode }),
      setDepGraph: (depGraph) => set({ depGraph }),
      setActiveGraphDepsId: (activeGraphDepsId) => set({ activeGraphDepsId }),
      setShowAddModal: (showAddModal) => set({ showAddModal }),
      setModModalTaskId: (modModalTaskId) => set({ modModalTaskId }),
      setDepsModalTaskId: (depsModalTaskId) => set({ depsModalTaskId }),
      setAddTaskPendingDeps: (addTaskPendingDeps) => set({ addTaskPendingDeps }),
      setAddTaskSubmitting: (addTaskSubmitting) => set({ addTaskSubmitting }),

      setShowSettingsModal: (showSettingsModal) => set({ showSettingsModal }),
      setShowNewProjectModal: (showNewProjectModal) => set({ showNewProjectModal }),
      setShowGpuWindowModal: (showGpuWindowModal) => set({ showGpuWindowModal }),
      setShowDeleteProjectModal: (showDeleteProjectModal) => set({ showDeleteProjectModal }),
      setDeleteProjectTarget: (deleteProjectTarget) => set({ deleteProjectTarget }),
      setShowArchiveProjectModal: (showArchiveProjectModal) => set({ showArchiveProjectModal }),
      setArchiveProjectTarget: (archiveProjectTarget) => set({ archiveProjectTarget }),
      setShowImportProjectModal: (showImportProjectModal) => set({ showImportProjectModal }),
      setShowMembersModal: (showMembersModal) => set({ showMembersModal }),
      setMembersProjectId: (membersProjectId) => set({ membersProjectId }),
      setSettingsTab: (settingsTab) => set({ settingsTab }),
      setSettingsDirty: (settingsDirty) => set({ settingsDirty }),
      setLastCfg: (lastCfg) => set({ lastCfg }),
      setLastReady: (lastReady) => set({ lastReady }),
      clearRunError: () => set({ runError: null }),
      setScaffoldPid: (scaffoldPid) => set({ scaffoldPid }),
      setScaffoldChatId: (scaffoldChatId) => set({ scaffoldChatId }),
      setScaffoldName: (scaffoldName) => set({ scaffoldName }),

      // SSE event handler with 200ms trailing debounce to batch bursts
      handleSSEEvent: (evt) => {
        // Simple non-debounced version for now - can add debounce later if needed
        const refetch = async () => {
          switch (evt.type) {
            case 'task_changed':
              set({ tasks: await api.tasks.list() });
              break;
            case 'execution_changed':
              set({ executions: await api.executions.list(100) });
              // Quota counters (runs/tokens) change with executions. Runs are
              // incremented pre-flight but tokens only after the provider
              // returns, so without this the QuotaBar lagged until the 30s
              // loadAll poll. Throttled so event bursts don't spam /api/me.
              _refreshQuotasThrottled();
              break;
            case 'chat_changed': {
              const chats = await api.chats.list({ status: 'active' });
              set({ chats, _lastChangedChatId: evt.chat_id ?? null });
              // Live-refresh the open transcript (e.g. a task-completion
              // post landing in the Guide chat), but never while a
              // user-initiated reply is in flight — ChatPanel's send flow
              // owns the transcript then and refetches on completion.
              // The transcript sentinel closes a straddle race: a stalled
              // fetch started while idle must not overwrite a send that
              // completed while it was in flight.
              const cid = evt.chat_id;
              if (typeof cid === 'number' && !get().chatSending && get().activeChat?.id === cid) {
                const before = get().activeChat?.transcript;
                try {
                  const fresh = await api.chats.get(cid);
                  if (get().activeChat?.id === cid && !get().chatSending && get().activeChat?.transcript === before) {
                    set({ activeChat: fresh });
                  }
                } catch { /* open chat deleted or transient failure — ignore */ }
              }
              break;
            }
            case 'work_session_changed':
              set({ workSessions: await api.workSessions.status() });
              break;
            case 'run_failed':
              // Background run-thread failure (e.g. free-tier quota block):
              // the kick returned ok:true so the board shows Running — toast
              // the reason and refresh quotas so the QuotaBar catches up.
              set({ runError: { msg: String(evt.error || 'Run blocked'), slot: typeof evt.slot === 'number' ? evt.slot : null, at: Date.now() } });
              _refreshQuotasThrottled();
              break;
            case 'cost_changed': {
              const c = await api.costSummary();
              const byModel: Record<string, { totalCost: number; execCount: number }> = {};
              if (c.by_model && typeof c.by_model[Symbol.iterator] === 'function') {
                for (const m of c.by_model) {
                  byModel[m.model] = { totalCost: m.total_cost, execCount: m.exec_count };
                }
              }
              set({ costSummary: c, costToday: c.today ?? 0, costWeek: c.week ?? 0, costByModel: byModel });
              break;
            }
          }
        };
        refetch();
      },
    }),
    {
      name: 'superagent_store',
      partialize: (state) => ({
        activeProject: state.activeProject,
        activeMainTab: state.activeMainTab,
        expandedProjects: Array.from(state.expandedProjects),
        expandedPhases: Array.from(state.expandedPhases),
        expandedTaskGroups: Array.from(state.expandedTaskGroups),
        kanbanTokenBudget: state.kanbanTokenBudget,
        sidebarCollapsed: state.sidebarCollapsed,
        rightPanelWidth: state.rightPanelWidth,
      }),
      merge: (persisted, current) => {
        const p = (persisted as Record<string, unknown>) || {};
        const validTabs: MainTab[] = ['dashboard', 'board', 'graph'];
        const activeMainTab = validTabs.includes(p.activeMainTab as MainTab)
          ? (p.activeMainTab as MainTab)
          : current.activeMainTab;
        return {
          ...current,
          ...p,
          activeMainTab,
          expandedProjects: Array.isArray(p.expandedProjects)
            ? new Set(p.expandedProjects as number[])
            : current.expandedProjects,
          expandedPhases: Array.isArray(p.expandedPhases)
            ? new Set(p.expandedPhases as string[])
            : current.expandedPhases,
          expandedTaskGroups: Array.isArray(p.expandedTaskGroups)
            ? new Set(p.expandedTaskGroups as string[])
            : current.expandedTaskGroups,
        };
      },
    }
  )
);
