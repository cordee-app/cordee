import { useState, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { cn } from '../utils/cn';
import { RightPanelResizer } from './RightPanelResizer';
import { useIsMobile } from '../hooks/useIsMobile';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import { Sparkles, ChevronDown, ChevronRight, LoaderCircle, ClipboardList } from 'lucide-react';

export const AIngelPanel = () => {
  const {
    aingelPanelOpen, setAingelPanelOpen,
    activeProject,
    projects, chats, setActiveChat, addAingelChatId, setChats, setProjects,
    rightPanelWidth,
  } = useStore();

  const isMobile = useIsMobile();
  const perms = useProjectPermissions(activeProject);

  const [overviewMarkdown, setOverviewMarkdown] = useState('');
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [overviewError, setOverviewError] = useState('');
  const [chatIniting, setChatIniting] = useState(false);
  const [chatInitError, setChatInitError] = useState('');

  const [overviewExpanded, setOverviewExpanded] = useState(true);

  const handleLoadOverview = useCallback(async () => {
    if (!activeProject) return;
    setOverviewLoading(true);
    setOverviewError('');
    setOverviewMarkdown('');
    try {
      const result = await api.projects.aingel.overview(activeProject);
      setOverviewMarkdown(result.markdown);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setOverviewError(msg);
    } finally {
      setOverviewLoading(false);
    }
  }, [activeProject]);

  const activeProj = projects.find((p) => p.id === activeProject);
  const aingelChatId = activeProj?.aingel_chat_id ?? null;
  const aingelChat = aingelChatId ? chats.find((c) => c.id === aingelChatId) : undefined;
  const hasAingelChat = Boolean(aingelChat);

  const handleOpenAingelChat = useCallback(async () => {
    if (!aingelChatId) return;
    try {
      const full = await api.chats.get(aingelChatId);
      setActiveChat(full);
      setAingelPanelOpen(false);
    } catch (e) {
      console.error('Failed to load AIngel chat', e);
    }
  }, [aingelChatId, setActiveChat, setAingelPanelOpen]);

  const handleInitAingelChat = useCallback(async () => {
    if (!activeProject || !perms.canEdit) return;
    setChatIniting(true);
    setChatInitError('');
    try {
      const res = await api.projects.aingel.init(activeProject);
      if (res.chat_id) addAingelChatId(res.chat_id);
      // init writes aingel_chat_id onto the project row, and this panel reads the
      // chat id from there — without refreshing projects too, a successful init
      // still renders as "No AIngel chat yet" until the next poll.
      const [freshChats, freshProjects] = await Promise.all([
        api.chats.list({ status: 'active' }),
        api.projects.list(),
      ]);
      setChats(freshChats);
      setProjects(freshProjects);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setChatInitError(msg);
    } finally {
      setChatIniting(false);
    }
  }, [activeProject, perms.canEdit, addAingelChatId, setChats, setProjects]);

  if (!aingelPanelOpen) return null;

  return (
    <div className={cn('memory-panel fixed right-0 bottom-0 bg-surface-raised border-l border-default z-[1100] flex flex-col shadow-side relative', isMobile ? 'inset-0 top-0 w-full border-l-0' : 'top-[76px]')} style={isMobile ? undefined : { width: rightPanelWidth }}>
      {!isMobile && <RightPanelResizer />}
      <div className="memory-header flex justify-between px-3 py-2.5 border-b border-border-muted">
        <h3 className="m-0 text-md-">Guide</h3>
        <button
          data-tip="Close Guide panel"
          className="btn py-0.5 px-2.5 text-sm+ border border-default rounded cursor-pointer text-sm dark:border-border-dark-default dark:text-text-dark-soft"
          onClick={() => setAingelPanelOpen(false)}
        >
          Close
        </button>
      </div>

      <div className="memory-body flex-1 overflow-y-auto p-3">
        {!activeProject ? (
          <div className="mem-empty py-2 px-0.5 text-sm text-text-muted">Open a project to use the Guide</div>
        ) : (
          <>
            <div className="mb-3.5 border border-border-muted rounded-md overflow-hidden dark:border-border-dark-muted">
              <div
                className="flex items-center gap-1.5 px-2.5 py-1.5 bg-surface-subtle text-sm font-semibold text-ink dark:bg-surface-dark-subtle dark:text-text-dark"
              >
                <span className="shrink-0 w-1.5 h-1.5 rounded-full" style={{ background: '#c67139' }} />
                Guide chat
              </div>
              <div className="px-2.5 py-2">
                {hasAingelChat && aingelChat ? (
                  <div
                    className="flex items-center gap-1.5 py-1 cursor-pointer text-sm+ rounded hover:bg-accent-soft"
                    onClick={handleOpenAingelChat}
                    title={aingelChat.name}
                  >
                    <span className="text-ai dark:text-ai-dark inline-flex items-center"><Sparkles size={13} /></span>
                    <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-ai font-bold dark:text-ai-dark">
                      {aingelChat.name}
                    </span>
                    {aingelChat.message_count ? (
                      <span className="text-2xs bg-[#eee7db] text-[#645c50] px-1 rounded-[6px] shrink-0 dark:bg-border-dark-default dark:text-text-dark-muted">
                        {aingelChat.message_count}
                      </span>
                    ) : null}
                  </div>
                ) : (
                  <>
                    <p className="m-0 mb-2 text-sm+ text-text-soft">
                      {perms.canEdit
                        ? "No Guide chat yet. Initialize one to start talking with your project's Guide."
                        : 'No Guide chat yet. Ask a project member to initialize one.'}
                    </p>
                    {perms.canEdit && (
                      <button
                        data-tip="Create the Guide chat for this project"
                        className="btn btn-primary bg-accent text-white border border-accent py-1 px-3.5 text-sm+ rounded cursor-pointer hover:bg-accent-deep disabled:opacity-60 dark:bg-accent-dark dark:border-accent-dark inline-flex items-center gap-1.5"
                        disabled={chatIniting}
                        onClick={handleInitAingelChat}
                      >
                        {chatIniting ? <><LoaderCircle size={14} className="animate-spin" /> Initializing\u2026</> : <><Sparkles size={14} /> Start Guide chat</>}
                      </button>
                    )}
                    {chatInitError ? (
                      <div className="mt-2 text-sm+ text-danger">{chatInitError}</div>
                    ) : null}
                  </>
                )}
              </div>
            </div>

            <div className="mb-3.5 border border-border-muted rounded-md overflow-hidden dark:border-border-dark-muted">
              <div
                onClick={() => setOverviewExpanded((v) => !v)}
                className="flex items-center gap-1.5 px-2.5 py-1.5 bg-surface-subtle cursor-pointer text-sm font-semibold text-ink dark:bg-surface-dark-subtle dark:text-text-dark"
              >
                <span className="text-text-faint">{overviewExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
                Project Overview
              </div>
              {overviewExpanded && (
                <div className="px-2.5 py-2">
                  <p className="m-0 mb-2 text-sm+ text-text-soft">
                    Load the Guide-generated project overview.
                  </p>
                  <button
                    data-tip="Load the Guide-generated project overview"
                    className="btn btn-primary bg-accent text-white border border-accent py-1 px-3.5 text-sm+ rounded cursor-pointer hover:bg-accent-deep disabled:opacity-60 dark:bg-accent-dark dark:border-accent-dark inline-flex items-center gap-1.5"
                    disabled={overviewLoading}
                    onClick={handleLoadOverview}
                  >
                    {overviewLoading ? <><LoaderCircle size={14} className="animate-spin" /> Loading\u2026</> : <><ClipboardList size={14} /> Load Overview</>}
                  </button>
                  {overviewError ? (
                    <div className="mt-2 text-sm+ text-danger">{overviewError}</div>
                  ) : null}
                  {overviewMarkdown ? (
                    <pre className="m-2 mt-0 p-2 bg-surface-muted border border-border-muted rounded text-sm+ whitespace-pre-wrap max-h-[400px] overflow-y-auto dark:bg-surface-dark-muted dark:border-border-dark-muted dark:text-text-dark-soft">
                      {overviewMarkdown}
                    </pre>
                  ) : null}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
};

export default AIngelPanel;
