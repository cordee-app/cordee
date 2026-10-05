"""AIngel multi-tenancy — Phase 2: auth foundation.

Single-user Flask app growing multi-tenant. This module adds the auth layer
*without* changing any existing behaviour while ``AINGEL_AUTH=off`` (the
default): every decorator passes through and the ``before_request`` handler
only initialises ``g.current_user = None``.

Set ``AINGEL_AUTH=oidc`` (plus ``AINGEL_SESSION_SECRET`` and the
``AINGEL_OIDC_*`` vars — see ``.env.example``) to enable Authentik OIDC
(authorization-code flow + PKCE S256, session cookies).

Phase 3 (not this module) wires ``@require_auth`` / ``@require_admin`` /
``@require_project_access`` onto the existing routes in ``agent_api.py``.

Design notes (from Plans §11.2):
- Background threads (slot scheduler, chat replies, GPU runner, H2/H3/H4
  daemons) cannot use Flask ``g`` — enforcement there uses durable DB
  identity (``projects.owner_id`` / ``created_by``). This module only guards
  request-scoped routes.
- ``/dav/*`` is left completely alone: WebDAV keeps its own edge-auth until
  Phase 7, so the ``before_request`` handler returns immediately for it.
- Proxy identity headers (``X-Authentik-*`` / ``X-Forwarded-*`` / …) become
  an impersonation backdoor once in-app OIDC exists, so they are stripped
  from the WSGI environ for every non-``/dav`` request in oidc mode. In
  ``off`` mode nothing is stripped (zero behaviour change).
- Enumeration: non-member / missing-project access returns **404**, never
  403, because all IDs are sequential.
"""

import functools
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, redirect, request, session, url_for

import agent_db as db

_log = logging.getLogger(__name__)

try:
    from authlib.integrations.flask_client import OAuth
except Exception:  # authlib missing: oidc mode refuses boot, off mode unaffected
    OAuth = None


# ── Config ────────────────────────────────────────────────────────────────────
# Import-time snapshots (documented defaults). Request-time code always uses
# the live helpers below so tests can flip modes via os.environ.

AINGEL_AUTH = os.environ.get('AINGEL_AUTH', 'off')
AINGEL_SESSION_SECRET = os.environ.get('AINGEL_SESSION_SECRET', '')
AINGEL_OIDC_ISSUER = os.environ.get('AINGEL_OIDC_ISSUER', '')
AINGEL_OIDC_CLIENT_ID = os.environ.get('AINGEL_OIDC_CLIENT_ID', '')
AINGEL_OIDC_CLIENT_SECRET = os.environ.get('AINGEL_OIDC_CLIENT_SECRET', '')


def auth_mode():
    """Live auth mode: 'oidc' when enabled, otherwise 'off'."""
    return os.environ.get('AINGEL_AUTH', 'off').strip().lower()


def auth_enabled():
    """True only in oidc mode. Everything passes through when False."""
    return auth_mode() == 'oidc'


def public_base_url():
    """External base URL for OIDC redirect URIs, or None to derive from request.

    Behind a TLS-terminating tunnel (Cloudflare → cloudflared → Flask over
    plain HTTP) ``url_for(..., _external=True)`` emits ``http://…`` because the
    WSGI scheme is ``http``. Authentik matches redirect URIs strictly, so the
    flow would be rejected. ``AINGEL_PUBLIC_URL`` pins the external origin
    (e.g. ``https://cordee.example``); when unset the request host is
    used as before.
    """
    return os.environ.get('AINGEL_PUBLIC_URL', '').strip().rstrip('/') or None


def _callback_url():
    """Absolute URL Authentik redirects back to after login."""
    base = public_base_url()
    if base:
        return base + '/api/auth/callback'
    return url_for('aingel_auth.callback', _external=True)


def _post_logout_url():
    """Absolute URL to land on after the IdP session ends."""
    base = public_base_url()
    if base:
        return base + '/?loggedout=1'
    return request.host_url.rstrip('/') + '/?loggedout=1'


# ── OIDC client (lazy) ────────────────────────────────────────────────────────
# Registered lazily on first login/callback so that app boot (and unit tests)
# never depend on IdP reachability. Only the missing-secret check refuses boot.

oauth = OAuth() if OAuth is not None else None


