import { useEffect, useCallback } from 'react';
import { useStore, type MainTab } from './store';
import { api } from './api';
import { cn } from './utils/cn';
import { useProjectSSE } from './hooks/useProjectSSE';
import { useIsMobile } from './hooks/useIsMobile';
import { MOBILE_BREAKPOINT } from './utils/breakpoints';
import { MobileBottomNav } from './components/MobileBottomNav';
import { Sidebar } from './components/Sidebar';
import { Header } from './components/Header';
import { TaskBoard } from './components/TaskBoard';
import { ExecutionLog } from './components/ExecutionLog';
import { ChatPanel } from './components/ChatPanel';
import { ErrorBoundary } from './components/ErrorBoundary';
import { DependencyGraph } from './components/DependencyGraph';
import { BoardTools } from './components/BoardTools';
import { Dashboard } from './components/Dashboard';
import { MemoryPanel } from './components/MemoryPanel';
import { AIngelPanel } from './components/AIngelPanel';
import { ChatsSidebar as ChatsPanel } from './components/ChatsSidebar';
import { SearchModal } from './components/SearchModal';
import { AttachPicker } from './components/AttachPicker';
import AddTaskModal from './components/AddTaskModal';
import NewProjectModal from './components/NewProjectModal';
import { ScaffoldProgress } from './components/ScaffoldProgress';
import SettingsModal from './components/SettingsModal';
import { GpuWindowModal } from './components/GpuWindowModal';
import { DeleteProjectModal } from './components/DeleteProjectModal';
import { MembersModal } from './components/MembersModal';
import ChatModal from './components/ChatModal';
import DependenciesModal from './components/DependenciesModal';
import BudgetModal from './components/BudgetModal';
import Notifications from './components/Notifications';

const ScaffoldProgressApp = () => {
  const {
    scaffoldPid, setScaffoldPid,
    scaffoldChatId, setScaffoldChatId,
    scaffoldName, setScaffoldName,
    setProjects, setTasks, setChats, setPhasesData,
    setActiveChat, setActiveMainTab, setActiveProject,
  } = useStore();

  if (scaffoldPid == null) return null;

  const cleanup = () => {
    setScaffoldPid(null);
    setScaffoldChatId(null);
    setScaffoldName('');
  };

  return (
    <ScaffoldProgress
      projectId={scaffoldPid}
      chatId={scaffoldChatId ?? undefined}
      projectName={scaffoldName}
      onApply={async () => {
        if (scaffoldChatId) {
          await api.projects.applyGuide(scaffoldPid, scaffoldChatId);
          await api.chats.update(scaffoldChatId, { status: 'archived' });
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
        }
        cleanup();
        setActiveMainTab('board');
      }}
      onDismiss={() => {
        cleanup();
      }}
      onDiscuss={async () => {
        if (scaffoldChatId) {
          try {
            const chat = await api.chats.get(scaffoldChatId);
            setActiveProject(scaffoldPid);
            setActiveChat(chat);
          } catch (e) {
            console.error('Failed to open scaffolding chat', e);
          }
        }
        cleanup();
      }}
    />
  );
};

