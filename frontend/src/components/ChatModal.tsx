import { useState, useEffect, useMemo } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { useProjectPermissions } from '../hooks/useProjectPermissions';

type Scope = 'project' | 'phase' | 'task';

export const ChatModal = () => {
  const {
    showChatModal, setShowChatModal,
    projects, phasesData, tasks, models,
    setActiveChat, setChats, addAingelChatId,
    user,
  } = useStore();

  const isFreeUser = user?.plan === 'free' && user?.role !== 'admin';

  const [name, setName] = useState('');
  const [scope, setScope] = useState<Scope>('project');
  const [projectId, setProjectId] = useState<number>(0);
  const [phaseName, setPhaseName] = useState('');
  const [taskId, setTaskId] = useState<number>(0);
  const [model, setModel] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const perms = useProjectPermissions(projectId || null);

  if (!showChatModal) return null;

  const scopeOptions: { value: Scope; label: string }[] = [
    { value: 'project', label: 'Project' },
    { value: 'phase', label: 'Phase' },
    { value: 'task', label: 'Task' },
  ];

  const selectedProjectPhases = phasesData.filter(
    (p) => p.project_id === projectId || p.project_id === undefined,
  );

  const selectedPhaseTasks = tasks.filter(
    (t) =>
      t.project_id === projectId &&
      (scope !== 'task' || !phaseName || t.phase_name === phaseName),
  );

  const selectedTask = useMemo(() => {
    if (scope === 'task' && taskId) {
      return tasks.find((t) => t.id === taskId) || null;
    }
    return null;
  }, [scope, taskId, tasks]);

  useEffect(() => {
    if (selectedTask?.model) {
      setModel(selectedTask.model);
    }
  }, [selectedTask]);

  const taskModelLabel = useMemo(() => {
    if (!selectedTask?.model) return '';
    const m = models.find((x) => x.id === selectedTask.model);
    return m?.label || selectedTask.model;
  }, [selectedTask, models]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !projectId) return;

    setSubmitting(true);
    try {
      const body: {
        project_id: number;
        name: string;
        phase_name?: string;
        task_id?: number;
        model?: string;
      } = {
        project_id: projectId,
        name: name.trim(),
      };

      if (scope === 'phase' && phaseName) body.phase_name = phaseName;
      if (scope === 'task' && taskId) body.task_id = taskId;
      if (model) body.model = model;

      const newChat = await api.chats.create(body);

      const freshChats = await api.chats.list({ status: 'active' });
      setChats(freshChats);

      const proj = projects.find((p) => p.id === projectId);
      if (proj && (proj.name || '').toLowerCase().includes('aingel')) {
        addAingelChatId(newChat.id);
      }

      setActiveChat(newChat);
      setShowChatModal(false);
    } catch (e) {
      console.error('Chat creation failed', e);
    } finally {
      setSubmitting(false);
    }
  };

  const handleClose = () => {
    if (!submitting) setShowChatModal(false);
  };

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={(e) => { if (e.target === e.currentTarget) handleClose(); }}>
      <div className="modal-content bg-surface-raised rounded-lg p-6 min-w-[420px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong" onClick={(e) => e.stopPropagation()}>
        <h3 className="m-0 mb-4 text-base">New Chat</h3>
        <form onSubmit={handleSubmit}>
          <div className="form-group mb-3">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">Name</label>
            <input
              data-tip="Enter a name for this chat"
              type="text"
              className="w-full py-[7px] px-2.5 border border-default rounded text-base"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Chat name"
              required
              autoFocus
            />
          </div>

          <div className="form-group mb-3">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">Scope</label>
            <select data-tip="Choose chat scope: project, phase, or task" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={scope} onChange={(e) => setScope(e.target.value as Scope)}>
              {scopeOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>

          <div className="form-group mb-3">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">Project</label>
            <select
              data-tip="Choose the project for this chat"
              className="w-full py-[7px] px-2.5 border border-default rounded text-base"
              value={projectId || ''}
              onChange={(e) => {
                setProjectId(Number(e.target.value) || 0);
                setPhaseName('');
                setTaskId(0);
              }}
              required
            >
              <option value="">-- Select project --</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>

          {scope === 'phase' && (
            <div className="form-group mb-3">
              <label className="block text-sm font-semibold text-text-soft mb-[3px]">Phase</label>
              <select
                data-tip="Choose the phase for this chat"
                className="w-full py-[7px] px-2.5 border border-default rounded text-base"
                value={phaseName}
                onChange={(e) => setPhaseName(e.target.value)}
                required={scope === 'phase'}
              >
                <option value="">-- Select phase --</option>
                {selectedProjectPhases.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
          )}

          {scope === 'task' && (
            <div className="form-group mb-3">
              <label className="block text-sm font-semibold text-text-soft mb-[3px]">Task</label>
              <select
                data-tip="Choose the task for this chat"
                className="w-full py-[7px] px-2.5 border border-default rounded text-base"
                value={taskId || ''}
                onChange={(e) => setTaskId(Number(e.target.value) || 0)}
                required={scope === 'task'}
              >
                <option value="">-- Select task --</option>
                {selectedPhaseTasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    #{t.id} {t.title}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="form-group mb-3">
            <label className="block text-sm font-semibold text-text-soft mb-[3px]">Model</label>
            <select data-tip="Choose the default model for this chat" className="w-full py-[7px] px-2.5 border border-default rounded text-base" value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">-- Default --</option>
              {models.map((m) => (
                <option key={m.id} value={m.id} disabled={isFreeUser && m.free_allowed === false}>
                  {m.label}{isFreeUser && m.free_allowed === false ? ' (Upgrade)' : ''}
                </option>
              ))}
            </select>
            {selectedTask?.model && (
              <div className="text-xs text-accent mt-1">
                Model will be set to: {taskModelLabel}
              </div>
            )}
          </div>

          <div className="modal-actions flex justify-end gap-2 mt-4">
            <button data-tip="Close without creating a chat" type="button" className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-" onClick={handleClose} disabled={submitting}>
              Cancel
            </button>
            {perms.canEdit && (
              <button data-tip="Create the new chat" type="submit" className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white" disabled={submitting || !name.trim() || !projectId}>
                {submitting ? 'Creating...' : 'Create Chat'}
              </button>
            )}
          </div>
        </form>
      </div>
    </div>
  );
};

export default ChatModal;
