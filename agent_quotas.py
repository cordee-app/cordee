"""AIngel multi-tenancy — Phase 4: quota enforcement.

Free-tier resource limits enforced at every spend point: project creation,
task runs, token consumption, file uploads, and feature access (RAG, GPU,
autopilot, batch, briefs).

Design:
- Admins bypass all quotas.
- ``AINGEL_AUTH=off`` bypasses all quotas (single-user mode unchanged).
- Tokens are metered inside ``route()`` (agent_router.py) — the single choke
  point every LLM call funnels through.  This catches task runs, chat replies,
  H2/H3/H4 overseer calls, briefs, scaffold, improve-prompt, batch chunks,
  compaction — everything.
- Pre-flight token check in ``run_task()`` and ``chat_reply()`` blocks a
  run before it spends provider credits when the budget is exhausted.
- Runs are counted at ``create_execution`` — only user-initiated task runs,
  not internal H2/H3/H4 calls (those use ``caller`` to distinguish).
- All increments are atomic conditional UPDATEs via ``check_and_increment_usage``.
- Background threads resolve owner identity from ``projects.owner_id`` (durable
  DB identity, not Flask ``g``).
"""

import os
from datetime import datetime, timezone

import agent_db as db

# ── Free-tier limits ─────────────────────────────────────────────────────────

FREE_TIER_LIMITS = {
    'max_projects': 1,
    'max_runs': 20,
    # in + out combined per month. Env-overridable so an operator can raise
    # the cap for a specific deployment (e.g. testing) without a code change;
    # unset → the product default of 100k.
    'max_tokens': int(os.getenv('AINGEL_FREE_MAX_TOKENS', '100000')),
    'max_storage': 50 * 1024 * 1024,  # 50 MB
    'max_eu_data': 50 * 1024 * 1024,  # 50 MB (deferred — needs design call)
}

# Models allowed on the free tier (prefix match).
_FREE_MODEL_PREFIXES = (
    'mistral-',
    'open-mistral',
    'codestral-',
    'devstral-',
    'scw-',
    'oll-',
)

# scw-dep-* = dedicated GPU deployments — billed hourly, not free-tier.
_FREE_MODEL_EXCLUDE_PREFIXES = (
    'scw-dep-',
)


class QuotaError(Exception):
    """Raised when a free-tier user exceeds a quota.

    Attributes:
        field: which counter (runs, tokens_in, tokens_out, storage_bytes, projects)
        limit: the limit that was hit
        current: the current usage (best-effort, may be None)
    """

    def __init__(self, message, *, field=None, limit=None, current=None):
        super().__init__(message)
        self.field = field
        self.limit = limit
        self.current = current


# ── Helpers ──────────────────────────────────────────────────────────────────

def _current_month():
    return datetime.now(timezone.utc).strftime('%Y-%m')


def is_free_tier(user_id):
    """True when user_id is on the free plan. Admins are never free-tier."""
    if not user_id:
        return False
    user = db.get_user(user_id)
    if not user:
        return False
    if user.get('role') == 'admin':
        return False
    return user.get('plan') == 'free'


def quotas_applicable(user_id):
    """True when quotas should be enforced for this user.

    Returns False when auth is off or the user is an admin.
    """
    if not os.environ.get('AINGEL_AUTH', 'off') == 'oidc':
        return False
    if not user_id:
        return False
    return is_free_tier(user_id)


def get_limits(user_id):
    """Return the limits dict for the user's plan."""
    if is_free_tier(user_id):
        return dict(FREE_TIER_LIMITS)
    return {}


def get_usage(user_id):
    """Return current usage dict for the user.

    Runs/tokens are monthly flow counters; storage is the LIVE disk stock of
    the projects the user owns (a monthly counter resets while files persist,
    letting a free user accumulate +limit every month — user report
    2026-10-01). Stock is never incremented or decremented; it is measured.
    """
    usage = dict(db.get_usage(user_id, _current_month()) or {})
    usage['storage_bytes'] = storage_stock_bytes(user_id)
    return usage


def get_quota_status(user_id):
    """Return a dict with limits + current usage for the frontend."""
    if not quotas_applicable(user_id):
        return None
    limits = get_limits(user_id)
    usage = get_usage(user_id)
    return {
        'plan': 'free',
        'limits': {
            'max_projects': limits['max_projects'],
            'max_runs': limits['max_runs'],
            'max_tokens': limits['max_tokens'],
            'max_storage': limits['max_storage'],
        },
        'usage': {
            'runs': usage.get('runs', 0),
            'tokens_in': usage.get('tokens_in', 0),
            'tokens_out': usage.get('tokens_out', 0),
            'storage_bytes': usage.get('storage_bytes', 0),
            'tokens_total': (usage.get('tokens_in', 0) + usage.get('tokens_out', 0)),
        },
    }


# ── Resolution for background threads ────────────────────────────────────────

