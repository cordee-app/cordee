import { useCallback, useEffect, useState } from 'react';
import { Crown, Check, X, Trash2 } from 'lucide-react';
import { useStore } from '../store';
import { api, ApiError } from '../api';
import type { AdminUser, ProjectMember, MemberRole, DirectoryUser } from '../types';

const MEMBER_ROLES: MemberRole[] = ['viewer', 'member', 'admin', 'owner'];

const selectCls =
  'py-[7px] px-2.5 border border-default rounded text-sm+ bg-surface-raised dark:bg-surface-dark-raised';
const btnCls =
  'btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md- disabled:opacity-50 disabled:cursor-not-allowed';

const friendly = (err: unknown): string => {
  if (err instanceof ApiError) {
    switch (err.message) {
      case 'cannot_modify_self':
        return 'You cannot modify your own account.';
      case 'last_admin':
        return 'Cannot delete the last admin.';
      case 'last_owner':
        return 'This would remove the last owner.';
      case 'not_found':
        return 'User or project not found.';
      case 'invalid_role':
        return 'Invalid role.';
      case 'invalid_plan':
        return 'Invalid plan.';
      case 'invalid_status':
        return 'Invalid status.';
      default:
        return err.message || 'Request failed.';
    }
  }
  return err instanceof Error ? err.message : 'Unknown error';
};

const CAPS: { label: string; caps: [boolean, boolean, boolean, boolean] }[] = [
  { label: 'See the project & read everything (tasks, chats, files, costs)', caps: [true, true, true, true] },
  { label: 'Create / edit / delete tasks and run them', caps: [false, true, true, true] },
  { label: 'Upload, move, copy, delete files', caps: [false, true, true, true] },
  { label: 'Chat with AI, approve / reject memory', caps: [false, true, true, true] },
  { label: 'Edit skills, spec, dependencies, HF models', caps: [false, true, true, true] },
  { label: 'Manage members (add / change role / remove)', caps: [false, false, true, true] },
  { label: 'Delete the project', caps: [false, false, false, true] },
  { label: 'Edit project settings (name, EU flag, model, git)', caps: [false, false, false, true] },
  { label: 'Budget, permissions, roles', caps: [false, false, false, true] },
  { label: 'GPU deployments & Scaleway session', caps: [false, false, false, true] },
];