def _oidc_client():
    """Return the registered authlib OIDC client, registering it on first use.

    Returns None when authlib is missing or the provider is not configured.
    """
    if oauth is None:
        return None
    try:
        client = oauth.create_client('oidc')
    except Exception:
        client = None
    if client is not None:
        return client
    issuer = os.environ.get('AINGEL_OIDC_ISSUER', '').rstrip('/')
    client_id = os.environ.get('AINGEL_OIDC_CLIENT_ID', '')
    if not issuer or not client_id:
        return None
    try:
        oauth.register(
            'oidc',
            server_metadata_url=issuer + '/.well-known/openid-configuration',
            client_id=client_id,
            client_secret=os.environ.get('AINGEL_OIDC_CLIENT_SECRET', ''),
            client_kwargs={
                'scope': 'openid email profile',
                'code_challenge_method': 'S256',
            },
        )
    except Exception:
        return None
    try:
        return oauth.create_client('oidc')
    except Exception:
        return None


# ── Header stripping ──────────────────────────────────────────────────────────
# Mirrors the AUTH_HEADERS concept from agent_webdav.py (kept as a local copy
# on purpose: this module must not import agent_api or agent_webdav).
_SPOOF_HEADERS = (
    'X-Authentik-Username',
    'X-Authentik-User',
    'X-Forwarded-User',
    'X-Forwarded-Email',
    'X-Authentik-Email',
    'Remote-User',
    'Cf-Access-Authenticated-User-Email',
    'X-Forwarded-For',
)


def _strip_spoof_headers(environ):
    """Delete proxy identity headers from the WSGI environ (in place)."""
    for header in _SPOOF_HEADERS:
        environ.pop('HTTP_' + header.upper().replace('-', '_'), None)


# ── before_request ────────────────────────────────────────────────────────────

def _auth_before_request():
    """Session resolution + header hygiene for every request.

    (i)   ``/dav*`` returns immediately — WebDAV keeps its own edge-auth
          until Phase 7.
    (ii)  Strip spoofable proxy identity headers (oidc mode only; in off
          mode nothing is stripped so behaviour is bit-for-bit unchanged).
    (iii) Resolve ``session['uid']`` → ``g.current_user``; missing users and
          non-``active`` (suspended) users have their session cleared.
    """
    if request.path.startswith('/dav'):
        return None
    if auth_enabled():
        try:
            _strip_spoof_headers(request.environ)
        except Exception:
            pass
    else:
        g.current_user = None
        return None
    uid = session.get('uid')
    if not uid:
        g.current_user = None
        return None
    try:
        user = db.get_user(uid)
    except Exception:
        user = None
    if not user or user.get('status') != 'active':
        session.pop('uid', None)
        g.current_user = None
        return None
    g.current_user = user
    return None


# ── Decorators ────────────────────────────────────────────────────────────────

def require_auth(fn):
    """401 JSON when anonymous. Pass-through when auth is off."""
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        if not auth_enabled():
            return fn(*args, **kwargs)
        if g.get('current_user') is None:
            return jsonify(error='auth_required'), 401
        return fn(*args, **kwargs)
    return _wrapper


def require_admin(fn):
    """401 when anonymous, 404 when authenticated but not a global admin.

    404 (not 403) on purpose: role existence must not be enumerable.
    Pass-through when auth is off.
    """
    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        if not auth_enabled():
            return fn(*args, **kwargs)
        user = g.get('current_user')
        if user is None:
            return jsonify(error='auth_required'), 401
        if user.get('role') != 'admin':
            return jsonify(error='not_found'), 404
        return fn(*args, **kwargs)
    return _wrapper


_ROLE_RANK = {'viewer': 1, 'member': 2, 'admin': 3, 'owner': 4}


def _role_satisfies(role, min_role):
    """True when `role` ranks at or above `min_role`. Unknown roles fail."""
    return _ROLE_RANK.get(role, 0) >= _ROLE_RANK.get(min_role, 0)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ProjectAccessMismatch(Exception):
    """Raised when a request references ids belonging to different projects.

    Callers must deny (403) rather than trust any single id: otherwise a client
    could pair their own ``project_id`` with another tenant's ``task_id`` /
    ``exec_id`` / ``chat_id`` and pass the membership check.
    """


def _ref_project_id(ref_type, ref):
    """Project id of a referenced task/execution/chat, or None."""
    lookup = {
        'task_id': db.get_task,
        'exec_id': db.get_execution,
        'chat_id': db.get_chat,
    }
    fn = lookup.get(ref_type)
    if fn is None:
        return None
    try:
        row = fn(ref)
    except Exception:
        row = None
    if not row:
        return None
    return _to_int(row.get('project_id'))