def resolve_owner_id(task, proj):
    """Resolve the user_id for quota enforcement in background threads.

    Priority: acting user (task.created_by) → project.owner_id.
    Actor-first so a free-tier member running on a shared project is always
    metered — even when the project is owned by an admin, whose
    ``quotas_applicable()`` is False (owner-first would silently disable both
    the run/token counters and the model whitelist for that member). The
    project owner is the fallback for rows with no recorded author
    (background daemons, pre-multi-tenancy tasks).
    Returns None when neither is set.
    """
    if task and task.get('created_by'):
        return task['created_by']
    if proj and proj.get('owner_id'):
        return proj['owner_id']
    return None


import time as _time

_PATH_OWNER_CACHE = {}
_PATH_OWNER_TTL = 30  # seconds — batch chunks reuse the cached lookup


def _resolve_owner_from_path(project_path):
    """Resolve the quota owner from a project path alone (owner fallback).

    Used by ``resolve_owner_for_call`` only when no acting execution/chat
    author is available (background daemons, brief/H2 sub-calls). Resolution
    order there is actor-first, owner-fallback; a project owned by an admin
    would otherwise bypass the free tier for every member.

    Short-lived cache (30s) avoids a DB lookup per route() call when batch
    mode calls route() in a tight loop with the same project_path.
    """
    if not project_path:
        return None
    now = _time.monotonic()
    cached = _PATH_OWNER_CACHE.get(project_path)
    if cached and now - cached[1] < _PATH_OWNER_TTL:
        return cached[0]
    try:
        proj = db.get_project_by_path(project_path)
        owner = proj.get('owner_id') if proj else None
    except Exception:
        owner = None
    _PATH_OWNER_CACHE[project_path] = (owner, now)
    if len(_PATH_OWNER_CACHE) > 200:
        _PATH_OWNER_CACHE.clear()
    return owner


def resolve_owner_for_call(project_path, exec_id=None, chat_id=None):
    """Owner for a route() call: execution author → chat author → path owner.

    Actor-first, matching ``resolve_owner_id``: the user who initiated the
    execution/chat is the one metered, so a free-tier member is enforced even
    on a project owned by an admin. The project owner is only a fallback when
    no acting author is recorded (background daemons, legacy rows). This also
    keeps legacy ownerless projects metering the acting member.
    """
    if exec_id:
        key = ('exec', project_path, exec_id)
        now = _time.monotonic()
        cached = _PATH_OWNER_CACHE.get(key)
        if cached and now - cached[1] < _PATH_OWNER_TTL:
            owner = cached[0]
        else:
            try:
                row = db.get_execution(exec_id)
                owner = (row or {}).get('created_by')
            except Exception:
                owner = None
            _PATH_OWNER_CACHE[key] = (owner, now)
        if owner:
            return owner
    if chat_id:
        key = ('chat', project_path, chat_id)
        now = _time.monotonic()
        cached = _PATH_OWNER_CACHE.get(key)
        if cached and now - cached[1] < _PATH_OWNER_TTL:
            owner = cached[0]
        else:
            try:
                row = db.get_chat(chat_id, project_path) if project_path else db.get_chat(chat_id)
                owner = (row or {}).get('created_by')
            except Exception:
                owner = None
            _PATH_OWNER_CACHE[key] = (owner, now)
        if owner:
            return owner
    return _resolve_owner_from_path(project_path)


# ── Quota checks ─────────────────────────────────────────────────────────────

def check_project_create(user_id):
    """Raise QuotaError if user is at or over the project limit."""
    if not quotas_applicable(user_id):
        return
    projects = db.get_user_projects(user_id)
    limit = FREE_TIER_LIMITS['max_projects']
    if len(projects) >= limit:
        raise QuotaError(
            f'Free tier allows up to {limit} project(s). You currently have {len(projects)}.',
            field='projects', limit=limit, current=len(projects),
        )


def check_and_increment_run(user_id):
    """Atomically increment the runs counter for this month.

    Returns silently on success. Raises QuotaError when over limit.
    Skips silently when quotas don't apply (admin, auth off, no user).
    """
    if not quotas_applicable(user_id):
        return
    month = _current_month()
    limit = FREE_TIER_LIMITS['max_runs']
    ok = db.check_and_increment_usage(user_id, month, 'runs', 1, limit)
    if not ok:
        current = db.get_usage(user_id, month).get('runs', 0)
        raise QuotaError(
            f'Free tier allows {limit} runs per month. You have used {current}.',
            field='runs', limit=limit, current=current,
        )


def check_token_budget(user_id):
    """Pre-flight check: raise QuotaError if the user is already at or over
    the combined token limit for this month.

    Called before route() to prevent a user whose token budget is exhausted
    from launching new runs/chats that would spend real provider credits
    without being metered.
    """
    if not quotas_applicable(user_id):
        return
    month = _current_month()
    limit = FREE_TIER_LIMITS['max_tokens']
    usage = db.get_usage(user_id, month)
    total = (usage.get('tokens_in', 0) or 0) + (usage.get('tokens_out', 0) or 0)
    if total >= limit:
        raise QuotaError(
            f'Free tier allows {limit:,} tokens per month (in + out). '
            f'You have used {total:,}.',
            field='tokens', limit=limit, current=total,
        )


