import { useEffect, useRef, useMemo } from 'react';
import { useStore } from '../store';
import { cn } from '../utils/cn';
import { TriangleAlert, CircleCheck, LoaderCircle } from 'lucide-react';

interface NotifItem {
  icon: string;
  title: string;
  desc: string;
  time: string;
  type: 'failed' | 'approve' | 'running';
  taskId?: number;
  execId?: number;
  unread: boolean;
}

export const Notifications = () => {
  const {
    notifOpen, setNotifOpen,
    notifItems, setNotifItems,
    tasks, executions,
    activeProject, setActiveProject,
  } = useStore();

  const dropdownRef = useRef<HTMLDivElement>(null);

  const built = useMemo(() => {
    const items: NotifItem[] = [];
    const now = Date.now();
    const dayAgo = now - 24 * 60 * 60 * 1000;

    const failedEx = executions.filter((e) => {
      if (e.status !== 'failed') return false;
      const t = e.started_at ? new Date(e.started_at + (e.started_at.endsWith('Z') ? '' : 'Z')).getTime() : 0;
      return t > dayAgo;
    }).slice(0, 5);
    for (const e of failedEx) {
      items.push({
        icon: '\u2715',
        title: `Execution #${e.id} failed`,
        desc: (e.task_title || e.instructions || '').slice(0, 60),
        time: String(e.started_at || ''),
        type: 'failed',
        taskId: e.task_id,
        execId: e.id,
        unread: true,
      });
    }

    const pendingApprovals = executions.filter(e => {
      return e.status === 'done' && e.memory_status === 'pending';
    }).slice(0, 5);
    for (const e of pendingApprovals) {
      items.push({
        icon: '\u2713',
        title: `Execution #${e.id} ready to approve`,
        desc: (e.task_title || '').slice(0, 60),
        time: String(e.finished_at || e.started_at || ''),
        type: 'approve',
        execId: e.id,
        taskId: e.task_id,
        unread: true,
      });
    }

    const running = tasks.filter(t => t.status === 'running').slice(0, 3);
    for (const t of running) {
      const ext = t as unknown as Record<string, unknown>;
      items.push({
        icon: '\u23F3',
        title: `Task #${t.id} running`,
        desc: (t.title || '').slice(0, 60),
        time: String(ext.updated_at || ext.created_at || ''),
        type: 'running',
        taskId: t.id,
        unread: false,
      });
    }

    items.sort((a, b) => {
      const ta = a.time ? new Date(a.time).getTime() : 0;
      const tb = b.time ? new Date(b.time).getTime() : 0;
      return tb - ta;
    });
    return items;
  }, [tasks, executions]);

  useEffect(() => {
    setNotifItems(built);
  }, [built, setNotifItems]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        const bell = document.querySelector('.notif-bell');
        if (bell && !bell.contains(e.target as Node)) {
          setNotifOpen(false);
        }
      }
    };
    if (notifOpen) {
      document.addEventListener('mousedown', handler);
    }
    return () => document.removeEventListener('mousedown', handler);
  }, [notifOpen, setNotifOpen]);

  const handleItemClick = (item: NotifItem) => {
    setNotifOpen(false);
    if (item.taskId) {
      const task = tasks.find(t => t.id === item.taskId);
      if (task && activeProject !== task.project_id) {
        setActiveProject(task.project_id);
      }
    } else if (item.execId) {
      const exec = executions.find(e => e.id === item.execId);
      if (exec && exec.project_id != null && activeProject !== exec.project_id) {
        setActiveProject(exec.project_id);
      }
    }
  };

  const handleClear = () => {
    setNotifItems([]);
  };

  const fmtTime = (iso: string) => {
    if (!iso) return '';
    try {
      const d = new Date(iso.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(iso) ? iso : iso.replace(' ', 'T') + 'Z');
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      return `${hh}:${mm}`;
    } catch { return ''; }
  };

  if (!notifOpen) return null;

  return (
    <div
      ref={dropdownRef}
      className="fixed top-12 right-4 w-[340px] max-h-[400px] overflow-y-auto bg-surface-raised rounded-lg shadow-medium border border-default z-[210]"
    >
      <div className="flex justify-between items-center px-3 py-2 border-b border-default text-sm font-semibold dark:border-border-dark-default">
        <span>Notifications</span>
        <button
          data-tip="Clear all notifications"
          className="btn px-2 text-xs dark:text-text-dark-soft"
          style={{ padding: '2px 8px' }}
          onClick={handleClear}
        >
          Clear
        </button>
      </div>

      {notifItems.length === 0 ? (
        <div className="p-4 text-center text-sm text-faint dark:text-text-dark-faint">
          No notifications
        </div>
      ) : (
        (notifItems as NotifItem[]).map((item, idx) => (
          <div
            key={idx}
            className={cn(
              'flex gap-2.5 px-3 py-2.5 border-b border-default/50 cursor-pointer dark:border-border-dark-muted/50',
              item.unread ? 'bg-[#fbf6ee] dark:bg-accent-dark-tint' : 'bg-transparent'
            )}
            onClick={() => handleItemClick(item)}
          >
            <span className="text-base leading-[18px] w-5 text-center flex-shrink-0 inline-flex items-center justify-center">
              {item.type === 'failed' ? (
                <TriangleAlert size={16} className="text-danger" />
              ) : item.type === 'approve' ? (
                <CircleCheck size={16} className="text-status-done dark:text-status-done-dark" />
              ) : (
                <LoaderCircle size={16} className="text-status-running dark:text-status-running-dark animate-spin" />
              )}
            </span>
            <div className="flex-1 min-w-0">
              <div className={cn('text-sm mb-0.5', item.unread ? 'font-semibold' : 'font-normal')}>
                {item.title}
              </div>
              <div className="text-sm+ text-soft overflow-hidden text-ellipsis whitespace-nowrap dark:text-text-dark-soft">
                {item.desc}
              </div>
              {item.time && (
                <div className="text-xs text-faint mt-0.5 dark:text-text-dark-faint">
                  {fmtTime(item.time)}
                </div>
              )}
            </div>
          </div>
        ))
      )}
    </div>
  );
};

export default Notifications;