const App = () => {
  const {
    setProjects, setTasks, setModels, setExecutions, setPhasesData,
    setChats, setRoles, setRoleTemplates, setWorkSessions, setConfig,
    setReadiness, setCostSummary, setDashboardData,
    user, setUser, setQuotas,
    setCostToday, setCostWeek, setCostByModel, setKanbanTokenBudget,
    activeProject, activeChat, activeMainTab,
    settingsDirty, setShowSearch,
    setSidebarCollapsed, setActiveChat,
    addAingelChatId,
  } = useStore();

  const isMobile = useIsMobile();

  const loadAll = useCallback(async () => {
    const justLoggedOut = new URLSearchParams(window.location.search).has('loggedout');

    try {
      const me = await api.auth.me();
      setUser(me.user);
      setQuotas(me.quotas);
    } catch (e: any) {
      if (e?.status === 401) {
        if (justLoggedOut) {
          setUser(null);
          setQuotas(null);
        } else {
          api.auth.login();
          return;
        }
      } else {
        setUser(null);
        setQuotas(null);
      }
    }

    try {
      const [
        projects, tasks, models, executions, phases, chats,
        roles, roleTemplates, sessions, cost, _cfg,
      ] = await Promise.all([
        api.projects.list(),
        api.tasks.list(),
        api.models.list(),
        api.executions.list(100),
        api.phases.list(),
        api.chats.list({ status: 'active' }),
        api.roles.list(),
        api.roles.templates(),
        api.workSessions.status(),
        api.costSummary(),
        (async () => {
          const cfg = await api.config.get();
          if (cfg.claude_pro_token_budget) setKanbanTokenBudget(cfg.claude_pro_token_budget);
          setConfig(cfg);
          // Readiness is admin-only — non-admins get a safe config without
          // `ready`, so skip the separate /api/config/readiness call to
          // avoid 404 console spam every 30s.
          if (cfg.ready) {
            setReadiness(cfg.ready);
          } else if (cfg.instance_name !== undefined) {
            // Admin in off mode or edge case: fetch readiness separately.
            try { const r = await api.config.readiness(); setReadiness(r); }
            catch { /* ignore */ }
          }
          return cfg;
        })(),
      ]);
      setProjects(projects);
      for (const p of projects) {
        if (p.aingel_chat_id) addAingelChatId(p.aingel_chat_id);
      }
      // The active project is persisted across sessions; drop it when the
      // current user has no access (e.g. a viewer whose membership changed, or
      // a different user sharing the browser). Without this the header shows
      // "Project #<id>" and panels 404 for a project they cannot see.
      const persistedActive = useStore.getState().activeProject;
      if (persistedActive != null && !projects.some((p) => p.id === persistedActive)) {
        useStore.getState().setActiveProject(null);
        useStore.getState().setActiveMainTab('dashboard');
      }
      setTasks(tasks);
      setModels(models);
      setExecutions(executions);
      setPhasesData(phases);
      setChats(chats);
      setRoles(roles);
      setRoleTemplates(roleTemplates);
      setWorkSessions(sessions);
      setCostSummary(cost);
      if (cost) {
        setCostToday(cost.today ?? 0);
        setCostWeek(cost.week ?? 0);
        const byModel: Record<string, { totalCost: number; execCount: number }> = {};
        if (cost.by_model && typeof cost.by_model[Symbol.iterator] === 'function') {
          for (const m of cost.by_model) { byModel[m.model] = { totalCost: m.total_cost, execCount: m.exec_count }; }
        }
        setCostByModel(byModel);
      }
    } catch (e: any) {
      if (e?.status === 401) {
        if (new URLSearchParams(window.location.search).has('loggedout')) {
          setUser(null);
          setQuotas(null);
        } else {
          api.auth.login();
          return;
        }
      } else {
        console.error('loadAll failed', e);
      }
    }
  }, []);

  useEffect(() => { loadAll(); }, [loadAll]);

  useEffect(() => {
    const iv = setInterval(loadAll, 30000);
    return () => clearInterval(iv);
  }, [loadAll]);

  useEffect(() => {
    const apply = () => {
      const mobile = window.innerWidth < MOBILE_BREAKPOINT;
      if (mobile) setSidebarCollapsed(true);
    };
    apply();
    window.addEventListener('resize', apply);
    return () => window.removeEventListener('resize', apply);
  }, [setSidebarCollapsed]);

  // SSE subscription for real-time updates (replaces per-component polling)
  useProjectSSE(activeProject);

  useEffect(() => {
    if (activeProject !== null && activeMainTab !== 'dashboard') return;
    const fetchDashboard = () => api.dashboard().then(setDashboardData).catch(() => {});
    fetchDashboard();
    const iv = setInterval(fetchDashboard, 30000);
    return () => clearInterval(iv);
  }, [activeProject, activeMainTab]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        setShowSearch(true);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [setShowSearch]);

  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (settingsDirty) { e.preventDefault(); e.returnValue = ''; }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [settingsDirty]);

  // Bridge Execution Log "Chat" button events to store actions.
  // setActiveChat now closes the other right panels (mutual exclusion).
  useEffect(() => {
    const openHandler = async (e: Event) => {
      const { chatId } = (e as CustomEvent).detail as { chatId: number };
      try {
        const full = await api.chats.get(chatId);
        setActiveChat(full);
      } catch (err) { console.error('open-chat failed', err); }
    };

    const newTaskHandler = async (e: Event) => {
      const { taskId } = (e as CustomEvent).detail as { taskId: number };
      const state = useStore.getState();
      const t = state.tasks.find((x) => x.id === taskId);
      if (!t) return;

      const existing = state.chats.find(
        (c) => c.task_id === t.id && (c.status || 'active') === 'active',
      );
      if (existing) {
        try {
          const full = await api.chats.get(existing.id);
          setActiveChat(full);
        } catch (err) { console.error('open existing task chat failed', err); }
        return;
      }

      const proj = state.projects.find((p) => p.id === t.project_id);
      const model = proj?.aingel_model || undefined;

      try {
        const newChat = await api.chats.create({
          project_id: t.project_id,
          name: (t.title || '').slice(0, 50) || `Task #${t.id}`,
          phase_name: t.phase_name || undefined,
          task_id: t.id,
          ...(model ? { model } : {}),
        });
        const freshChats = await api.chats.list({ status: 'active' });
        setChats(freshChats);
        setActiveChat(newChat);
      } catch (err) { console.error('new-chat-task failed', err); }
    };

    window.addEventListener('aingel:open-chat', openHandler);
    window.addEventListener('aingel:new-chat-task', newTaskHandler);
    return () => {
      window.removeEventListener('aingel:open-chat', openHandler);
      window.removeEventListener('aingel:new-chat-task', newTaskHandler);
    };
  }, [setActiveChat, setChats]);

  const isHome = activeProject === null;
  const isLoggedOut = new URLSearchParams(window.location.search).has('loggedout') && !user;

  if (isLoggedOut) {
    return (
      <div className="flex flex-col items-center justify-center h-screen bg-surface-muted gap-4">
        <h1 className="text-2xl font-bold text-ink dark:text-text-dark-DEFAULT">You've been logged out</h1>
        <p className="text-soft dark:text-text-dark-soft text-sm">Your Cordée and Authentik sessions have been ended.</p>
        <button
          data-tip="Log back in to your account"
          className="btn btn-primary px-6 py-2 rounded-md"
          onClick={() => {
            window.location.href = '/';
          }}
        >
          Log in
        </button>
      </div>
    );
  }

  return (
    <div className="app-container flex flex-col h-screen overflow-hidden">
      <Header />
      <div className="app-body flex flex-1 overflow-hidden">
        {isHome ? (
          <div className="app-main flex-1 flex flex-col overflow-hidden" style={{ marginLeft: 0 }}>
            <div className="main-panel flex-1 overflow-y-auto p-4 bg-surface-muted" style={{ background: 'transparent' }}>
              <Dashboard />
            </div>
          </div>
        ) : (
          <>
            <Sidebar />
            <div className="app-main flex-1 flex flex-col overflow-hidden">
              {!isMobile && <MainTabs />}
              <div className={cn('main-panel flex-1 overflow-y-auto p-4 bg-surface-muted', isMobile && 'pb-24')}>
                {activeMainTab === 'dashboard' && <Dashboard />}
                {activeMainTab === 'board' && <TaskBoard />}
                {activeMainTab === 'graph' && <DependencyGraph />}
              </div>
              <ExecutionLog />
            </div>
            {activeChat && (
              <ErrorBoundary>
                <ChatPanel />
              </ErrorBoundary>
            )}
          </>
        )}
      </div>
      {isMobile && activeProject !== null && <MobileBottomNav />}
      <AIngelPanel />
      <ChatsPanel />
      <MemoryPanel />

      <AddTaskModal />
      <NewProjectModal />
      <ScaffoldProgressApp />
      <SettingsModal />
      <GpuWindowModal />
      <DeleteProjectModal />
      <MembersModal />
      <ChatModal />
      <DependenciesModal />
      <BudgetModal />
      <Notifications />
      <SearchModal />
      <AttachPicker />
    </div>
  );
};

const MainTabs = () => {
  const { activeMainTab, setActiveMainTab } = useStore();
  const tabs: { key: MainTab; label: string }[] = [
    { key: 'dashboard', label: 'Dashboard' },
    { key: 'board', label: 'Project Board' },
    { key: 'graph', label: 'Graph' },
  ];
  return (
    <div className="main-tabs flex bg-ink px-3 h-[34px] items-end shrink-0">
      {tabs.map((t) => (
        <button
          key={t.key}
          data-tip={`Switch to the ${t.label} tab`}
          className={cn('tab-btn px-4 py-1.5 border-0 bg-transparent text-white/50 text-md- cursor-pointer rounded-t-md hover:text-white/80', activeMainTab === t.key && 'active text-white')}
          onClick={() => setActiveMainTab(t.key)}
        >
          {t.label}
        </button>
      ))}
      {activeMainTab === 'board' && <BoardTools variant="tabbar" />}
    </div>
  );
};

export default App;