def record_tokens(user_id, tok_in, tok_out):
    """Increment token counters after a route() call.

    Always records the actual usage. The provider call has already been made
    and paid for, so refusing to count it once the increment would cross the
    limit (the previous behaviour) let a user sitting near the cap keep
    spending forever while their counter stayed frozen below the limit. The
    limit itself is enforced by ``check_token_budget`` *before* the call.

    Silently skips when quotas don't apply or tokens are zero.
    """
    if not quotas_applicable(user_id):
        return
    tok_in = int(tok_in or 0)
    tok_out = int(tok_out or 0)
    if tok_in == 0 and tok_out == 0:
        return
    month = _current_month()

    conn = db.get_db()
    try:
        try:
            conn.execute('BEGIN IMMEDIATE')
        except Exception:
            pass
        conn.execute(
            'INSERT OR IGNORE INTO usage_counters (user_id, month) VALUES (?,?)',
            (user_id, month))
        conn.execute(
            'UPDATE usage_counters '
            'SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ? '
            'WHERE user_id = ? AND month = ?',
            (tok_in, tok_out, user_id, month))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


_STOCK_CACHE = {}
_STOCK_CACHE_TTL = 15.0  # seconds — /api/me polls every 30s; du-walk is cheap

# Transient service dirs never count toward a user's stock: staging chunks
# (.uploads, incl. chunks of a rejected upload) and soft-deleted files
# awaiting the 7-day reaper (.trash) are not usable files, and the Files UI
# can't even show them — counting them would lock an owner out with no way
# to free the space.
_STOCK_SKIP_DIRS = frozenset({'.uploads', '.trash'})


def storage_stock_bytes(user_id):
    """Actual on-disk bytes across all projects owned by ``user_id``.

    Walks every project whose ``owner_id`` matches and sums file sizes
    (du-style; transient service dirs are skipped — see ``_STOCK_SKIP_DIRS``).
    Short-TTL cache so repeated /api/me + upload gates don't re-walk large
    trees on every call.
    """
    import os as _os
    now = _time.time()
    hit = _STOCK_CACHE.get(user_id)
    if hit is not None and now - hit[0] < _STOCK_CACHE_TTL:
        return hit[1]
    total = 0
    try:
        for p in db.get_projects():
            if p.get('owner_id') != user_id:
                continue
            path = p.get('path') or ''
            if not path or not _os.path.isdir(path):
                continue
            for root, dirs, files in _os.walk(path):
                dirs[:] = [d for d in dirs if d not in _STOCK_SKIP_DIRS]
                for f in files:
                    try:
                        total += _os.lstat(_os.path.join(root, f)).st_size
                    except OSError:
                        pass
    except Exception:
        pass
    _STOCK_CACHE[user_id] = (now, total)
    return total


def check_storage(user_id, size_bytes):
    """Gate an upload of ``size_bytes`` against live disk stock.

    Storage is stock-based (not a monthly counter): the check compares the
    current measured stock plus the incoming bytes against the limit. No
    increment — the stock recomputes from disk after the write, and deletes
    free it automatically. ``user_id`` is the quota subject: callers pass the
    PROJECT OWNER, because uploaded bytes land in and persist against the
    owner's project.
    """
    if not quotas_applicable(user_id):
        return
    size_bytes = int(size_bytes or 0)
    if size_bytes <= 0:
        return
    limit = FREE_TIER_LIMITS['max_storage']
    current = storage_stock_bytes(user_id)
    if current + size_bytes > limit:
        raise QuotaError(
            f'Free tier allows {limit // (1024*1024)} MB storage. '
            f'You are currently using {current // (1024*1024)} MB.',
            field='storage_bytes', limit=limit, current=current,
        )


# ── Feature gating ────────────────────────────────────────────────────────────

def is_allowed_model(model_id, user_id):
    """True when a free-tier user is allowed to use this model.

    Free tier: mistral-*, scw-* (excl. scw-dep-*), oll-*.
    """
    if not quotas_applicable(user_id):
        return True
    return _is_free_model(model_id)


def _is_free_model(model_id):
    """Check model_id against the free-tier whitelist (prefix-based)."""
    m = model_id or ''
    for ex in _FREE_MODEL_EXCLUDE_PREFIXES:
        if m.startswith(ex):
            return False
    for pref in _FREE_MODEL_PREFIXES:
        if m.startswith(pref):
            return True
    return False


def can_use_autopilot(user_id):
    """True when the user can use AIngel autopilot (H2/H3/H4)."""
    return not quotas_applicable(user_id)


def can_use_batch(user_id):
    """True when the user can use batch/transform mode."""
    return not quotas_applicable(user_id)


def can_use_briefs(user_id):
    """True when the user gets auto-generated briefs."""
    return not quotas_applicable(user_id)


def can_use_rag(user_id):
    """True when the user can use RAG (prefetch + rag_query tool)."""
    return not quotas_applicable(user_id)


def can_use_gpu(user_id):
    """True when the user can access GPU endpoints."""
    return not quotas_applicable(user_id)