def _resolve_pid():
    """Resolve the project id for the current request, or None.

    Every id in the request (view args, query string, JSON body) is resolved,
    not just the first one found. If they point at different projects the
    request is rejected via :class:`ProjectAccessMismatch`, so a caller cannot
    pass their own ``project_id`` alongside another tenant's ``task_id`` /
    ``exec_id`` / ``chat_id``.
    """
    found = set()

    def _add(value):
        v = _to_int(value)
        if v is not None:
            found.add(v)

    view_args = request.view_args or {}
    _add(view_args.get('pid', view_args.get('project_id')))
    _add(_ref_project_id('task_id', _to_int(view_args.get('tid'))))
    _add(_ref_project_id('exec_id', _to_int(view_args.get('exec_id'))))
    _add(_ref_project_id('chat_id', _to_int(view_args.get('cid'))))

    args = request.args or {}
    _add(args.get('pid', args.get('project_id')))
    for key in ('task_id', 'exec_id', 'chat_id'):
        ref = _to_int(args.get(key))
        if ref is not None:
            _add(_ref_project_id(key, ref))

    try:
        body = request.get_json(silent=True) or {}
    except Exception:
        body = {}
    if isinstance(body, dict):
        _add(body.get('project_id'))
        for key in ('task_id', 'exec_id', 'chat_id'):
            ref = _to_int(body.get(key))
            if ref is not None:
                _add(_ref_project_id(key, ref))

    if not found:
        return None
    if len(found) > 1:
        raise ProjectAccessMismatch()
    return next(iter(found))


def _effective_role(project, user):
    """Owner when global admin or ``projects.owner_id``; else membership role.

    Returns None when the user has no access to the project.
    """
    if user.get('role') == 'admin':
        return 'owner'
    try:
        if project.get('owner_id') is not None and project.get('owner_id') == user.get('id'):
            return 'owner'
    except Exception:
        pass
    try:
        return db.get_user_role(project.get('id'), user.get('id'))
    except Exception:
        return None


def require_project_access(min_role='viewer'):
    """Project membership gate with viewer < member < admin < owner.

    - Anonymous → 401.
    - Missing project OR no membership → 404 (anti-enumeration).
    - Membership below ``min_role`` → 403.
    - Global admins bypass membership (treated as owner).
    - Pass-through when auth is off.
    """
    if min_role not in _ROLE_RANK:
        raise ValueError(f"invalid min_role: {min_role!r}")

    def _decorator(fn):
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            if not auth_enabled():
                return fn(*args, **kwargs)
            user = g.get('current_user')
            if user is None:
                return jsonify(error='auth_required'), 401
            try:
                pid = _resolve_pid()
            except ProjectAccessMismatch:
                return jsonify(error='forbidden'), 403
            if pid is None:
                return jsonify(error='not_found'), 404
            try:
                project = db.get_project(pid)
            except Exception:
                project = None
            if not project:
                return jsonify(error='not_found'), 404
            role = _effective_role(project, user)
            if role is None:
                return jsonify(error='not_found'), 404
            if not _role_satisfies(role, min_role):
                return jsonify(error='forbidden'), 403
            return fn(*args, **kwargs)
        return _wrapper
    return _decorator


def get_user_project_ids(user):
    """All project ids where `user` is owner or member.

    Returns None for global admins (callers treat None as "no filtering").
    An unknown/anonymous user gets an empty set.
    """
    if not user:
        return set()
    if user.get('role') == 'admin':
        return None
    uid = user.get('id')
    ids = set()
    try:
        for p in db.get_user_projects(uid):
            pid = _to_int(p.get('id'))
            if pid is not None:
                ids.add(pid)
    except Exception:
        pass
    try:
        conn = db.get_db()
        try:
            rows = conn.execute(
                'SELECT id FROM projects WHERE owner_id=?', (uid,)).fetchall()
            for r in rows:
                pid = _to_int(r['id'])
                if pid is not None:
                    ids.add(pid)
        finally:
            conn.close()
    except Exception:
        pass
    return ids


def has_project_access(user, pid, min_role='viewer'):
    """Effective role string when `user` may access `pid` at `min_role`.

    Returns the role ('owner' for global admins and owner_id matches, else
    the membership role) when its rank suffices, else None. Mirrors
    ``require_project_access`` without the HTTP layer.
    """
    if min_role not in _ROLE_RANK:
        raise ValueError(f"invalid min_role: {min_role!r}")
    if not user:
        return None
    pid = _to_int(pid)
    if pid is None:
        return None
    try:
        project = db.get_project(pid)
    except Exception:
        project = None
    if not project:
        return None
    role = _effective_role(project, user)
    if role is None or not _role_satisfies(role, min_role):
        return None
    return role


