"""AIngel Terms & Conditions — versioning + acceptance gate.

Users must accept the current Terms & Conditions version before they can use
the product. This module owns the request-scoped enforcement; the storage lives
in ``agent_db`` (``terms_versions`` / ``terms_acceptances`` + two ``app_meta``
keys) and the go-live step is the CLI ``ops/publish_terms.py``.

Contract:

- **Off mode is a no-op.** With ``AINGEL_AUTH=off`` (the default) the gate
  returns immediately — no DB read, no behaviour change. Everything below only
  applies in ``oidc`` mode.
- **Fail open.** Any DB error, a missing pointer, or a pointer that does not
  resolve to an existing version means "no gate". A broken terms table must
  never lock every user out of the product.
- **Scope.** Only ``/api/*`` and ``/files*`` are gated. The app shell
  (``/``, ``/assets/*``, ``/favicon.svg``) is untouched so the frontend can
  boot and render the acceptance screen; ``/dav/*`` (WebDAV) is left alone,
  exactly like ``agent_auth`` does.
- **Exemptions.** ``/api/me`` (exact match only — a prefix match would exempt
  the whole ``/api/memory`` API), ``/api/auth/*`` (login / logout / callback
  must work pre-acceptance), ``/api/terms/*`` (read + accept) and
  ``/api/health``.
- **Background threads are unaffected.** This is a ``before_request`` hook: it
  only guards HTTP requests. Schedulers, chat replies and GPU runners keep
  working (they have no Flask ``g``), same as the auth layer.
- **Versions are immutable once accepted.** ``agent_db.upsert_terms_version``
  refuses to rewrite a version that has acceptances, and the schema refuses to
  delete one. Publishing a new tag is the only way to change the text.

Registration order matters: :func:`register_terms` must be called *after*
``agent_auth.init_auth(app)`` so ``g.current_user`` is already resolved when the
gate runs (Flask runs ``before_request`` handlers in registration order).

This module must never import ``agent_api`` (circular import). It imports
``agent_auth`` at module level, which is safe because ``agent_api`` imports
``agent_auth`` first.
"""

import hashlib
import logging
import os
import re

from flask import Blueprint, g, jsonify, request

import agent_db as db
from agent_auth import auth_enabled, require_auth

_log = logging.getLogger(__name__)

# Repo-root legal/terms — one markdown file per version, named <tag>.<lang>.md
# (e.g. 2026-11.en.md). Seeded into terms_versions at boot.
LEGAL_TERMS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'legal', 'terms')

# <tag>.<lang>.md — tag is the free-form version label (2026-11, 2027-03-01…).
_TERMS_FILENAME_RE = re.compile(r'^([^./\\]+)\.([A-Za-z]{2,8})\.md$')

_EMPTY_STATE = {'required': False, 'version_id': None, 'tag': None,
                'accepted_at': None}


# ── Seeding ───────────────────────────────────────────────────────────────────

