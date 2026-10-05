import { useStore } from '../store';

export type ProjectRole = 'viewer' | 'member' | 'admin' | 'owner';

/** viewer < member < admin < owner. Mirrors agent_auth._ROLE_RANK. */
export const ROLE_RANK: Record<ProjectRole, number> = {
  viewer: 1, member: 2, admin: 3, owner: 4,
};

export interface ProjectPermissions {
  /** Effective role on the project; null when unknown / no project. */
  role: ProjectRole | null;
  /** Global admin — treated as owner everywhere. */
  isAdmin: boolean;
  /** Auth disabled (single-user local install) — everything allowed. */
  authOff: boolean;
  /** member and above: create/edit/run tasks, files, chats, spec, deps. */
  canEdit: boolean;
  /** admin and above: add/remove members, change roles. */
  canManageMembers: boolean;
  /** owner (or global admin): settings, budget, git, roles, GPU/SCW, delete. */
  canAdminister: boolean;
}

/**
 * Resolve the caller's permissions on a project from the role the API puts on
 * each project (`current_user_role`).
 *
 * Fails closed when auth is on and the project isn't in the store: an unknown
 * project grants nothing. Auth-off (`user === null`) grants everything, and a
 * global admin gets owner everywhere — matching the backend.
 */
export function useProjectPermissions(projectId?: number | null): ProjectPermissions {
  const user = useStore((s) => s.user);
  const projects = useStore((s) => s.projects);

  const authOff = user == null;
  const isAdmin = user?.role === 'admin';

  let role: ProjectRole | null = null;
  if (projectId != null) {
    const p = projects.find((x) => x.id === projectId);
    const r = p?.current_user_role;
    if (r) role = r as ProjectRole;
    else if (authOff || isAdmin) role = 'owner';
  } else if (authOff || isAdmin) {
    role = 'owner';
  }

  const rank = role ? ROLE_RANK[role] : 0;
  const unrestricted = authOff || isAdmin;

  return {
    role,
    isAdmin,
    authOff,
    canEdit: unrestricted || rank >= ROLE_RANK.member,
    canManageMembers: unrestricted || rank >= ROLE_RANK.admin,
    canAdminister: unrestricted || rank >= ROLE_RANK.owner,
  };
}

/** Convenience for a dashboard/card list where the project may not be active. */
export function roleAtLeast(role: ProjectRole | null | undefined, min: ProjectRole): boolean {
  if (!role) return false;
  return ROLE_RANK[role] >= ROLE_RANK[min];
}
