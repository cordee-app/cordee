import { useState, useRef, useEffect } from 'react';
import { ChevronDown, LogOut, User as UserIcon, Crown, BarChart3, Users } from 'lucide-react';
import { useStore } from '../store';
import { api } from '../api';

export const UserMenu = () => {
  const { user, quotas, activeProject } = useStore();
  const setShowSettingsModal = useStore((s) => s.setShowSettingsModal);
  const setSettingsTab = useStore((s) => s.setSettingsTab);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  if (!user) return null;

  const isAdmin = user.role === 'admin';
  const isFree = user.plan === 'free' && !isAdmin;

  const handleLogout = async () => {
    try {
      const res = await api.auth.logout();
      if (res.end_session_url) {
        window.location.href = res.end_session_url;
      } else {
        window.location.href = '/?loggedout=1';
      }
    } catch {
      window.location.href = '/?loggedout=1';
    }
  };

  const pct = (used: number, limit: number) => Math.min(100, Math.round((used / limit) * 100));
  const fmt = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(0)}K` : String(n);
  const fmtBytes = (n: number) => n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`;

  return (
    <div className="relative" ref={ref}>
      <button
        data-tip="Open the user menu"
        className="header-btn flex items-center gap-1.5 px-2.5 py-1 border border-white/20 dark:border-white/30 rounded bg-transparent text-white text-sm+ cursor-pointer hover:bg-white/10"
        onClick={() => setOpen(v => !v)}
      >
        <UserIcon size={14} />
        <span className="max-w-[120px] overflow-hidden text-ellipsis whitespace-nowrap">{user.name || user.email}</span>
        {isAdmin && <Crown size={12} className="text-amber-400" />}
        <ChevronDown size={12} className="opacity-60" />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-[260px] rounded-md shadow-lg z-[1450] overflow-hidden" style={{ background: '#ffffff', border: '1px solid #e4e4e7' }}>
          {/* User info header */}
          <div className="px-3 py-2.5" style={{ borderBottom: '1px solid #e4e4e7' }}>
            <div className="text-sm font-semibold" style={{ color: '#18181b' }}>{user.name || user.email}</div>
            <div className="text-xs" style={{ color: '#71717a' }}>{user.email}</div>
            <div className="flex items-center gap-1.5 mt-1">
              <span className={`text-[10px] uppercase tracking-wide font-semibold px-1.5 py-0.5 rounded ${
                isAdmin
                  ? 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300'
                  : isFree
                    ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300'
                    : 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300'
              }`}>
                {isAdmin ? 'Admin' : isFree ? 'Free' : 'Paid'}
              </span>
            </div>
          </div>

          {/* Quota bar for free users */}
          {isFree && quotas && (
            <div className="px-3 py-2.5 border-b border-border-muted dark:border-border-dark-muted space-y-2">
              <div className="flex items-center gap-1.5 text-xs font-semibold" style={{ color: '#18181b' }}>
                <BarChart3 size={12} /> Usage
              </div>
              {[
                { label: 'Runs', used: quotas.usage.runs, limit: quotas.limits.max_runs, fmt: fmt },
                { label: 'Tokens', used: quotas.usage.tokens_total, limit: quotas.limits.max_tokens, fmt: fmt },
                { label: 'Storage', used: quotas.usage.storage_bytes, limit: quotas.limits.max_storage, fmt: fmtBytes },
              ].map(q => (
                <div key={q.label}>
                  <div className="flex justify-between text-[11px] mb-0.5" style={{ color: '#5c5c66' }}>
                    <span>{q.label}</span>
                    <span>{q.fmt(q.used)} / {q.fmt(q.limit)}</span>
                  </div>
                  <div className="h-2 rounded-full overflow-hidden" style={{ background: '#e8e8ec' }}>
                    <div
                      className="h-full rounded-full transition-all"
                      style={{
                        width: `${pct(q.used, q.limit)}%`,
                        background: pct(q.used, q.limit) >= 90 ? '#ef4444' : pct(q.used, q.limit) >= 75 ? '#f59e0b' : '#3b82f6',
                        minHeight: '8px',
                      }}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
          {isFree && !quotas && (
            <div className="px-3 py-2 border-b border-border-muted dark:border-border-dark-muted">
              <span className="text-xs" style={{ color: '#71717a' }}>Usage data unavailable</span>
            </div>
          )}

          {/* Users: project members when a project is open, accounts otherwise */}
          <button
            data-tip={activeProject != null ? 'Manage project members' : 'Manage user accounts'}
            className="w-full text-left px-3 py-2.5 text-sm flex items-center gap-2 hover:bg-gray-50 border-b border-border-muted dark:border-border-dark-muted"
            style={{ color: '#18181b' }}
            onClick={() => { setOpen(false); setSettingsTab('users'); setShowSettingsModal(true); }}
          >
            <Users size={15} style={{ color: '#18181b' }} />
            {activeProject != null ? 'Project members' : 'Manage users'}
          </button>

          {/* Logout */}
          <button
            data-tip="Log out of your account"
            className="w-full text-left px-3 py-2.5 text-sm flex items-center gap-2 hover:bg-gray-50"
            style={{ color: '#18181b' }}
            onClick={handleLogout}
          >
            <LogOut size={15} style={{ color: '#18181b' }} /> Logout
          </button>
        </div>
      )}
    </div>
  );
};

export default UserMenu;