def _title_from(content, fallback):
    """First ``# `` heading of the markdown, else the version tag."""
    for line in (content or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('# '):
            return stripped[2:].strip() or fallback
        if stripped:
            break
    return fallback


def seed_terms(directory=None):
    """Load ``legal/terms/*.md`` into terms_versions. Never raises.

    Filename convention ``<tag>.<lang>.md``; anything else is skipped. Rows are
    never deleted — a file removed from the repo keeps its row (and any
    acceptance evidence). Re-running with unchanged content is a no-op;
    changed content only updates a version nobody has accepted yet (see
    ``agent_db.upsert_terms_version``).

    Returns a summary dict ``{'seeded', 'updated', 'skipped', 'errors'}``.
    """
    summary = {'seeded': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
    try:
        folder = directory or LEGAL_TERMS_DIR
        if not os.path.isdir(folder):
            _log.info('terms: no legal/terms directory at %s — nothing to seed',
                      folder)
            return summary
        names = sorted(os.listdir(folder))
    except Exception:
        _log.warning('terms: cannot list %s', LEGAL_TERMS_DIR, exc_info=True)
        summary['errors'] += 1
        return summary

    for name in names:
        match = _TERMS_FILENAME_RE.match(name)
        if not match:
            continue
        tag, lang = match.group(1), match.group(2).lower()
        try:
            with open(os.path.join(folder, name), 'r', encoding='utf-8') as f:
                content = f.read()
            if not content.strip():
                _log.warning('terms: skipping empty file %s', name)
                summary['errors'] += 1
                continue
            content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
            title = _title_from(content, tag)
            before = db.get_terms_version_by_tag(tag, lang, 'all')
            db.upsert_terms_version(tag, lang, 'all', title, content,
                                    content_hash)
            if before is None:
                summary['seeded'] += 1
            elif before.get('content_hash') == content_hash:
                summary['skipped'] += 1
            else:
                # Changed text: either updated (no acceptances) or refused
                # (accepted → immutable). Re-read to tell the two apart.
                after = db.get_terms_version_by_tag(tag, lang, 'all') or {}
                if after.get('content_hash') == content_hash:
                    summary['updated'] += 1
                else:
                    summary['skipped'] += 1
        except Exception:
            _log.warning('terms: failed to seed %s', name, exc_info=True)
            summary['errors'] += 1

    _log.info('terms: seed complete — %d new, %d updated, %d unchanged/'
              'immutable, %d error(s)', summary['seeded'], summary['updated'],
              summary['skipped'], summary['errors'])
    return summary


# ── State helpers ─────────────────────────────────────────────────────────────

def terms_state_for(user):
    """Terms state for one user dict (or None). Never raises.

    ``required`` is True only when the gate is active, a user is known, and
    that user has no acceptance row for the current version. Any DB error is
    reported as "not required" so ``/api/me`` can never 500 because of terms.
    """
    state = dict(_EMPTY_STATE)
    try:
        if user is None:
            return state
        current = db.get_current_terms_version()
        if current is None:
            return state
        state['version_id'] = current['id']
        state['tag'] = current.get('version_tag')
        if str(db.get_app_meta('terms_gate_enabled') or '') != '1':
            return state
        acceptance = db.get_terms_acceptance(user['id'], current['id'])
        if acceptance:
            state['accepted_at'] = acceptance.get('accepted_at')
        else:
            state['required'] = True
        return state
    except Exception:
        _log.warning('terms: state lookup failed', exc_info=True)
        return dict(_EMPTY_STATE)


def _in_scope(path):
    """True for the paths the gate guards: /api/* and /files*."""
    return (path.startswith('/api/')
            or path == '/files'
            or path.startswith('/files/'))


def _is_exempt(path):
    """Paths that stay reachable before accepting the terms."""
    return (path in ('/api/me', '/api/health')
            or path.startswith('/api/auth/')
            or path.startswith('/api/terms/'))


# ── before_request gate ───────────────────────────────────────────────────────

def _terms_gate_before_request():
    """403 ``terms_acceptance_required`` until the current version is accepted.

    Registered by :func:`register_terms` after ``init_auth``. Returns None
    (pass through) in off mode, for anonymous users, for exempt paths, when the
    gate is disabled, and on any DB error (fail open).
    """
    if not auth_enabled():
        return None
    # g.get, never g.current_user: the auth handler returns before setting it
    # for /dav paths, and bare attribute access would 500 WebDAV.
    user = g.get('current_user')
    if user is None:
        return None
    path = request.path
    if not _in_scope(path) or _is_exempt(path):
        return None
    try:
        if not db.terms_gate_active():
            return None
        current = db.get_current_terms_version()
        if current is None:
            return None
        if db.get_terms_acceptance(user['id'], current['id']):
            return None
    except Exception:
        _log.warning('terms: gate check failed, allowing request', exc_info=True)
        return None
    return jsonify({
        'error': 'terms_acceptance_required',
        'terms': {
            'version_id': current['id'],
            'tag': current.get('version_tag'),
            'language': current.get('language'),
        },
    }), 403


# ── Blueprint: terms routes ───────────────────────────────────────────────────

terms_bp = Blueprint('aingel_terms', __name__)


@terms_bp.route('/api/terms/current', methods=['GET'])
@require_auth
def current_terms():
    """The current terms text plus this caller's acceptance timestamp."""
    user = g.get('current_user')
    if user is None:
        return jsonify(error='auth_required'), 401
    current = db.get_current_terms_version()
    if current is None:
        return jsonify(terms=None)
    acceptance = db.get_terms_acceptance(user['id'], current['id'])
    return jsonify(terms={
        'version_id': current['id'],
        'tag': current.get('version_tag'),
        'language': current.get('language'),
        'title': current.get('title') or '',
        'content': current.get('content') or '',
        'content_hash': current.get('content_hash'),
        'created_at': current.get('created_at'),
        'accepted_at': acceptance.get('accepted_at') if acceptance else None,
    })


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@terms_bp.route('/api/terms/accept', methods=['POST'])
@require_auth
def accept_terms():
    """Record acceptance of the current version (idempotent)."""
    # require_auth passes through in off mode, so the anonymous check is here.
    user = g.get('current_user')
    if user is None:
        return jsonify(error='auth_required'), 401
    current = db.get_current_terms_version()
    if current is None:
        return jsonify(error='no_current_terms'), 409
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    version_id = _as_int(data.get('version_id'))
    if version_id != current['id']:
        # The client rendered an older version: tell it what to show instead.
        return jsonify({
            'error': 'terms_version_changed',
            'terms': {
                'version_id': current['id'],
                'tag': current.get('version_tag'),
                'language': current.get('language'),
            },
        }), 409
    row = db.record_terms_acceptance(
        user['id'], current['id'], current.get('language') or 'en')
    return jsonify(ok=True,
                   accepted_at=(row or {}).get('accepted_at'),
                   version_id=current['id'],
                   tag=current.get('version_tag'))


# ── init ──────────────────────────────────────────────────────────────────────

def register_terms(app):
    """Register the gate + the terms Blueprint.

    Must be called AFTER ``agent_auth.init_auth(app)``: Flask runs
    ``before_request`` handlers in registration order, and the gate reads
    ``g.current_user``, which the auth handler sets.
    """
    app.before_request(_terms_gate_before_request)
    app.register_blueprint(terms_bp)
    return app
