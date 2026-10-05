import { useState, useEffect } from 'react';
import { X, Trash2, UserPlus } from 'lucide-react';
import { useStore } from '../store';
import { api, ApiError } from '../api';
import type { ProjectMember, MemberRole, DirectoryUser } from '../types';

const ROLES: MemberRole[] = ['viewer', 'member', 'admin', 'owner'];

const mapError = (err: unknown): string => {
  const raw = err instanceof ApiError || err instanceof Error ? err.message : 'unknown error';
  switch (raw) {
    case 'not_found':
      return 'No such user';
    case 'last_owner':
      return 'Cannot remove the last owner';
    case 'invalid_role':
      return 'Invalid role';
    case 'missing_user_id_or_role':
      return 'Choose a user and a role';
    case 'invalid_body':
      return 'Invalid request';
    default:
      return raw;
  }
};

/**
 * Project card member manager: add people to, or remove them from, this
 * project. Changing an existing member's role is done from the project's
 * Settings → Users tab (to keep one home for each action).
 */
export const MembersModal = () => {
  const {
    showMembersModal, setShowMembersModal,
    membersProjectId, setMembersProjectId,
    projects, user,
  } = useStore();

  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [directory, setDirectory] = useState<DirectoryUser[]>([]);
  const [addUserId, setAddUserId] = useState('');
  const [addRole, setAddRole] = useState<MemberRole>('member');
  const [adding, setAdding] = useState(false);

  const project = projects.find((p) => p.id === membersProjectId);

  useEffect(() => {
    if (showMembersModal && membersProjectId != null && !project) {
      setShowMembersModal(false);
      setMembersProjectId(null);
    }
  }, [showMembersModal, membersProjectId, project, setShowMembersModal, setMembersProjectId]);

  useEffect(() => {
    if (!showMembersModal || membersProjectId == null) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    setMembers([]);
    api.members.list(membersProjectId)
      .then((res) => { if (!cancelled) setMembers(res.members); })
      .catch((err: unknown) => { if (!cancelled) setError(mapError(err)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    api.auth.directory()
      .then((res) => { if (!cancelled) setDirectory(res.users); })
      .catch(() => { if (!cancelled) setDirectory([]); });
    return () => { cancelled = true; };
  }, [showMembersModal, membersProjectId]);

  if (!showMembersModal || membersProjectId == null) return null;
  if (!project) return null;

  const pid = membersProjectId;
  const myRole = project.current_user_role ?? null;
  const canManage = myRole === 'owner' || myRole === 'admin' || user?.role === 'admin';
  const ownerCount = members.filter((m) => m.role === 'owner').length;
  const isSelf = (m: ProjectMember) => user != null && m.user_id === user.id;
  const memberIds = new Set(members.map((m) => m.user_id));
  const candidates = directory.filter((u) => !memberIds.has(u.id));
  const labelFor = (u: DirectoryUser) =>
    `${u.name || u.email || `User #${u.id}`}${u.email && u.email !== u.name ? ` (${u.email})` : ''}`;

  const close = () => {
    setShowMembersModal(false);
    setMembersProjectId(null);
    setMembers([]);
    setError('');
    setAddUserId('');
    setAddRole('member');
  };

  const handleBackdrop = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) close();
  };

  const handleAdd = async () => {
    const uid = parseInt(addUserId, 10);
    if (!Number.isFinite(uid) || uid <= 0) {
      setError('Choose a user to add.');
      return;
    }
    setAdding(true);
    setError('');
    try {
      const res = await api.members.add(pid, uid, addRole);
      setMembers(res.members);
      setAddUserId('');
      setAddRole('member');
    } catch (err: unknown) {
      setError(mapError(err));
    } finally {
      setAdding(false);
    }
  };

  const handleRemove = async (m: ProjectMember) => {
    if (isSelf(m)) return;
    const label = m.name || m.email || `User #${m.user_id}`;
    if (!window.confirm(`Remove ${label} from this project?`)) return;
    setError('');
    try {
      const res = await api.members.remove(pid, m.user_id);
      setMembers(res.members);
    } catch (err: unknown) {
      setError(mapError(err));
    }
  };

  return (
    <div className="modal-backdrop fixed inset-0 bg-black/40 flex items-center justify-center z-modal" onClick={handleBackdrop}>
      <div className="modal-content bg-surface-raised dark:bg-surface-dark-raised rounded-lg p-6 min-w-[440px] max-w-[90vw] max-h-[85vh] overflow-y-auto shadow-strong">
        <div className="flex items-start justify-between mb-4">
          <h3 className="m-0 text-base">Members — {project.name}</h3>
          <button
            data-tip="Close the members dialog"
            className="p-1 border-none bg-transparent cursor-pointer text-text-soft hover:text-text"
            onClick={close}
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {!canManage ? (
          <div className="mb-4 p-3 rounded-md border border-border-muted bg-surface-subtle text-sm text-text-soft">
            Only project admins or owners can change membership.
          </div>
        ) : null}

        {error ? (
          <div className="text-dangerStrong text-sm mb-2">{error}</div>
        ) : null}

        {loading ? (
          <div className="p-4 text-center text-text-faint">Loading...</div>
        ) : members.length === 0 ? (
          <div className="p-4 text-center text-text-faint">No members yet.</div>
        ) : (
          <div className="flex flex-col divide-y divide-border-muted">
            {members.map((m) => {
              const label = m.name || m.email || `User #${m.user_id}`;
              const secondary = m.email && m.email !== m.name ? m.email : '';
              const onlyOwner = m.role === 'owner' && ownerCount <= 1;
              return (
                <div key={m.user_id} className="flex items-center gap-3 py-2.5">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm truncate">
                      {label}
                      {isSelf(m) ? <span className="ml-1.5 text-xs text-text-faint">(you)</span> : null}
                    </div>
                    {secondary ? (
                      <div className="text-xs text-text-faint truncate">{secondary}</div>
                    ) : null}
                  </div>
                  <span className="text-xs text-text-faint px-2 py-[3px] border border-border-muted rounded">
                    {m.role}
                  </span>
                  {canManage ? (
                    <button
                      data-tip={isSelf(m) ? 'You cannot remove yourself' : onlyOwner ? 'Cannot remove the last owner' : 'Remove from this project'}
                      className="p-1.5 border-none bg-transparent cursor-pointer text-danger disabled:opacity-40 disabled:cursor-not-allowed"
                      onClick={() => handleRemove(m)}
                      disabled={isSelf(m) || onlyOwner}
                      aria-label={`Remove ${label}`}
                    >
                      <Trash2 size={16} />
                    </button>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}

        {canManage ? (
          <div className="mt-4 pt-4 border-t border-border-muted">
            <div className="flex items-end gap-2">
              <div className="flex-1">
                <label className="block text-sm font-semibold text-text-soft mb-[3px]">Add user</label>
                <select
                  data-tip="Choose a user from the directory"
                  className="w-full py-[7px] px-2.5 border border-default rounded text-base bg-surface-raised"
                  value={addUserId}
                  onChange={(e) => setAddUserId(e.target.value)}
                >
                  <option value="">Select a user...</option>
                  {candidates.map((u) => (
                    <option key={u.id} value={String(u.id)}>{labelFor(u)}</option>
                  ))}
                </select>
                {candidates.length === 0 && (
                  <div className="text-2xs text-text-faint mt-1">
                    Everyone in the directory is already a member.
                  </div>
                )}
              </div>
              <div>
                <label className="block text-sm font-semibold text-text-soft mb-[3px]">Role</label>
                <select
                  data-tip="Choose the member role to grant"
                  className="py-[8px] px-2 border border-default rounded text-base bg-surface-raised"
                  value={addRole}
                  onChange={(e) => setAddRole(e.target.value as MemberRole)}
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
              </div>
              <button
                data-tip="Add the selected user to the project"
                className="btn btn-primary py-[7px] px-[18px] border border-accent rounded cursor-pointer text-md- bg-accent text-white inline-flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                onClick={handleAdd}
                disabled={adding || !addUserId}
              >
                <UserPlus size={16} />
                {adding ? 'Adding...' : 'Add'}
              </button>
            </div>
          </div>
        ) : null}

        <div className="modal-actions flex justify-end gap-2 mt-4">
          <button
            data-tip="Close the members dialog"
            className="btn py-[7px] px-[18px] border border-default rounded cursor-pointer text-md-"
            onClick={close}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
};

export default MembersModal;
