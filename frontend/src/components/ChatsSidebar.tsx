import { useState, useMemo } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import type { Chat } from '../types';
import { cn } from '../utils/cn';
import { RightPanelResizer } from './RightPanelResizer';
import { useIsMobile } from '../hooks/useIsMobile';
import { useProjectPermissions } from '../hooks/useProjectPermissions';
import { ChevronRight, Folder, Check, X, Trash2, Sparkles } from 'lucide-react';

type Scope = 'project' | 'phase' | 'task';

function chatScopeLabel(c: { task_id?: number; phase_name?: string }): Scope {
  if (c.task_id) return 'task';
  if ((c.phase_name || '').trim()) return 'phase';
  return 'project';
}
const SCOPE_DOT_COLORS: Record<Scope, string> = {
  project: 'var(--green)',
  phase: '#3f628f',
  task: '#c67139',
};

const AINGEL_COLOR = '#c67139';

export const ChatsSidebar = () => {
  const {
    chats, activeChat, setActiveChat, activeProject,
    activePhase, activeStep, aingelChatIds, tasks,
    chatsPanelOpen, setChatsPanelOpen, setChats,
    rightPanelWidth,
  } = useStore();

  const isMobile = useIsMobile();

  const permsProjectId = activeStep?.projectId ?? activePhase?.projectId ?? activeProject;
  const perms = useProjectPermissions(permsProjectId);

  const [search, setSearch] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  const filteredChats = useMemo(() => {
    let result = chats.filter((c) => !aingelChatIds.has(c.id));

    if (activeStep !== null) {
      const matchedTask = tasks.find(
        (t) => t.project_id === activeStep.projectId && t.title === activeStep.stepText,
      );
      if (matchedTask) {
        result = result.filter((c) => c.task_id === matchedTask.id);
      } else {
        result = [];
      }
    } else if (activePhase !== null) {
      const phaseName = activePhase.phaseName;
      const phaseTaskIds = new Set(
        tasks
          .filter(
            (t) =>
              t.project_id === activePhase.projectId &&
              t.phase_name === phaseName,
          )
          .map((t) => t.id),
      );
      result = result.filter(
        (c) =>
          c.project_id === activePhase.projectId &&
          ((c.phase_name && c.phase_name === phaseName) ||
            (c.task_id && phaseTaskIds.has(c.task_id))),
      );
    } else if (activeProject !== null) {
      result = result.filter((c) => c.project_id === activeProject);
    } else {
      result = [];
    }

    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter(
        (c) =>
          (c.name || '').toLowerCase().includes(q) ||
          (c.project_name || '').toLowerCase().includes(q) ||
          (c.task_title || '').toLowerCase().includes(q),
      );
    }

    return result;
  }, [chats, activeProject, activePhase, activeStep, tasks, search, aingelChatIds]);

  const grouped = useMemo(() => {
    const project: Chat[] = [];
    const phase = new Map<string, Chat[]>();
    const task = new Map<string, Chat[]>();

    for (const c of filteredChats) {
      const scope = chatScopeLabel(c);
      if (scope === 'project') {
        project.push(c);
      } else if (scope === 'phase') {
        const ph = c.phase_name || 'Notebook';
        const list = phase.get(ph) || [];
        list.push(c);
        phase.set(ph, list);
      } else {
        const tt = c.task_title || `Task #${c.task_id}`;
        const list = task.get(tt) || [];
        list.push(c);
        task.set(tt, list);
      }
    }

    return { project, phase, task };
  }, [filteredChats]);

  const handleSelectChat = async (chatId: number) => {
    try {
      const full = await api.chats.get(chatId);
      setActiveChat(full);
      setChatsPanelOpen(false);
    } catch (e) {
      console.error('Failed to load chat', e);
    }
  };

  const handleDeleteChat = async (chatId: number) => {
    try {
      await api.chats.delete(chatId);
      const fresh = await api.chats.list({ status: 'active' });
      setChats(fresh);
      setConfirmDeleteId(null);
    } catch (e) {
      console.error('Failed to delete chat', e);
      setConfirmDeleteId(null);
    }
  };

  if (!chatsPanelOpen) return null;

  const totalCount = filteredChats.length;

  return (
    <div className={cn('memory-panel fixed right-0 bottom-0 bg-surface-raised border-l border-default z-50 flex flex-col shadow-side relative', isMobile ? 'inset-0 top-0 w-full border-l-0' : 'top-[76px]')} style={isMobile ? undefined : { width: rightPanelWidth }}>
      {!isMobile && <RightPanelResizer />}
      <div className="memory-header flex justify-between px-3 py-2.5 border-b border-[#dcd3c4] dark:border-border-dark-muted">
        <h3 className="m-0 text-md-">Chats</h3>
        <button
          data-tip="Close chats panel"
          className="btn py-0.5 px-2.5 border border-[#dcd3c4] rounded cursor-pointer text-md- dark:border-border-dark-default dark:text-text-dark-soft"
          onClick={() => setChatsPanelOpen(false)}
        >
          Close
        </button>
      </div>

      <div className="px-3 py-1.5 border-b border-[#dcd3c4] dark:border-border-dark-muted">
        <input
          data-tip="Filter chats by name, project, or task"
          type="text"
          placeholder={`Search ${totalCount} chat${totalCount === 1 ? '' : 's'}\u2026`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full box-border py-1 px-2 text-sm+ border border-[#dcd3c4] rounded bg-surface-raised dark:bg-surface-dark-base dark:border-border-dark-default dark:text-text-dark"
        />
      </div>

      <div className="memory-body flex-1 overflow-y-auto p-0">
        {filteredChats.length === 0 ? (
          <div className="chats-sidebar-empty p-3 text-sm+ text-muted italic text-center">
            {search.trim() ? 'No chats match your search' : 'No chats in this scope'}
          </div>
        ) : (
          <div className="chats-sidebar-list flex-1 overflow-y-auto">
            {grouped.project.map((c) => (
              <ChatRow
                key={c.id}
                chat={c}
                isActive={activeChat?.id === c.id}
                isAIngel={false}
                scope="project"
                canEdit={perms.canEdit}
                onClick={() => handleSelectChat(c.id)}
                confirmDelete={confirmDeleteId === c.id}
                onRequestDelete={() => setConfirmDeleteId(c.id)}
                onConfirmDelete={() => handleDeleteChat(c.id)}
                onCancelDelete={() => setConfirmDeleteId(null)}
              />
            ))}

            {Array.from(grouped.phase.entries()).map(([ph, list]) => (
              <div key={ph}>
                <div className="chat-sub-header py-0.5 pl-[22px] text-xs text-muted overflow-hidden text-ellipsis whitespace-nowrap inline-flex items-center gap-1 dark:text-text-dark-muted"><Folder size={12} className="shrink-0" /> {ph}</div>
                {list.map((c) => (
                  <ChatRow
                    key={c.id}
                    chat={c}
                    isActive={activeChat?.id === c.id}
                    isAIngel={aingelChatIds.has(c.id)}
                    scope="phase"
                    canEdit={perms.canEdit}
                    onClick={() => handleSelectChat(c.id)}
                    confirmDelete={confirmDeleteId === c.id}
                    onRequestDelete={() => setConfirmDeleteId(c.id)}
                    onConfirmDelete={() => handleDeleteChat(c.id)}
                    onCancelDelete={() => setConfirmDeleteId(null)}
                  />
                ))}
              </div>
            ))}

            {Array.from(grouped.task.entries()).map(([tt, list]) => (
              <div key={tt}>
                <div className="chat-sub-header py-0.5 pl-[22px] text-xs text-muted overflow-hidden text-ellipsis whitespace-nowrap inline-flex items-center gap-1 dark:text-text-dark-muted" title={tt}>
                  <ChevronRight size={12} className="shrink-0" /> {tt.length > 26 ? tt.slice(0, 26) + '\u2026' : tt}
                </div>
                {list.map((c) => (
                  <ChatRow
                    key={c.id}
                    chat={c}
                    isActive={activeChat?.id === c.id}
                    isAIngel={aingelChatIds.has(c.id)}
                    scope="task"
                    canEdit={perms.canEdit}
                    onClick={() => handleSelectChat(c.id)}
                    confirmDelete={confirmDeleteId === c.id}
                    onRequestDelete={() => setConfirmDeleteId(c.id)}
                    onConfirmDelete={() => handleDeleteChat(c.id)}
                    onCancelDelete={() => setConfirmDeleteId(null)}
                  />
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

const ChatRow = ({
  chat,
  isActive,
  isAIngel,
  scope,
  canEdit,
  onClick,
  confirmDelete,
  onRequestDelete,
  onConfirmDelete,
  onCancelDelete,
}: {
  chat: { id: number; name: string; message_count?: number };
  isActive: boolean;
  isAIngel: boolean;
  scope: Scope;
  canEdit: boolean;
  onClick: () => void;
  confirmDelete: boolean;
  onRequestDelete: () => void;
  onConfirmDelete: () => void;
  onCancelDelete: () => void;
}) => {
  const dotColor = isAIngel ? AINGEL_COLOR : SCOPE_DOT_COLORS[scope];
  const truncatedName = (chat.name || '').length > 35
    ? (chat.name || '').slice(0, 35) + '\u2026'
    : (chat.name || '');

  return (
    <div
      className={cn('chat-row group flex items-center gap-1.5 px-3 py-1 cursor-pointer text-sm+ rounded-none hover:bg-accent-soft', isActive && 'active')}
      onClick={onClick}
      title={chat.name}
    >
      <span className="chat-row-dot w-1.5 h-1.5 rounded-full shrink-0" style={{ background: dotColor }} />
      {isAIngel ? (
        <span className="chat-row-name aingel flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[#c67139] font-bold inline-flex items-center gap-1 dark:text-ai-dark"><Sparkles size={13} className="shrink-0" /> {truncatedName.replace(/^\u2746\s*/, '')}</span>
      ) : (
        <span className="chat-row-name flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{truncatedName}</span>
      )}
      {chat.message_count ? (
        <span className="chat-row-count text-2xs bg-[#eee7db] text-[#645c50] px-1 rounded-[6px] shrink-0 dark:bg-border-dark-default dark:text-text-dark-muted">{chat.message_count}</span>
      ) : null}
      {canEdit && (confirmDelete ? (
        <span className="flex items-center gap-0.5 shrink-0" onClick={(e) => e.stopPropagation()}>
          <button
            data-tip="Confirm delete"
            className="text-xs text-danger px-1 py-0 cursor-pointer hover:font-bold inline-flex items-center"
            onClick={onConfirmDelete}
          >
            <Check size={13} />
          </button>
          <button
            data-tip="Cancel delete"
            className="text-xs text-text-muted px-1 py-0 cursor-pointer inline-flex items-center"
            onClick={onCancelDelete}
          >
            <X size={13} />
          </button>
        </span>
      ) : (
        <button
          data-tip="Delete chat"
          className="chat-row-delete text-xs text-text-faint px-0.5 cursor-pointer opacity-0 group-hover:opacity-100 hover:text-danger shrink-0 inline-flex items-center"
          onClick={(e) => { e.stopPropagation(); onRequestDelete(); }}
        >
          <Trash2 size={13} />
        </button>
      ))}
    </div>
  );
};

export default ChatsSidebar;