# ── Blueprint: auth routes + user API ─────────────────────────────────────────

auth_bp = Blueprint('aingel_auth', __name__)


@auth_bp.route('/api/health', methods=['GET'])
def health():
    """Public liveness probe (also reports the auth mode)."""
    return jsonify(ok=True, auth=auth_mode())


@auth_bp.route('/api/auth/login', methods=['GET'])
def login():
    """Start the OIDC authorization-code flow (state + nonce + PKCE S256)."""
    if not auth_enabled():
        return jsonify(error='auth_disabled'), 400
    client = _oidc_client()
    if client is None:
        return jsonify(error='oidc_not_configured'), 500
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    session['oidc_state'] = state
    session['oidc_nonce'] = nonce
    redirect_uri = _callback_url()
    try:
        return client.authorize_redirect(
            redirect_uri, state=state, nonce=nonce)
    except Exception:
        _log.exception('OIDC authorize redirect failed')
        session.pop('oidc_state', None)
        session.pop('oidc_nonce', None)
        return jsonify(error='oidc_unavailable'), 502


_OIDC_TEMP_PREFIXES = ('oidc_', '_state_', '_nonce_', '_verifier_')


def _clear_oidc_temp_keys():
    for key in list(session.keys()):
        if key.startswith(_OIDC_TEMP_PREFIXES):
            session.pop(key, None)


@auth_bp.route('/api/auth/callback', methods=['GET'])
def callback():
    """OIDC redirect target: validate state, exchange code, provision user."""
    if not auth_enabled():
        return jsonify(error='auth_disabled'), 400
    client = _oidc_client()
    if client is None:
        return jsonify(error='oidc_not_configured'), 500
    expected = session.get('oidc_state')
    got = request.args.get('state', '')
    if not expected or not hmac.compare_digest(str(expected), str(got)):
        return jsonify(error='invalid_state'), 400
    nonce = session.get('oidc_nonce')
    try:
        token = client.authorize_access_token()
    except Exception:
        _log.exception('OIDC token exchange failed')
        _clear_oidc_temp_keys()
        return jsonify(error='token_exchange_failed'), 400
    try:
        claims = dict(client.parse_id_token(token, nonce=nonce) or {})
    except Exception:
        _log.exception('OIDC id_token validation failed')
        _clear_oidc_temp_keys()
        return jsonify(error='invalid_id_token'), 400
    try:
        userinfo = dict(client.userinfo(token=token) or {})
    except Exception:
        userinfo = {}
    sub = claims.get('sub') or userinfo.get('sub')
    email = claims.get('email') or userinfo.get('email') or ''
    name = claims.get('name') or userinfo.get('name') or ''
    if not sub:
        _clear_oidc_temp_keys()
        return jsonify(error='invalid_id_token'), 400
    try:
        user = db.get_or_create_user(sub, email, name)
    except Exception:
        _clear_oidc_temp_keys()
        return jsonify(error='provisioning_failed'), 500
    if user.get('status') != 'active':
        _clear_oidc_temp_keys()
        return jsonify(error='suspended'), 403
    session['uid'] = user['id']
    _clear_oidc_temp_keys()
    return redirect('/')


@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    """Clear the AIngel session and return the IdP end-session URL so the
    frontend can also terminate the Authentik SSO session.

    Without ending the IdP session, the 401→login auto-redirect would
    silently re-authenticate the user via the still-live SSO session,
    making logout appear to do nothing.
    """
    session.clear()
    end_session_url = None
    issuer = os.environ.get('AINGEL_OIDC_ISSUER', '').rstrip('/')
    if issuer and auth_enabled():
        import urllib.parse
        redirect = _post_logout_url()
        end_session_url = issuer + '/end-session/?' + urllib.parse.urlencode({
            'post_logout_redirect_uri': redirect,
        })
    return jsonify(ok=True, end_session_url=end_session_url)


def _current_month():
    return datetime.now(timezone.utc).strftime('%Y-%m')