const CapabilityMatrix = () => (
  <div className="mb-5">
    <div className="font-semibold text-sm+ text-text-soft mb-2">Role capabilities</div>
    <div className="text-xs text-text-faint mb-3">
      Ranks add up: viewer &lt; member &lt; admin &lt; owner. A global admin has owner rights
      everywhere. Project owners (and global admins) always pass every gate.
    </div>
    <div className="overflow-x-auto border border-border-muted rounded-md">
      <table className="w-full text-xs border-collapse">
        <thead>
          <tr className="bg-surface-subtle">
            <th className="text-left font-semibold px-2.5 py-2 border-b border-border-muted">Capability</th>
            {['viewer', 'member', 'admin', 'owner'].map(r => (
              <th key={r} className="font-semibold px-2.5 py-2 border-b border-border-muted capitalize">{r}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {CAPS.map((row) => (
            <tr key={row.label} className="odd:bg-surface-muted">
              <td className="px-2.5 py-2 border-b border-border-subtle text-text-soft">{row.label}</td>
              {row.caps.map((yes, i) => (
                <td key={i} className="px-2.5 py-2 border-b border-border-subtle text-center">
                  {yes ? (
                    <Check size={14} className="inline text-status-done" aria-label="yes" />
                  ) : (
                    <X size={14} className="inline text-text-faint" aria-label="no" />
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  </div>
);

/** Per-project panel: change the project role of existing members only.
 *  Adding/removing members is done from the project card (👥). */
const ProjectMembersPanel = ({ projectId }: { projectId: number }) => {
  const { projects, user } = useStore();
  const project = projects.find((p) => p.id === projectId);
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<number | null>(null);

  const myRole = project?.current_user_role ?? null;
  const canManage = myRole === 'owner' || myRole === 'admin' || user?.role === 'admin';
  const ownerCount = members.filter((m) => m.role === 'owner').length;

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await api.members.list(projectId);
      setMembers(res.members);
    } catch (err: unknown) {
      setError(friendly(err));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  const changeRole = async (m: ProjectMember, role: MemberRole) => {
    setBusy(m.user_id);
    setError('');
    try {
      const res = await api.members.update(projectId, m.user_id, role);
      setMembers(res.members);
    } catch (err: unknown) {
      setError(friendly(err));
    } finally {
      setBusy(null);
    }
  };

  const isSelf = (m: ProjectMember) => user != null && m.user_id === user.id;

  return (
    <div>
      <CapabilityMatrix />
      <div className="font-semibold text-sm+ text-text-soft mb-2">
        Members — {project?.name ?? ''}
      </div>
      <div className="text-xs text-text-faint mb-3">
        Change each member's role for this project. To add or remove people, use the
        members button on the project card.
      </div>

      {!canManage && (
        <div className="mb-3 p-3 rounded-md border border-border-muted bg-surface-subtle text-sm text-text-soft">
          Only project admins or owners can change roles.
        </div>
      )}
      {error && <div className="text-dangerStrong text-sm mb-2">{error}</div>}

      {loading ? (
        <div className="p-4 text-center text-text-faint">Loading...</div>
      ) : members.length === 0 ? (
        <div className="p-4 text-center text-text-faint">No members yet.</div>
      ) : (
        <div className="flex flex-col divide-y divide-border-muted border border-border-muted rounded-md">
          {members.map((m) => {
            const label = m.name || m.email || `User #${m.user_id}`;
            const onlyOwner = m.role === 'owner' && ownerCount <= 1;
            return (
              <div key={m.user_id} className="flex items-center gap-3 px-3 py-2.5">
                <div className="flex-1 min-w-0">
                  <div className="text-sm truncate">
                    {label}
                    {isSelf(m) ? <span className="ml-1.5 text-xs text-text-faint">(you)</span> : null}
                  </div>
                  {m.email && m.email !== m.name ? (
                    <div className="text-xs text-text-faint truncate">{m.email}</div>
                  ) : null}
                </div>
                {canManage ? (
                  <select
                    data-tip="Change this member's role"
                    className="py-[5px] px-2 border border-default rounded text-sm bg-surface-raised"
                    value={m.role}
                    disabled={busy === m.user_id || (isSelf(m) && onlyOwner)}
                    onChange={(e) => changeRole(m, e.target.value as MemberRole)}
                  >
                    {MEMBER_ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                ) : (
                  <span className="text-xs text-text-faint px-2 py-[3px] border border-border-muted rounded">
                    {m.role}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

/** Global accounts panel (dashboard): list users, flip plan/status/role, delete.
 *  Adding people to a project is done from the project card (👥). */
const AccountsPanel = () => {
  const { user } = useStore();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [directory, setDirectory] = useState<DirectoryUser[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<number | null>(null);
  const [rowError, setRowError] = useState<Record<number, string>>({});
  const [rowOk, setRowOk] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await api.auth.listUsers();
      setUsers(res.users);
      const dir = await api.auth.directory().catch(() => ({ users: [] as DirectoryUser[] }));
      setDirectory(dir.users);
    } catch (err: unknown) {
      setError('Failed to load users: ' + friendly(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const setRowErr = (id: number, msg: string) => {
    setRowError((prev) => ({ ...prev, [id]: msg }));
    setRowOk((prev) => ({ ...prev, [id]: '' }));
  };
  const setRowSuccess = (id: number, msg: string) => {
    setRowOk((prev) => ({ ...prev, [id]: msg }));
    setRowError((prev) => ({ ...prev, [id]: '' }));
    window.setTimeout(() => setRowOk((prev) => ({ ...prev, [id]: '' })), 2500);
  };

  const patchUser = async (
    target: AdminUser,
    data: Partial<Pick<AdminUser, 'plan' | 'status' | 'role'>>,
  ) => {
    setBusy(target.id);
    try {
      const res = await api.auth.updateUser(target.id, data);
      setUsers((prev) => prev.map((x) => (x.id === res.user.id ? res.user : x)));
      setRowSuccess(target.id, 'Saved');
    } catch (err: unknown) {
      setRowErr(target.id, friendly(err));
    } finally {
      setBusy(null);
    }
  };

  const deleteUser = async (target: AdminUser) => {
    const label = target.name || target.email || `User #${target.id}`;
    if (!window.confirm(
      `Delete ${label}? Their project memberships and usage counters are removed. ` +
      `Projects they owned are kept but become unowned. This cannot be undone.`)) return;
    setBusy(target.id);
    try {
      await api.auth.deleteUser(target.id);
      setUsers((prev) => prev.filter((x) => x.id !== target.id));
    } catch (err: unknown) {
      setRowErr(target.id, friendly(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <CapabilityMatrix />
      <div className="font-semibold text-sm+ text-text-soft mb-1 flex items-center gap-1.5">
        <Crown size={14} className="text-ai" /> User accounts
      </div>
      <div className="text-xs text-text-faint mb-3">
        Instance-wide accounts. Assign people to a project from the project card.
        Directory: {directory.length} account{directory.length === 1 ? '' : 's'}.
      </div>

      {loading ? (
        <div className="p-4 text-center text-text-faint">Loading...</div>
      ) : error ? (
        <div className="text-dangerStrong text-md- mb-2">{error}</div>
      ) : users.length === 0 ? (
        <div className="p-4 text-center text-text-faint">No users.</div>
      ) : (
        users.map((u) => {
          const isSelf = u.id === user?.id;
          const rowBusy = busy === u.id;
          return (
            <div key={u.id} className="border border-border-muted rounded-md p-3 mb-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-1.5 font-semibold text-ink dark:text-text-dark">
                    {u.role === 'admin' && <Crown size={14} className="text-ai shrink-0" />}
                    <span className="truncate">{u.name || u.email || 'User #' + u.id}</span>
                    {isSelf && <span className="text-2xs text-text-faint">(you)</span>}
                  </div>
                  <div className="text-sm+ text-text-faint truncate">{u.email}</div>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  <span className={'text-2xs px-1.5 py-0.5 rounded ' + (u.plan === 'paid' ? 'bg-accent-soft text-accent dark:bg-accent-dark-soft dark:text-accent-dark' : 'bg-surface-subtle text-text-muted dark:bg-surface-dark-subtle dark:text-text-dark-muted')}>
                    {u.plan}
                  </span>
                  <span className={'text-2xs px-1.5 py-0.5 rounded ' + (u.status === 'active' ? 'bg-status-done-bg text-status-done' : 'bg-status-failed-bg text-status-failed')}>
                    {u.status}
                  </span>
                  <span className="text-2xs px-1.5 py-0.5 rounded bg-surface-subtle text-text-muted dark:bg-surface-dark-subtle dark:text-text-dark-muted">
                    {u.role}
                  </span>
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-2 mt-2.5">
                <label className="flex items-center gap-1 text-sm+ text-text-soft dark:text-text-dark-soft">
                  Plan
                  <select data-tip="Change this user's plan" className={selectCls} value={u.plan} disabled={rowBusy}
                    onChange={(e) => patchUser(u, { plan: e.target.value as AdminUser['plan'] })}>
                    <option value="free">free</option>
                    <option value="paid">paid</option>
                  </select>
                </label>
                <label className="flex items-center gap-1 text-sm+ text-text-soft dark:text-text-dark-soft">
                  Role
                  <select data-tip="Change this user's global role" className={selectCls} value={u.role} disabled={rowBusy || isSelf}
                    onChange={(e) => patchUser(u, { role: e.target.value as AdminUser['role'] })}>
                    <option value="user">user</option>
                    <option value="admin">admin</option>
                  </select>
                </label>
                <button data-tip={u.status === 'active' ? 'Suspend this account' : 'Reactivate this account'} className={btnCls} disabled={rowBusy || isSelf}
                  onClick={() => patchUser(u, { status: u.status === 'active' ? 'suspended' : 'active' })}>
                  {u.status === 'active' ? 'Suspend' : 'Reactivate'}
                </button>
                <button
                  data-tip={isSelf ? 'You cannot delete yourself' : 'Delete account'}
                  className="btn py-[7px] px-[18px] border border-danger rounded cursor-pointer text-md- text-danger inline-flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                  disabled={rowBusy || isSelf}
                  onClick={() => deleteUser(u)}
                >
                  <Trash2 size={14} /> Delete
                </button>
                {isSelf && <span className="text-2xs text-text-faint">You cannot modify or delete your own account.</span>}
                {rowOk[u.id] ? <span className="text-sm+ text-status-done">{rowOk[u.id]}</span> : null}
                {rowError[u.id] ? <span className="text-sm+ text-dangerStrong">{rowError[u.id]}</span> : null}
              </div>
            </div>
          );
        })
      )}
    </div>
  );
};

/** Settings → Users. Global accounts when no project is open; the open
 *  project's membership roles otherwise. */
export const SettingsUsersTab = () => {
  const { user, activeProject } = useStore();
  const isAdmin = user?.role === 'admin';

  if (activeProject != null) {
    return <ProjectMembersPanel projectId={activeProject} />;
  }
  if (!isAdmin) {
    return (
      <div>
        <CapabilityMatrix />
        <div className="text-sm text-text-soft">
          Open a project to see and change its members' roles. Only an administrator
          can manage instance-wide accounts.
        </div>
      </div>
    );
  }
  return <AccountsPanel />;
};

export default SettingsUsersTab;