@auth_bp.route('/api/me', methods=['GET'])
@require_auth
def me():
    """Current user + this month's usage counters + quota status."""
    user = g.current_user
    if user is None:
        return jsonify(user=None, usage=None, month=_current_month(), quotas=None)
    month = _current_month()
    try:
        usage = db.get_usage(user['id'], month)
    except Exception:
        usage = {'user_id': user['id'], 'month': month, 'runs': 0,
                 'storage_bytes': 0, 'tokens_in': 0, 'tokens_out': 0,
                 'gpu_minutes': 0}
    quotas = None
    try:
        import agent_quotas
        quotas = agent_quotas.get_quota_status(user['id'])
    except Exception:
        pass
    return jsonify(user=user, usage=usage, month=month, quotas=quotas)


@auth_bp.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_list_users():
    return jsonify(users=db.list_users())


def _directory_row(u):
    return {'id': u.get('id'), 'name': u.get('name') or '',
            'email': u.get('email') or '', 'role': u.get('role') or 'user'}


@auth_bp.route('/api/users', methods=['GET'])
@require_auth
def list_user_directory():
    """Minimal user directory for picking members by name.

    Global admins see every user. Everyone else sees only the users they share
    a project with (plus themselves), so a free user cannot enumerate the whole
    user base. Members of a project can already see each other's names via
    GET /api/projects/<pid>/members.
    """
    me = g.current_user
    if not me:
        return jsonify(users=[])
    if me.get('role') == 'admin':
        return jsonify(users=[_directory_row(u) for u in db.list_users()])
    seen = {me.get('id')}
    rows = [_directory_row(me)]
    try:
        # Same visibility set as project listing: owner_id OR a membership row.
        for pid in (get_user_project_ids(me) or set()):
            for m in db.get_project_members(pid):
                uid = m.get('user_id')
                if uid is None or uid in seen:
                    continue
                seen.add(uid)
                rows.append({'id': uid, 'name': m.get('name') or '',
                             'email': m.get('email') or '', 'role': 'user'})
    except Exception:
        pass
    rows.sort(key=lambda r: (r.get('name') or r.get('email') or '').lower())
    return jsonify(users=rows)


_USER_ROLE_VALUES = ('user', 'admin')


@auth_bp.route('/api/admin/users/<int:user_id>', methods=['PUT'])
@require_admin
def admin_update_user(user_id):
    """Flip plan / status (and role) for one user.

    Forbids self-suspend and self-demote with 400 ``cannot_modify_self``.
    """
    target = db.get_user(user_id)
    if not target:
        return jsonify(error='not_found'), 404
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='invalid_body'), 400
    me_user = g.current_user
    is_self = (me_user.get('id') == user_id)
    if 'status' in data:
        if data['status'] not in ('active', 'suspended'):
            return jsonify(error='invalid_status'), 400
        if is_self and data['status'] != 'active':
            return jsonify(error='cannot_modify_self'), 400
    if 'plan' in data:
        if data['plan'] not in ('free', 'paid'):
            return jsonify(error='invalid_plan'), 400
    if 'role' in data:
        if data['role'] not in _USER_ROLE_VALUES:
            return jsonify(error='invalid_role'), 400
        if is_self and me_user.get('role') == 'admin' and data['role'] != 'admin':
            return jsonify(error='cannot_modify_self'), 400
    try:
        if 'plan' in data:
            db.update_user_plan(user_id, data['plan'])
        if 'status' in data:
            db.update_user_status(user_id, data['status'])
        if 'role' in data:
            conn = db.get_db()
            try:
                conn.execute('UPDATE users SET role=? WHERE id=?',
                             (data['role'], user_id))
                conn.commit()
            finally:
                conn.close()
    except ValueError:
        return jsonify(error='invalid_value'), 400
    return jsonify(user=db.get_user(user_id))


@auth_bp.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@require_admin
def admin_delete_user(user_id):
    """Delete a user account (admin only).

    Forbids deleting yourself. Refuses to delete the last admin
    (``last_admin``). Projects the user owned are left ownerless rather than
    deleted; their memberships and usage counters are removed.
    """
    if g.current_user and g.current_user.get('id') == user_id:
        return jsonify(error='cannot_modify_self'), 400
    if not db.get_user(user_id):
        return jsonify(error='not_found'), 404
    try:
        orphaned = db.delete_user(user_id)
    except ValueError as e:
        if str(e) == 'last_admin':
            return jsonify(error='last_admin'), 400
        return jsonify(error='invalid_value'), 400
    return jsonify(ok=True, orphaned_projects=orphaned or 0)


def _owner_count(members):
    return sum(1 for m in members if m.get('role') == 'owner')


def _sync_project_owner(pid):
    """Keep projects.owner_id aligned with membership ownership.

    owner_id is the single canonical owner for quotas, storage stock and
    display; membership roles are the access-control truth. After any
    membership change: if the current owner_id has no owner membership left
    (demoted, removed, or never a member), transfer owner_id to a member
    that does hold the owner role. No-op when ownership is already
    consistent (user report 2026-10-02: transfer in the Members modal
    changed the role but left owner_id on the previous owner).
    """
    try:
        proj = db.get_project(pid) or {}
        cur = proj.get('owner_id')
        members = db.get_project_members(pid)
        owners = [m['user_id'] for m in members if m.get('role') == 'owner']
        if not owners:
            return  # last_owner guard upstream; never orphan owner_id here
        if cur in owners:
            return
        db.set_project_owner(pid, owners[0])
    except Exception:
        logging.getLogger(__name__).warning(
            'owner sync failed for project %s', pid, exc_info=True)


@auth_bp.route('/api/projects/<int:pid>/members', methods=['GET'])
@require_project_access('viewer')
def list_members(pid):
    return jsonify(members=db.get_project_members(pid))


@auth_bp.route('/api/projects/<int:pid>/members', methods=['POST'])
@require_project_access('admin')
def add_member(pid):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='invalid_body'), 400
    user_id = _to_int(data.get('user_id'))
    role = data.get('role')
    if user_id is None or not role:
        return jsonify(error='missing_user_id_or_role'), 400
    if role not in ('owner', 'admin', 'member', 'viewer'):
        return jsonify(error='invalid_role'), 400
    if not db.get_user(user_id):
        return jsonify(error='not_found'), 404
    try:
        db.add_project_member(pid, user_id, role)
    except ValueError:
        return jsonify(error='invalid_role'), 400
    _sync_project_owner(pid)
    return jsonify(ok=True, members=db.get_project_members(pid))


@auth_bp.route('/api/projects/<int:pid>/members/<int:uid>', methods=['PUT'])
@require_project_access('admin')
def update_member(pid, uid):
    """Change a member's role. Demoting the last owner → 400 ``last_owner``."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error='invalid_body'), 400
    role = data.get('role')
    if role not in ('owner', 'admin', 'member', 'viewer'):
        return jsonify(error='invalid_role'), 400
    members = db.get_project_members(pid)
    current = next((m for m in members if m.get('user_id') == uid), None)
    if current is None:
        return jsonify(error='not_found'), 404
    if (current.get('role') == 'owner' and role != 'owner'
            and _owner_count(members) <= 1):
        return jsonify(error='last_owner'), 400
    try:
        db.add_project_member(pid, uid, role)
    except ValueError:
        return jsonify(error='invalid_role'), 400
    _sync_project_owner(pid)
    return jsonify(ok=True, members=db.get_project_members(pid))


@auth_bp.route('/api/projects/<int:pid>/members/<int:uid>', methods=['DELETE'])
@require_project_access('admin')
def remove_member(pid, uid):
    """Remove a member. Removing the last owner → 400 ``last_owner``."""
    members = db.get_project_members(pid)
    current = next((m for m in members if m.get('user_id') == uid), None)
    if current is None:
        return jsonify(error='not_found'), 404
    if current.get('role') == 'owner' and _owner_count(members) <= 1:
        return jsonify(error='last_owner'), 400
    db.remove_project_member(pid, uid)
    _sync_project_owner(pid)
    return jsonify(ok=True, members=db.get_project_members(pid))


# ── init ──────────────────────────────────────────────────────────────────────

def init_auth(app):
    """Wire auth into a Flask app. No-op-safe when ``AINGEL_AUTH=off``.

    - Sets ``app.secret_key`` (env value; random per-boot when off and unset —
      sessions are unused in off mode).
    - Refuses boot with RuntimeError in oidc mode without
      ``AINGEL_SESSION_SECRET``.
    - ``Secure`` + ``HttpOnly`` + ``SameSite=Lax`` session cookies (Lax, not
      Strict — Strict breaks the Authentik cross-site redirect callback).
    - Registers the ``before_request`` handler and the auth Blueprint.
    """
    secret = os.environ.get('AINGEL_SESSION_SECRET', '')
    if auth_enabled() and not secret:
        raise RuntimeError(
            'AINGEL_AUTH=oidc requires AINGEL_SESSION_SECRET to be set '
            '(refusing to boot without a session key).')
    app.secret_key = secret if secret else os.urandom(32)
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    if oauth is not None:
        try:
            oauth.init_app(app)
        except Exception:
            pass
    app.before_request(_auth_before_request)
    app.register_blueprint(auth_bp)
    return app
