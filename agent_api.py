#!/usr/bin/env python3
"""SuperAgent API server — Phase 1"""

import os
import re
import json
import math
import shutil
import sys
import time
import threading
import queue
from datetime import datetime, timezone, timedelta as _timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# slot -> {'thread': threading.Thread|_RESERVED|None, 'started': float}
# Tracks the live run thread per kanban slot so a stale in-memory flag can be
# reconciled against reality (a dead thread no longer blocks the column).
# _RESERVED marks a slot that has been claimed but whose run thread has not yet
# started — treated as "running" so a concurrent kick cannot double-start.
_RESERVED = object()
_running_slots: dict = {}
_running_slots_lock = threading.Lock()
_chat_execution_lock = threading.Lock()
_active_chat_execution = None


def _safe_resolve(project_path, *parts):
    """Join path components and verify the result stays inside project_path.
    Returns the resolved absolute path, or None if it escapes the project.
    """
    target = os.path.realpath(os.path.join(project_path, *parts))
    root = os.path.realpath(project_path)
    if not (target == root or target.startswith(root + os.sep)):
        return None
    return target

# Scaffolding progress: project_id -> queue of dicts
_scaffold_progress_queues: dict[int, queue.Queue] = {}
_scaffold_progress_lock = threading.Lock()

from flask import Flask, request, jsonify, send_from_directory, send_file, abort, Response, g
import agent_db as db
import agent_config
import agent_events
from agent_config import (
    API_PORT, MODELS, DEFAULT_MODEL, PROVIDER_COLORS,
    CLAUDE_CODE_SKIP_PERMISSIONS, INSTANCE_NAME, PROJECTS_ROOT,
    DEFINITION_FILE, resolve_def_filename, get_def_files_for_project,
)
from agent_importer import (
    import_all_projects,
    scaffold_project_folder,
    import_phase_tasks_for_project,
    slugify,
)
from agent_executor import (
    run_task, estimate_cost, chat_reply, estimate_chat_prompt_tokens,
    estimate_task_prompt_tokens, generate_queue_handoff,
    ATTACHMENT_CHAR_CAP, _parse_attachments,
)
from agent_router import route
import agent_memory as mem
import agent_chats as chats_mod
import agent_git as agit
import agent_filecat as fcat
import agent_files
import agent_archive
import agent_scw_session
import agent_scw_deploy
import hf_catalog
try:
    import agent_webdav as _webdav_mod
except Exception as _e:
    _webdav_mod = None
    print(f"[startup] WARNING: agent_webdav not loaded: {_e}")

# Phase 2 (multi-tenancy): auth foundation — session cookies, OIDC login,
# @require_auth / @require_admin / @require_project_access. init_auth(app) is
# a no-op for request handling when AINGEL_AUTH=off (the default).
from agent_auth import (
    init_auth, auth_enabled, get_user_project_ids, has_project_access,
    require_auth, require_admin, require_project_access,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, 'frontend', 'dist')
# static_folder=None: the previous static_folder=BASE_DIR auto-served the whole
# repo at /aingel-vault/<path> — including .env and aingel.db (Plans §11.2
# item 2). Nothing depends on Flask's auto-static: /, /assets/*,
# /favicon.svg, /old/, /agent_dashboard.css|js are all explicit @app.route
# handlers using send_from_directory/send_file, so disabling it is safe.
app = Flask(__name__, static_folder=None)
# Reject oversized request bodies before they are buffered in memory. Large
# files are uploaded in chunks (see /api/projects/<pid>/upload-chunk), so this
# only needs to be comfortably above a chunk, not above the largest file.
app.config['MAX_CONTENT_LENGTH'] = int(getattr(agent_config, 'MAX_CONTENT_LENGTH', 128 * 1024 * 1024))
init_auth(app)

# ── WebDAV (Lane C) ───────────────────────────────────────────────────────
# Mounted at /dav/<slug>/<rel>. See agent_webdav.py for provider docs.
# Deviation: spec suggests 127.0.0.1:8002 + a dav.* host, but we mount on
# the existing 8001 (same Authentik SSO, same process) for simplicity. The
# standalone 8002 mode is still available via `python agent_webdav.py`.
try:
    if _webdav_mod and hasattr(_webdav_mod, "register_webdav"):
        _webdav_mod.register_webdav(app)
        print("[startup] WebDAV mounted at /dav/<slug>/ (agent_webdav)")
except Exception as _e:
    print(f"[startup] WARNING: WebDAV mount failed: {_e}")

MEMORY_COMPACT_MAX_TOKENS = 1200

# ── File browser (read-only, /files → vault-projects) ─────────────────────
# Serves https://<host>/files  (same host, same SSO)
# Root is PROJECTS_ROOT. Hidden: *.db*, .env*, .git, dotfiles
FILES_ROOT = os.path.realpath(PROJECTS_ROOT)
_FORBIDDEN_SUFFIXES = ('.db', '.db-journal', '.db-wal', '.db-shm', '.bak')
_FORBIDDEN_EXACT = {'.env', '.env.example', 'aingel.db'}
_FORBIDDEN_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.claude', '.vibe', 'node_modules'}


def _is_forbidden_path(name: str) -> bool:
    """True if this file/dir should be hidden from listing and blocked from download."""
    low = name.lower()
    if low in _FORBIDDEN_EXACT:
        return True
    if low.startswith('.env'):
        return True
    for suf in _FORBIDDEN_SUFFIXES:
        if low.endswith(suf):
            return True
    # Hide dotfiles / dotdirs (except .gitignore-style that might be useful — we hide those too for safety)
    if name.startswith('.'):
        return True
    if name in _FORBIDDEN_DIRS:
        return True
    return False


def _files_safe_join(subpath: str):
    """Resolve subpath inside FILES_ROOT via realpath; return abspath or None if escapes or forbidden."""
    # Empty subpath → root
    joined = os.path.join(FILES_ROOT, subpath or '')
    real = os.path.realpath(joined)
    if not (real == FILES_ROOT or real.startswith(FILES_ROOT + os.sep)):
        return None
    # Also block direct access to any forbidden file/dir component
    parts = (subpath or '').split('/')
    for part in parts:
        if part and _is_forbidden_path(part):
            return None
    # Check final component itself if it's a file that is forbidden (even if hidden parts check missed suffix)
    if os.path.isfile(real) and _is_forbidden_path(os.path.basename(real)):
        return None
    return real


# ── Static ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    resp = send_from_directory(FRONTEND_DIR, 'index.html')
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    return resp

@app.route('/assets/<path:filename>')
def serve_react_assets(filename):
    return send_from_directory(os.path.join(FRONTEND_DIR, 'assets'), filename)

@app.route('/favicon.svg')
def serve_react_favicon():
    return send_file(os.path.join(FRONTEND_DIR, 'favicon.svg'))

# The legacy /old/ dashboard (agent_dashboard.{html,css,js}) was retired: it
# duplicated the React UI, was untested, and used esc() inside onclick strings
# (injectable). Removed to shrink the attack surface.


# Content types that a browser executes/renders. Serving these inline from the
# app's own origin is stored XSS (an uploaded or model-written .html/.svg runs
# with the app's cookies); force them to download and never sniff.
_ACTIVE_CONTENT_TYPES = frozenset({
    'text/html', 'application/xhtml+xml', 'image/svg+xml',
    'application/xml', 'text/xml',
    'application/javascript', 'text/javascript',
})


def _sniff_text_mimetype(path):
    """Best-effort text detection for extension-less files (task 10001171:
    the model created 'This is a test' with no extension — it served as
    application/octet-stream so browsers downloaded it instead of opening it).
    Returns 'text/plain; charset=utf-8' when the head of the file is valid
    UTF-8/ASCII text, else None. No security change: text/plain is not an
    active content type, and the nosniff + sandbox-CSP hardening below still
    applies. Reads at most 8 KB.
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(8192)
    except OSError:
        return None
    if not head or b'\x00' in head:
        return None
    try:
        head.decode('utf-8')
    except UnicodeDecodeError:
        return None
    # Return bare text/plain — Flask's send_file appends '; charset=utf-8'
    # itself for text types (passing it here would double it).
    return 'text/plain'


def _send_project_file_safely(path, mimetype=None):
    """send_file for user/model-controlled files, hardened against stored XSS.

    Active content types are forced to download; everything gets nosniff. A
    sandboxing CSP is added except for PDFs (Chrome's PDF viewer refuses to
    render inside a sandboxed document, which would break inline previews).
    Extension-less text files are sniffed so they open inline instead of
    downloading as octet-stream.
    """
    import mimetypes
    ctype = (mimetype or mimetypes.guess_type(path)[0] or '').split(';')[0].strip().lower()
    if not ctype:
        sniffed = _sniff_text_mimetype(path)
        if sniffed:
            mimetype = sniffed
            ctype = 'text/plain'
    active = ctype in _ACTIVE_CONTENT_TYPES
    resp = send_file(path, mimetype=mimetype, as_attachment=active,
                     download_name=os.path.basename(path), conditional=True)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    if ctype != 'application/pdf':
        resp.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return resp


@app.route('/files')
@app.route('/files/')
@app.route('/files/<path:subpath>')
@require_auth
def files_browser(subpath=''):
    """Read-only browser for PROJECTS_ROOT. HTML listing for dirs, file download for files."""
    from html import escape as _he
    from urllib.parse import quote as _q
    safe = _files_safe_join(subpath or '')
    if safe is None or not os.path.exists(safe):
        abort(404)
    # Multi-tenancy: scope the browser to the caller's projects (no-op when off).
    # First path segment under FILES_ROOT is the project dir; anything outside
    # the caller's memberships 404s (fail closed). Root listing is filtered below.
    _files_at_root = False
    if auth_enabled():
        _rel = os.path.relpath(safe, FILES_ROOT)
        _first = '.' if _rel == '.' else _rel.split(os.sep)[0]
        if _first == '.':
            _files_at_root = True
        else:
            _owner = db.get_project_by_path(os.path.join(FILES_ROOT, _first))
            _user = g.get('current_user')
            if not _owner or not _user or not has_project_access(_user, _owner.get('id'), 'viewer'):
                abort(404)
    # If it's a file → send it (attachment for download, inline for text/md/pdf would also be fine)
    if os.path.isfile(safe):
        # Double-check forbidden (covers symlink targets too)
        if _is_forbidden_path(os.path.basename(safe)):
            abort(404)
        # Prevent leaking outside via symlink-resolved path
        if not (safe == FILES_ROOT or safe.startswith(FILES_ROOT + os.sep)):
            abort(404)
        return _send_project_file_safely(safe)
    if not os.path.isdir(safe):
        abort(404)

    # Directory listing
    try:
        entries = os.listdir(safe)
    except PermissionError:
        abort(404)
    # Filter + sort: dirs first, then files, case-insensitive
    visible = []
    for name in entries:
        if _is_forbidden_path(name):
            continue
        full = os.path.join(safe, name)
        # Hide broken symlinks or forbidden symlink targets
        try:
            real = os.path.realpath(full)
            if not (real == FILES_ROOT or real.startswith(FILES_ROOT + os.sep)):
                continue
            if os.path.isfile(real) and _is_forbidden_path(os.path.basename(real)):
                continue
        except Exception:
            continue
        visible.append(name)
    # Multi-tenancy: at the vault root, list only the caller's project dirs
    # (admins see all; off mode untouched).
    if _files_at_root and auth_enabled():
        _ids = get_user_project_ids(g.get('current_user'))
        if _ids is not None:
            _allowed = set()
            for _p in db.get_projects():
                _pp = _p.get('path') or ''
                if _p.get('id') in _ids and _pp.startswith(FILES_ROOT + os.sep):
                    _allowed.add(os.path.basename(os.path.realpath(_pp)))
            visible = [n for n in visible if n in _allowed]

    dirs, files = [], []
    for name in visible:
        full = os.path.join(safe, name)
        # lstat without following symlink for type detection; fallback to os.path.isdir
        try:
            if os.path.isdir(full):
                dirs.append(name)
            else:
                files.append(name)
        except Exception:
            files.append(name)
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)

    # Breadcrumbs
    parts = [p for p in (subpath or '').split('/') if p]
    crumbs = ['<a href="/files">vault-projects</a>']
    acc = ''
    for p in parts:
        acc = f"{acc}/{p}" if acc else p
        crumbs.append(f'<a href="/files/{_q(acc)}">{_he(p)}</a>')
    breadcrumb = ' / '.join(crumbs)

    # Human size helper
    def _hr_size(n):
        try:
            for unit in ['B', 'KB', 'MB', 'GB']:
                if abs(n) < 1024:
                    return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
                n /= 1024
            return f"{n:.1f} TB"
        except Exception:
            return ''

    # Build rows
    rows = []
    if subpath:
        parent = '/'.join(parts[:-1])
        parent_href = '/files/' + _q(parent) if parent else '/files'
        rows.append(f'<tr><td>📁</td><td><a href="{parent_href}">..</a></td><td>—</td><td></td></tr>')
    for d in dirs:
        href = '/files/' + _q(f"{subpath}/{d}" if subpath else d)
        try:
            mtime = os.path.getmtime(os.path.join(safe, d))
            mtime_s = datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M')
        except Exception:
            mtime_s = ''
        rows.append(f'<tr><td>📁</td><td><a href="{href}">{_he(d)}/</a></td><td>—</td><td>{mtime_s}</td></tr>')
    for f in files:
        href = '/files/' + _q(f"{subpath}/{f}" if subpath else f)
        try:
            st = os.stat(os.path.join(safe, f))
            size_s = _hr_size(st.st_size)
            mtime_s = datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M')
        except Exception:
            size_s, mtime_s = '', ''
        rows.append(f'<tr><td>📄</td><td><a href="{href}">{_he(f)}</a></td><td>{size_s}</td><td>{mtime_s}</td></tr>')

    count_s = f"{len(dirs)} folders, {len(files)} files"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Files · { _he(subpath or '/') }</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#f4f5f7;color:#1a1a1a}}
 header{{background:#0f172a;color:#fff;padding:14px 20px;display:flex;align-items:center;gap:12px}}
 header a{{color:#93c5fd;text-decoration:none}}
 header a:hover{{text-decoration:underline}}
 .wrap{{max-width:1100px;margin:24px auto;padding:0 16px}}
 .crumb{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:14px;overflow-wrap:anywhere}}
 .meta{{color:#64748b;font-size:13px;margin:8px 2px 12px}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden}}
 th{{text-align:left;background:#f8fafc;color:#475569;font-weight:600;font-size:13px;padding:10px 12px;border-bottom:1px solid #e2e8f0}}
 td{{padding:9px 12px;border-bottom:1px solid #f1f5f9;font-size:14px}}
 tr:last-child td{{border-bottom:none}}
 a{{color:#2563eb;text-decoration:none}} a:hover{{text-decoration:underline}}
 .note{{color:#64748b;font-size:12px;margin-top:10px}}
</style></head><body>
<header><strong>Cordée Vault</strong> <span style="opacity:.6">·</span> <a href="/">← Back to Cordée</a></header>
<div class="wrap">
<div class="crumb">{breadcrumb}</div>
<div class="meta">{count_s} · read-only · hidden: <code>*.db</code> <code>.env</code> <code>.git</code></div>
<table><thead><tr><th style="width:36px"></th><th>Name</th><th style="width:110px">Size</th><th style="width:150px">Modified</th></tr></thead><tbody>
{''.join(rows) if rows else '<tr><td></td><td style="color:#94a3b8">Empty folder</td><td></td><td></td></tr>'}
</tbody></table>
<div class="note">Read-only browser for the projects folder, behind SSO. Downloads are direct file responses; no DB or env files are exposed.</div>
</div></body></html>"""
    resp = Response(html, mimetype='text/html')
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp

# ── Models ────────────────────────────────────────────────────────────────────

@app.route('/api/models')
@require_auth
def get_models():
    # Free-tier model whitelist annotation
    _is_free_model = None
    _user = g.get('current_user') if auth_enabled() else None
    if _user is not None:
        try:
            import agent_quotas
            _is_free_model = agent_quotas._is_free_model
        except Exception:
            pass
    out = []
    models = agent_config.dynamic_models()
    seen = set()
    for model_id, m in models.items():
        seen.add(model_id)
        entry = {
            'id': model_id,
            'label': m['label'],
            'provider': m['provider'],
            'color': PROVIDER_COLORS.get(m['provider'], '#888'),
            'cost_input': m['cost_input'],
            'cost_output': m['cost_output'],
            'default': m.get('default', False),
        }
        if _is_free_model is not None:
            entry['free_allowed'] = _is_free_model(model_id)
        out.append(entry)
    return jsonify(out)


@app.route('/api/config')
@require_auth
def get_config():
    # Get readiness status
    from flask import current_app
    import agent_rag
    # Safe subset: everything the frontend needs, no provider-key presence,
    # no instance identity. Served to any authenticated user.
    safe = {
        'anthropic_mode': agent_config.ANTHROPIC_MODE,
        'mistral_mode': agent_config.MISTRAL_MODE,
        'claude_code_skip_permissions': CLAUDE_CODE_SKIP_PERMISSIONS,
        'vibe_skip_permissions': agent_config.VIBE_SKIP_PERMISSIONS,
        'claude_pro_token_budget': agent_config.SESSION_TOKEN_BUDGET,
        # /opt/RAG legal corpora live on the vault host only — non-vault
        # installs (e.g. a laptop) have no /opt/RAG and would 404 on search.
        'rag_corpora': agent_rag.list_corpora() if agent_config.IS_VAULT else [],
    }
    _u = g.get('current_user')
    if auth_enabled() and _u is not None and _u.get('role') != 'admin':
        return jsonify(safe)
    # Off mode (anonymous, unchanged legacy behavior) and global admins:
    # full payload including readiness + instance identity.
    readiness = get_config_readiness().get_json()

    return jsonify({
        **safe,
        'instance_name': INSTANCE_NAME,
        'is_vault': agent_config.IS_VAULT,
        'ready': readiness,
    })


@app.route('/api/config', methods=['POST'])
@require_admin
def set_config():
    data = request.get_json(silent=True) or {}
    anthropic_mode = data.get('anthropic_mode')
    mistral_mode = data.get('mistral_mode')
    token_budget = data.get('claude_pro_token_budget')

    if anthropic_mode and anthropic_mode not in ('api', 'claude-code'):
        return jsonify({'error': 'anthropic_mode must be "api" or "claude-code"'}), 400
    if mistral_mode and mistral_mode not in ('api', 'vibe'):
        return jsonify({'error': 'mistral_mode must be "api" or "vibe"'}), 400
    if token_budget is not None:
        try:
            token_budget = int(token_budget)
        except (TypeError, ValueError):
            return jsonify({'error': 'claude_pro_token_budget must be an integer'}), 400
        if token_budget < 1000 or token_budget > 10_000_000:
            return jsonify({'error': 'claude_pro_token_budget must be between 1,000 and 10,000,000'}), 400

    if anthropic_mode:
        agent_config.ANTHROPIC_MODE = anthropic_mode
        db.set_app_setting('anthropic_mode', anthropic_mode)
    if mistral_mode:
        agent_config.MISTRAL_MODE = mistral_mode
        db.set_app_setting('mistral_mode', mistral_mode)
    if token_budget is not None:
        agent_config.SESSION_TOKEN_BUDGET = token_budget
        db.set_app_setting('claude_pro_token_budget', token_budget)

    return jsonify({
        'anthropic_mode': agent_config.ANTHROPIC_MODE,
        'mistral_mode': agent_config.MISTRAL_MODE,
        'claude_code_skip_permissions': CLAUDE_CODE_SKIP_PERMISSIONS,
        'vibe_skip_permissions': agent_config.VIBE_SKIP_PERMISSIONS,
        'claude_pro_token_budget': agent_config.SESSION_TOKEN_BUDGET,
        'instance_name': INSTANCE_NAME,
        'is_vault': agent_config.IS_VAULT,
    })


_READINESS_CACHE = {'ts': 0.0, 'value': None}
_READINESS_TTL = 30  # seconds

@app.route('/api/config/readiness')
@require_admin
def get_config_readiness():
    import shutil, os, subprocess, json as _json, time
    now = time.time()
    if _READINESS_CACHE['value'] is not None and now - _READINESS_CACHE['ts'] < _READINESS_TTL:
        return jsonify(_READINESS_CACHE['value'])
    claude_ok = False
    if shutil.which('claude'):
        try:
            res = subprocess.run(
                ['claude', 'auth', 'status', '--json'],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0:
                claude_ok = bool(_json.loads(res.stdout).get('loggedIn'))
        except (subprocess.TimeoutExpired, ValueError, OSError):
            claude_ok = False
    
    # Vibe has no `auth` subcommand. Authentication is via MISTRAL_VIBE_KEY
    # (Pro/Vibe scope) and MISTRAL_ORG_KEY (PAYG/Org scope) in ~/.vibe/.env.
    # Readiness requires the binary on PATH AND the Pro key — the Org key is
    # optional (only needed if you route Mistral Large through Vibe).
    vibe_ok = bool(shutil.which('vibe')) and bool(os.getenv('MISTRAL_VIBE_KEY'))

    codex_ok = False
    if shutil.which('codex'):
        try:
            res = subprocess.run(
                ['codex', 'login', 'status'],
                capture_output=True, text=True, timeout=5,
            )
            codex_ok = res.returncode == 0 and 'Logged in' in (res.stdout + res.stderr)
        except (subprocess.TimeoutExpired, OSError):
            codex_ok = False

    # mistral_api: direct-API mode uses the Pro/Vibe key (Improve-with-AI, etc.)
    value = {
        'api': bool(os.getenv('ANTHROPIC_API_KEY')),
        'claude_code': claude_ok,
        'vibe': vibe_ok,
        'mistral_api': bool(os.getenv('MISTRAL_VIBE_KEY')),
        'codex': codex_ok,
        'openai_api': bool(os.getenv('OPENAI_API_KEY')),
        'scaleway': agent_config.has_real_secret(agent_config.SCALEWAY_API_KEY),
        'ollama': agent_config.has_real_secret(agent_config.OLLAMA_API_KEY),
    }
    _READINESS_CACHE['ts'] = now
    _READINESS_CACHE['value'] = value
    return jsonify(value)


# ── Projects ──────────────────────────────────────────────────────────────────

def _write_aingel_json(project_path, proj):
    """Write aingel.json to the project folder from DB row. Atomic write."""
    if not project_path or not os.path.isdir(project_path):
        return
    data = {
        'name': proj.get('name', ''),
        'type': proj.get('project_type', ''),
        'execution_type': proj.get('execution_type') or 'standard',
        'eu_only': bool(proj.get('eu_only', False)),
        'llm_mode': proj.get('llm_mode') or 'standard',
        'budget_monthly': proj.get('budget_monthly'),
        'git_enabled': proj.get('git_enabled'),
        'aingel_name': proj.get('aingel_name'),
        'deployment_config': None,
        'aingel_version': '1.0',
        # Phase 7: AIngel Autopilot
        'aingel_autopilot': bool(proj.get('aingel_autopilot', False)),
        'aingel_mode': proj.get('aingel_mode') or 'advisory',
        'scw_session': {
            'enabled': bool(proj.get('scw_session_enabled')),
            'project_id': proj.get('scw_project_id'),
            'bucket': proj.get('scw_session_bucket'),
            'kms_key_id': proj.get('scw_kms_key_id'),
            'region': proj.get('scw_session_region'),
        } if proj.get('scw_session_enabled') else None,
    }
    path = os.path.join(project_path, 'aingel.json')
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as _e:
        print(f'[aingel.json] WARNING: could not write {path}: {_e}')


@app.route('/api/projects')
@require_auth
def list_projects():
    projects = db.get_projects()
    if auth_enabled():
        allowed = get_user_project_ids(g.current_user)
        if allowed is not None:
            projects = [p for p in projects if p.get('id') in allowed]
    for p in projects:
        p['def_filename'] = resolve_def_filename(p.get('path', ''))
        p['current_user_role'] = _project_role_for_user(p)
    return jsonify(projects)


def _project_role_for_user(project):
    """The caller's effective role on `project` ('owner' for global admins).

    Lets the UI decide whether to offer member management on a project the
    caller owns or admins without holding the global admin role. None when the
    caller has no access (or auth is off and no user is resolvable).
    """
    if not auth_enabled():
        return 'owner'
    try:
        import agent_auth
        return agent_auth._effective_role(project, g.current_user)
    except Exception:
        return None


_WEEK_SECS = 7 * 24 * 3600


def _count_recent(tasks, status=None):
    """Count tasks whose updated_at falls within the last 7 days.

    If `status` is given (string or tuple), only count tasks currently in that
    status. Otherwise count any task touched in the window (i.e. new + changed).
    Task timestamps are stored as UTC ISO strings ('...T...', optionally with an
    offset) or SQLite CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS' in UTC). Both are
    normalised to 'YYYY-MM-DD HH:MM:SS' so a single lexical compare is correct —
    comparing the raw ISO form against both cutoffs let 'T' > ' ' pass rows that
    fall on the cutoff's date but before its time.
    """
    cutoff = (datetime.now(timezone.utc) - _timedelta(seconds=_WEEK_SECS)).isoformat()
    cutoff_cmp = cutoff.replace('T', ' ')[:19]
    n = 0
    for t in tasks:
        ts = t.get('updated_at') or t.get('created_at') or ''
        if not ts:
            continue
        if ts.replace('T', ' ')[:19] < cutoff_cmp:
            continue
        if status is None or (isinstance(status, tuple) and t.get('status') in status) \
                or (not isinstance(status, tuple) and t.get('status') == status):
            n += 1
    return n


def _scoped_cost_summary(projects):
    """Phase 3C: cost aggregates scoped to `projects` for non-admin callers.

    Mirrors db.get_cost_summary()/db.get_cost_by_model() but sums only the
    given projects' execution tables, so members never see other users'
    spend. Shape matches the global version (zeros, not missing keys, when
    the project list is empty). Pass-through is handled by callers: global
    admins (allowed is None) keep using the unfiltered db functions.
    """
    total = today = week = 0.0
    done_count = 0
    project_week = {}
    today_map = {}
    week_map = {}
    for p in projects:
        pid = p['id']
        try:
            pconn = db.get_project_db(p.get('path') or '')
        except Exception:
            continue
        try:
            row = pconn.execute('''
                SELECT
                    COALESCE(SUM(cost_usd),0) total_spent,
                    COALESCE(SUM(CASE WHEN date(started_at)=date('now') THEN cost_usd END),0) today,
                    COALESCE(SUM(CASE WHEN started_at>=date('now','-7 days') THEN cost_usd END),0) week,
                    COUNT(CASE WHEN status='done' THEN 1 END) tasks_done
                FROM executions
            ''').fetchone()
            if row:
                total += row['total_spent'] or 0
                today += row['today'] or 0
                w = row['week'] or 0.0
                week += w
                done_count += row['tasks_done'] or 0
                project_week[pid] = w
            for r in pconn.execute(
                "SELECT model, COALESCE(SUM(cost_usd),0) AS spent FROM executions "
                "WHERE date(started_at)=date('now') GROUP BY model"
            ).fetchall():
                today_map[r['model']] = today_map.get(r['model'], 0) + (r['spent'] or 0)
            for r in pconn.execute(
                "SELECT model, COALESCE(SUM(cost_usd),0) AS spent FROM executions "
                "WHERE started_at>=date('now','-7 days') GROUP BY model"
            ).fetchall():
                week_map[r['model']] = week_map.get(r['model'], 0) + (r['spent'] or 0)
        except Exception:
            pass
        finally:
            try:
                pconn.close()
            except Exception:
                pass
    return {
        'total_spent': total, 'today': today, 'week': week,
        'tasks_done': int(done_count), 'project_week': project_week,
        'by_model': {
            'today': sorted([{'model': m, 'spent': s} for m, s in today_map.items()],
                            key=lambda x: -x['spent']),
            'week': sorted([{'model': m, 'spent': s} for m, s in week_map.items()],
                           key=lambda x: -x['spent']),
        },
    }


@app.route('/api/dashboard')
@require_auth
def get_dashboard():
    """Return aggregated dashboard data across all projects."""
    projects = db.get_projects()
    # Phase 3C: in auth mode, non-admins see only their member projects.
    # Filter the project set FIRST so every count/cost below reflects only
    # accessible projects. Global admins (allowed is None) see all.
    allowed_ids = None
    if auth_enabled():
        allowed = get_user_project_ids(g.current_user)
        if allowed is not None:
            projects = [p for p in projects if p.get('id') in allowed]
            allowed_ids = {p['id'] for p in projects}
    all_tasks = db.get_tasks(include_archived=False)
    if allowed_ids is not None:
        all_tasks = [t for t in all_tasks if t.get('project_id') in allowed_ids]
        cost_summary = _scoped_cost_summary(projects)
    else:
        cost_summary = db.get_cost_summary()
    project_week = cost_summary.get('project_week', {})
    
    # Per-project stats
    project_stats = []
    for p in projects:
        proj_tasks = [t for t in all_tasks if t['project_id'] == p['id']]
        total = len(proj_tasks)
        done = sum(1 for t in proj_tasks if t['status'] == 'done')
        running = sum(1 for t in proj_tasks if t['status'] == 'running')
        pending = sum(1 for t in proj_tasks if t['status'] in ('pending', 'confirmed'))
        failed = sum(1 for t in proj_tasks if t['status'] == 'failed')
        hf_models_count = len(db.get_project_hf_models(p['id']))
        awaiting_model_tasks = sum(1 for t in proj_tasks if t.get('awaiting_model'))
        week_done = _count_recent(proj_tasks, 'done')
        week_running = _count_recent(proj_tasks, 'running')
        week_pending = _count_recent(proj_tasks, ('pending', 'confirmed'))
        week_failed = _count_recent(proj_tasks, 'failed')
        
        # Budget info
        budget = p.get('budget_monthly') or 0.0
        spend = p.get('current_month_spend') or 0.0
        budget_pct = round((spend / budget * 100), 1) if budget > 0 else None
        
        # Recent executions for this project
        recent_execs = [e for e in db.get_executions(limit=5) if e.get('project_id') == p['id']]
        
        project_stats.append({
            'id': p['id'],
            'name': p['name'],
            'slug': p['slug'],
            'eu_only': bool(p.get('eu_only', False)),
            'scw_session_enabled': bool(p.get('scw_session_enabled', False)),
            'scw_session_bucket': p.get('scw_session_bucket') or '',
            'scw_kms_key_id': p.get('scw_kms_key_id') or '',
            'total_tasks': total,
            'done_tasks': done,
            'running_tasks': running,
            'pending_tasks': pending,
            'failed_tasks': failed,
            'hf_models_count': hf_models_count,
            'awaiting_model_tasks': awaiting_model_tasks,
            'week_done': week_done,
            'week_running': week_running,
            'week_pending': week_pending,
            'week_failed': week_failed,
            'progress_pct': round((done / total * 100), 1) if total > 0 else 0,
            'current_user_role': _project_role_for_user(p),
            'budget_monthly': budget or None,
            'current_month_spend': round(spend, 4),
            'week_spent': round(project_week.get(p['id'], 0.0), 4),
            'budget_pct': budget_pct,
            'budget_exceeded': bool(budget and spend >= budget),
            'recent_executions': [{
                'id': e['id'],
                'task_title': e.get('task_title', ''),
                'status': e['status'],
                'cost_usd': e.get('cost_usd', 0),
                'started_at': e.get('started_at', ''),
                'model': e.get('model', ''),
            } for e in recent_execs],
        })
    
    # Global stats
    total_tasks = len(all_tasks)
    total_done = sum(1 for t in all_tasks if t['status'] == 'done')
    total_running = sum(1 for t in all_tasks if t['status'] == 'running')
    total_pending = sum(1 for t in all_tasks if t['status'] in ('pending', 'confirmed'))
    total_failed = sum(1 for t in all_tasks if t['status'] == 'failed')
    week_total = _count_recent(all_tasks)
    week_done = _count_recent(all_tasks, 'done')
    week_running = _count_recent(all_tasks, 'running')
    week_pending = _count_recent(all_tasks, ('pending', 'confirmed'))
    week_failed = _count_recent(all_tasks, 'failed')
    total_hf_models = sum(s['hf_models_count'] for s in project_stats)
    total_awaiting = sum(s['awaiting_model_tasks'] for s in project_stats)
    
    return jsonify({
        'projects': project_stats,
        'global': {
            'total_projects': len(projects),
            'total_tasks': total_tasks,
            'total_done': total_done,
            'total_running': total_running,
            'total_pending': total_pending,
            'total_failed': total_failed,
            'hf_models_count': total_hf_models,
            'awaiting_model_tasks': total_awaiting,
            'week_total': week_total,
            'week_done': week_done,
            'week_running': week_running,
            'week_pending': week_pending,
            'week_failed': week_failed,
            'overall_progress_pct': round((total_done / total_tasks * 100), 1) if total_tasks > 0 else 0,
            'total_spent': round(cost_summary.get('total_spent', 0), 4),
            'today_spent': round(cost_summary.get('today', 0), 4),
            'week_spent': round(cost_summary.get('week', 0), 4),
        },
    })


_SCAFFOLD_PROMPT_TEMPLATE = """[SCAFFOLDING MODE — strict output format]

You are helping draft the phase roadmap for a new SuperAgent project.

CRITICAL OUTPUT CONTRACT — every reply, including this one and every follow-up
in this conversation, MUST:
1. Contain the COMPLETE updated GUIDE.md — never partial updates, never deltas,
   never "here's just Phase 3 with the change". Always re-emit the entire
   document with the requested change applied.
2. Start the phases at `## Phase 1 — Title` and number them contiguously
   (`## Phase 2 — Title`, `## Phase 3 — Title`, ...).
3. Under each phase header, list tasks as `- [ ] task title` lines
   (3-8 tasks per phase, 4-7 phases total).
4. Wrap the whole roadmap in a fenced markdown block:

```markdown
## Phase 1 — ...
- [ ] ...
...
```

5. Outside the fence, you may write a one-line note ("Added a setup phase as
   requested.") but never put any phase content outside the fence.

Brief conversational explanations are fine OUTSIDE the fence. Phase content
NEVER appears outside the fence. If you forget a phase, the whole reply is
ignored — so always re-emit them all.

Project: {name}
Stack hints: {stack}

Pitch:
{pitch}
"""

# How many distinct `## Phase N` headers a draft must have to be considered
# a "full" roadmap. Below this threshold we keep the previous stored draft.
_MIN_PHASES_FOR_VALID_DRAFT = 2


def _extract_full_draft(reply_text):
    """Try to extract a full-roadmap draft from an assistant reply.
    Returns the draft string if it passes validation (contains a fenced
    markdown block with `## Phase 1` and at least _MIN_PHASES_FOR_VALID_DRAFT
    phase headers). Otherwise returns ''."""
    if not reply_text:
        return ''
    # Models sometimes nest fences (```markdown … ```markdown … ``` ```).
    # A non-greedy match stops at the first closing fence, capturing only the
    # outer wrapper. Try non-greedy first; if the candidate fails validation,
    # retry with a greedy match to grab everything up to the LAST closing fence.
    m = re.search(r'```(?:markdown|md)?\s*\n(.*?)\n```', reply_text, re.DOTALL)
    if m:
        candidate = m.group(1).strip()
        headers = _PHASE_HEADER_RE.findall(candidate)
        if len(headers) >= _MIN_PHASES_FOR_VALID_DRAFT and \
                re.search(r'^#{1,3}\s+Phase\s+1\b', candidate, re.MULTILINE | re.IGNORECASE):
            return candidate
    # Greedy fallback: capture up to the last ``` in the reply.
    m = re.search(r'```(?:markdown|md)?\s*\n(.*)\n```', reply_text, re.DOTALL)
    if not m:
        return ''
    candidate = m.group(1).strip()
    # If there are nested fences, strip the inner opening fence(s).
    candidate = re.sub(r'^```(?:markdown|md)?\s*\n', '', candidate)
    headers = _PHASE_HEADER_RE.findall(candidate)
    if len(headers) < _MIN_PHASES_FOR_VALID_DRAFT:
        return ''
    if not re.search(r'^#{1,3}\s+Phase\s+1\b', candidate, re.MULTILINE | re.IGNORECASE):
        return ''
    return candidate


def _maybe_update_scaffold_draft(chat_id, reply_text):
    """If this chat is a scaffolding chat and the reply contains a full draft,
    persist it. Otherwise leave the existing draft alone (fallback)."""
    if not reply_text:
        return False
    chat = db.get_chat(chat_id)
    if not chat or chat.get('name') != 'Project scaffolding':
        return False
    draft = _extract_full_draft(reply_text)
    if not draft:
        return False
    db.update_chat(chat_id, scaffold_draft=draft)
    return True


@app.route('/api/projects', methods=['POST'])
@require_auth
def create_project():
    """Create a new project: scaffold folder, insert DB row, spawn a
    'Project scaffolding' chat seeded with the user's pitch."""
    data = request.json or {}
    name = (data.get('name') or '').strip()
    pitch = (data.get('pitch') or '').strip()
    stack_hints = (data.get('stack_hints') or '').strip()
    eu_only = bool(data.get('eu_only', False))
    # Scaffolding runs before the project row exists, so resolve the default
    # against the eu_only flag being submitted rather than a stored project.
    model = (data.get('scaffolding_model') or '').strip() \
        or agent_config.default_model_for(eu_only)
    project_type = (data.get('project_type') or '').strip()
    llm_mode = (data.get('llm_mode') or 'standard').strip()
    execution_type = (data.get('execution_type') or 'standard').strip()
    aingel_name = (data.get('aingel_name') or '').strip() or None

    if not name:
        return jsonify({'error': 'name required'}), 400
    if not pitch:
        return jsonify({'error': 'pitch required'}), 400
    if not re.match(r'^[A-Za-z0-9 _\-]+$', name):
        return jsonify({'error': 'name may only contain letters, digits, space, underscore, hyphen'}), 400
    # The scaffolding chat is seeded with the pitch — an EU-only project must not
    # send it to a US provider, and on the Vault no project may.
    ok, err = _eu_guard(eu_only, model)
    if not ok:
        return _eu_reject(None, model, 'create_project', err)

    slug = slugify(name)
    # Refuse duplicate slug
    if any(p['slug'] == slug for p in db.get_projects()):
        return jsonify({'error': f'A project named "{name}" already exists'}), 400

    project_path = os.path.join(PROJECTS_ROOT, name)
    if os.path.exists(project_path):
        return jsonify({'error': f'Folder already exists at {project_path}'}), 400

    # Free-tier project-count quota
    _creator = g.get('current_user') if auth_enabled() else None
    if _creator is not None:
        try:
            import agent_quotas
            agent_quotas.check_project_create(_creator['id'])
        except agent_quotas.QuotaError as e:
            return jsonify({'error': str(e), 'quota': e.field, 'limit': e.limit}), 429

    try:
        scaffold_project_folder(project_path, name, pitch)
    except Exception as e:
        return jsonify({'error': f'scaffold failed: {e}'}), 500

    project = db.upsert_project(name, slug, project_path, project_type, eu_only, llm_mode,
                                execution_type=execution_type, aingel_name=aingel_name)
    # New projects enforce the Guide gate: a hold stops the run. The column
    # default stays 'advisory' so existing rows keep whatever they were set to.
    db.set_project_field(project['id'], 'aingel_mode', 'strict')
    project['aingel_mode'] = 'strict'
    _write_aingel_json(project_path, project)

    # Multi-tenancy: in oidc mode the creator becomes owner (owner_id +
    # project_members row) so the project is instantly accessible to them.
    # Off mode keeps ownerless projects — zero behavior change.
    # (_creator was resolved above for the quota check.)
    if _creator is not None:
        try:
            db.set_project_owner(project['id'], _creator['id'])
            db.add_project_member(project['id'], _creator['id'], 'owner')
        except Exception as _e:
            app.logger.warning('owner bootstrap failed: %s', _e)

    # Create the scaffolding chat
    chat = db.create_chat(
        project_id=project['id'],
        name='Project scaffolding',
        model=model,
        attachments=[],
        created_by=_creator['id'] if _creator is not None else None,
    )
    full = db.get_chat(chat['id'])
    if full and full.get('project_path'):
        path = chats_mod.create_chat_file(full['project_path'], full)
        db.update_chat(chat['id'], file_path=path)

    # Prepare progress queue for this project
    with _scaffold_progress_lock:
        q = queue.Queue()
        _scaffold_progress_queues[project['id']] = q

    # Seed the first user message and get the AI draft in a background thread
    seeded = _SCAFFOLD_PROMPT_TEMPLATE.format(
        name=name,
        stack=stack_hints or 'none',
        pitch=pitch,
    )

    def _scaffold_hb(exec_id, proj_path):
        _stop = threading.Event()
        def _loop():
            while not _stop.wait(30):
                try:
                    db.touch_execution_heartbeat(exec_id, project_path=proj_path or None)
                except Exception:
                    pass
        threading.Thread(target=_loop, daemon=True, name=f'hb-{exec_id}').start()
        return _stop.set

    def _do_scaffold(chat_id, project_id, seeded_text, model_id):
        _hb_stop = None
        exec_id = None
        try:
            q.put({'type': 'log', 'message': 'Project created. Starting AI draft…'})
            q.put({'type': 'log', 'message': 'Reading project pitch and stack hints…'})
            q.put({'type': 'log', 'message': 'Generating phase roadmap…'})

            # Call the model directly with just the scaffolding prompt — do NOT
            # go through chat_reply(), which injects project memory, file
            # listings, auto-catch, tool instructions, etc.  Those 50k+ tokens
            # of context distract the model and cause it to emit a one-liner
            # summary instead of the full roadmap.  The scaffolding prompt is
            # self-contained and needs zero project context.
            proj_path = project.get('path') or ''
            exec_id = db.create_execution(
                task_id=None, model=model_id, chat_id=chat_id,
                project_path=proj_path or None,
            )
            _hb_stop = _scaffold_hb(exec_id, proj_path)
            text, tok_in, tok_out, cost = route(
                model_id, seeded_text, 4096,
                project_path=proj_path or None,
                caller='scaffold',
            )
            summary = text[:2000] + ('…' if len(text) > 2000 else '')
            db.finish_execution(
                exec_id, 'done',
                tokens_input=tok_in, tokens_output=tok_out,
                cost_usd=cost, output_summary=summary,
                project_path=proj_path or None,
            )
            # Persist to the chat file so the user can see the exchange later.
            chat_path = (db.get_chat(chat_id) or {}).get('file_path') or ''
            if chat_path:
                chats_mod.append_user_message(chat_path, seeded_text)
                chats_mod.append_assistant_message(
                    chat_path, text, model_id, tok_in, tok_out, cost, exec_id)
            db.touch_chat(chat_id, project_path=proj_path or None)

            # Spend is recorded centrally in route(); no per-call-site roll here.
            _maybe_update_scaffold_draft(chat_id, text)
            q.put({'type': 'log', 'message': 'Draft complete.'})
            q.put({'type': 'done'})
        except Exception as e:
            q.put({'type': 'log', 'message': f'Error during AI draft: {e}'})
            q.put({'type': 'done'})
            # The scaffold execution row must not stay 'running': delete-project
            # refuses while any running row exists, and nothing else will ever
            # finish this one (this thread is dying). Mark it failed instead.
            if exec_id is not None:
                try:
                    db.finish_execution(
                        exec_id, 'failed',
                        error_message=f'Scaffolding failed: {e}',
                        project_path=project.get('path') or None,
                    )
                except Exception as _fe:
                    app.logger.warning('scaffold finish-failed failed: %s', _fe)
        finally:
            try:
                if _hb_stop:
                    _hb_stop()
            except Exception:
                pass
            with _scaffold_progress_lock:
                _scaffold_progress_queues.pop(project_id, None)

    threading.Thread(
        target=_do_scaffold,
        args=(chat['id'], project['id'], seeded, model),
        daemon=True,
    ).start()

    # Auto-init AIngel if model specified at creation
    aingel_model = (data.get('aingel_model') or '').strip()
    if aingel_model:
        try:
            _init_aingel(project['id'], aingel_model)
        except Exception as e:
            app.logger.warning('aingel auto-init failed: %s', e)

    return jsonify({
        'project': project,
        'chat': db.get_chat(chat['id']),
        'reply': None,  # reply will come via SSE
    }), 201


def _extract_guide_from_message(text):
    """Pull GUIDE.md content out of an assistant reply. Prefers a ```markdown fence;
    falls back to the whole text if no fence found."""
    if not text:
        return ''
    m = re.search(r'```(?:markdown|md)?\s*\n(.*?)\n```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


_PHASE_HEADER_RE = re.compile(r'^#{1,3}\s+Phase\s+\d+', re.MULTILINE | re.IGNORECASE)


def _assistant_messages(transcript):
    """Yield assistant message bodies from a chat transcript, newest first."""
    if not transcript:
        return []
    parts = re.split(r'\n###\s*\[[^\]]+\]\s*🤖\s*Assistant[^\n]*\n', transcript)
    bodies = []
    for body in parts[1:]:
        body = re.sub(r'^\s*\*\*Model:\*\*[^\n]*\n', '', body)
        body = re.split(r'\n---\s*\n', body, maxsplit=1)[0].strip()
        if body:
            bodies.append(body)
    bodies.reverse()
    return bodies


def _pick_guide_draft(transcript):
    """Walk assistant messages newest→oldest. Return the first one that
    looks like a GUIDE.md (has `## Phase N` headers) after fence-extraction.
    Returns '' if no draft has phase headers."""
    for body in _assistant_messages(transcript):
        candidate = _extract_guide_from_message(body)
        if _PHASE_HEADER_RE.search(candidate):
            return candidate
    return ''


@app.route('/api/projects/<int:pid>/apply-guide', methods=['POST'])
@require_project_access('member')
def apply_scaffold_guide(pid):
    """Take the latest assistant message from the scaffolding chat, write it
    to <project>/GUIDE.md, and import phase tasks."""
    data = request.json or {}
    chat_id = data.get('chat_id')
    if not chat_id:
        return jsonify({'error': 'chat_id required'}), 400

    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    chat = db.get_chat(chat_id)
    if not chat or chat.get('project_id') != pid:
        return jsonify({'error': 'Chat not found in this project'}), 404

    # Prefer the server-validated stored draft. Fall back to scanning the
    # transcript only for chats created before the scaffold_draft mechanism.
    guide_text = (chat.get('scaffold_draft') or '').strip()
    if not guide_text:
        transcript = chats_mod.parse_messages(chat.get('file_path') or '')
        guide_text = _pick_guide_draft(transcript)
    if not guide_text:
        return jsonify({
            'error': 'No full-roadmap draft on file yet. Ask the AI to re-emit the COMPLETE '
                     'GUIDE.md (all phases, starting at Phase 1, in a ```markdown fence).',
        }), 422

    guide_path = os.path.join(project['path'], 'GUIDE.md')
    with open(guide_path, 'w', encoding='utf-8') as f:
        f.write(guide_text + '\n')

    imported = import_phase_tasks_for_project(pid)
    if imported == 0:
        return jsonify({
            'error': 'GUIDE.md written, but the parser found no phases. Header must look like '
                     '`## Phase 1 — Title` and tasks like `- [ ] description`.',
            'tasks_imported': 0,
        }), 422

    # Auto-chain: task N → task N-1 for all non-archived tasks (unless
    # the user already set a dependency manually).
    deps_added = db.auto_chain_dependencies(pid)

    return jsonify({'tasks_imported': imported, 'deps_added': deps_added})


@app.route('/api/projects/<int:pid>/auto-deps', methods=['POST'])
@require_project_access('member')
def auto_chain_project_deps(pid):
    """Add sequential dependencies (N → N-1) to all tasks in a project.

    Skips tasks that already have at least one dependency (preserves
    manual work). Returns the number of new dependency edges added.
    """
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    added = db.auto_chain_dependencies(pid)
    return jsonify({'added': added})


_TRASH_DIR = os.path.join(PROJECTS_ROOT, '.trash')
_TRASH_RETENTION_DAYS = 7
# Built project archives live outside every project tree (so quota storage
# walks do not count them), one subdir per project: `.archives/<pid>/<file>`.
# The per-pid nesting is what scopes the download route (IDOR fix). Kept beside
# .trash for operator symmetry; reaped by _reap_archives() after
# _ARCHIVE_RETENTION_DAYS (the reaper also cleans any legacy flat zip).
_ARCHIVES_DIR = os.path.join(PROJECTS_ROOT, '.archives')
_ARCHIVE_RETENTION_DAYS = 7
# Cap on an uploaded archive body. Flask's MAX_CONTENT_LENGTH already rejects
# oversized multipart bodies with 413; this is the explicit import-level guard
# (and the value chunked import will eventually replace). Chunked import is
# future work — a single 5 GiB upload is impractical over HTTP, so importing a
# larger project is a known limitation rather than a silent failure.
_MAX_IMPORT_BYTES = int(getattr(agent_config, 'MAX_CONTENT_LENGTH', 128 * 1024 * 1024)) \
    or 128 * 1024 * 1024


def _trash_path(slug, ts):
    """Return the trash destination for a project folder. The caller is
    responsible for the PROJECTS_ROOT containment check on the source path."""
    return os.path.join(_TRASH_DIR, f'{slug}-{ts}')


def _project_running_executions(project):
    """Return a list of running execution dicts for a project, or [] if the
    project has no project.db yet. Used to refuse deletion while work is live.

    Returns None if the project.db exists but could not be read (locked,
    corrupt) — the caller must treat that as "indeterminate" and refuse to
    delete, rather than assume there is nothing running."""
    path = project.get('path') or ''
    if not path or not os.path.exists(os.path.join(path, 'project.db')):
        return []
    try:
        pconn = db.get_project_db(path)
        rows = pconn.execute(
            "SELECT id, task_id, status, started_at FROM executions "
            "WHERE status='running' ORDER BY id"
        ).fetchall()
        pconn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[delete-project] running-exec check failed for {path}: {e}')
        return None


def _project_confirmed_tasks(project):
    """Return a list of confirmed (queued, not-yet-running) task dicts for a
    project, or [] if the project has no project.db yet. Used to refuse
    deletion while the user has queued work that hasn't run.

    Returns None if the project.db exists but could not be read (locked,
    corrupt) — the caller must treat that as "indeterminate" and refuse to
    delete, rather than assume there is nothing queued."""
    path = project.get('path') or ''
    if not path or not os.path.exists(os.path.join(path, 'project.db')):
        return []
    try:
        pconn = db.get_project_db(path)
        rows = pconn.execute(
            "SELECT id, title, status FROM tasks "
            "WHERE status='confirmed' ORDER BY id"
        ).fetchall()
        pconn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[delete-project] confirmed-task check failed for {path}: {e}')
        return None


def _reap_trash():
    """Hard-delete trash entries older than _TRASH_RETENTION_DAYS. Runs at
    startup. Never raises — a failure must not block boot."""
    try:
        if not os.path.isdir(_TRASH_DIR):
            return 0
        cutoff = datetime.now(timezone.utc) - _timedelta(days=_TRASH_RETENTION_DAYS)
        reaped = 0
        for entry in os.listdir(_TRASH_DIR):
            if entry == '_audit.log':
                continue
            full = os.path.join(_TRASH_DIR, entry)
            if not os.path.isdir(full):
                continue
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(full), timezone.utc)
            except Exception:
                continue
            if mtime < cutoff:
                shutil.rmtree(full, ignore_errors=True)
                reaped += 1
        return reaped
    except Exception as e:
        print(f'[startup] WARNING: trash reaper failed, continuing anyway: {e}')
        return 0


def _reap_archives():
    """Hard-delete built archives older than _ARCHIVE_RETENTION_DAYS. Runs at
    startup and daily (next to _reap_trash). Delegates to agent_archive so the
    retention constant lives with the code that writes the zips. Never raises."""
    try:
        return agent_archive.reap_archives(_ARCHIVES_DIR, _ARCHIVE_RETENTION_DAYS)
    except Exception as e:
        print(f'[startup] WARNING: archives reaper failed, continuing anyway: {e}')
        return 0


def _reap_uploads():
    """Remove stale per-project .uploads/<upload_id> dirs older than 24h.
    Scans every project_path/.uploads and deletes subdirs whose mtime > 24h old.
    Best-effort, never raises."""
    try:
        projects = db.get_projects()
        cutoff = time.time() - 86400  # 24h
        reaped = 0
        for proj in projects:
            ppath = proj.get('path') or ''
            if not ppath or not os.path.isdir(ppath):
                continue
            uploads_root = os.path.join(ppath, '.uploads')
            if not os.path.isdir(uploads_root):
                continue
            try:
                entries = os.listdir(uploads_root)
            except Exception:
                continue
            for entry in entries:
                full = os.path.join(uploads_root, entry)
                # Only consider directories (each upload_id subdir)
                if not os.path.isdir(full):
                    continue
                try:
                    mtime = os.path.getmtime(full)
                except Exception:
                    continue
                if mtime < cutoff:
                    try:
                        shutil.rmtree(full, ignore_errors=True)
                        reaped += 1
                    except Exception:
                        pass
        return reaped
    except Exception as e:
        print(f'[startup] WARNING: uploads reaper failed, continuing anyway: {e}')
        return 0


def _reap_project_trash():
    """Hard-delete per-project soft-delete trash entries older than
    _TRASH_RETENTION_DAYS.

    agent_files.delete soft-deletes files into the OUT-OF-TREE trash
    (<AINGEL_TRASH_ROOT>/<project_id>/files/<timestamp>, default
    <TRASH_ROOT>/<id>/files/<timestamp>) so deleted files cannot be
    resurrected from inside the project tree. This reaper makes them truly
    disappear after the retention window and reaps expired tombstones. It also
    reaps any legacy in-tree <project>/.trash/files/<timestamp> entries (from
    before relocation). Best-effort, never raises.

    Failures are counted and logged, not swallowed: a silent no-op here (e.g. a
    permission mismatch between the vault service user and the trash owner) would
    otherwise let expired trash accumulate forever with no signal. rmtree is run
    WITHOUT ignore_errors so a partial failure raises and is attributed to the
    entry."""
    _reaped = 0
    try:
        import agent_config
        TRASH_ROOT = getattr(agent_config, 'TRASH_ROOT', None)
    except Exception:
        TRASH_ROOT = None
    cutoff = time.time() - (_TRASH_RETENTION_DAYS * 86400)
    cutoff_ts = datetime.now(timezone.utc) - _timedelta(days=_TRASH_RETENTION_DAYS)
    cutoff_iso = cutoff_ts.strftime('%Y-%m-%dT%H:%M:%SZ')
    failed = 0
    projects = db.get_projects()

    def _reap_root(trash_root, project_path):
        nonlocal _reaped, failed
        if not os.path.isdir(trash_root):
            return
        try:
            entries = os.listdir(trash_root)
        except Exception:
            return
        for entry in entries:
            full = os.path.join(trash_root, entry)
            if not os.path.isdir(full):
                continue
            try:
                mtime = os.path.getmtime(full)
            except Exception:
                continue
            if mtime < cutoff:
                try:
                    shutil.rmtree(full)
                    _reaped += 1
                except Exception as e:
                    failed += 1
                    print(f'[project-trash-reaper] WARNING: failed to reap {full}: {e}')
        # Reap tombstones older than retention too.
        if project_path:
            try:
                db.reap_delete_tombstones(project_path, cutoff_iso)
            except Exception:
                pass

    for proj in projects:
        ppath = proj.get('path') or ''
        pid = proj.get('id')
        if not ppath or not os.path.isdir(ppath):
            continue
        # New layout: <TRASH_ROOT>/<project_id>/files/<ts>
        if TRASH_ROOT and pid:
            _reap_root(os.path.join(TRASH_ROOT, str(pid), 'files'), ppath)
        # Legacy layout: <project>/.trash/files/<ts> (pre-relocation)
        _reap_root(os.path.join(ppath, '.trash', 'files'), ppath)

    if failed:
        print(f'[project-trash-reaper] WARNING: {failed} expired trash entr(ies) could not be reaped')
    return _reaped


def _audit_delete(project, hard_deleted, archived_until=None):
    """Append one line to <PROJECTS_ROOT>/.trash/_audit.log recording a project
    deletion. Best-effort; never raises."""
    try:
        os.makedirs(_TRASH_DIR, exist_ok=True)
        line = ('{ts} | pid={pid} | name={name} | eu_only={eu} | '
                'hard_deleted={hard} | archived_until={until}\n').format(
            ts=datetime.now(timezone.utc).isoformat(timespec='seconds'),
            pid=project.get('id'),
            name=project.get('name'),
            eu=1 if project.get('eu_only') else 0,
            hard=1 if hard_deleted else 0,
            until=archived_until or '',
        )
        with open(os.path.join(_TRASH_DIR, '_audit.log'), 'a') as f:
            f.write(line)
    except Exception as e:
        print(f'[delete-project] audit log write failed: {e}')


@app.route('/api/projects/<int:pid>', methods=['DELETE'])
@require_project_access('owner')
def delete_project_route(pid):
    """Delete a project and all of its data.

    - Refuses while any execution is running (HTTP 409 with the task list).
    - Closes the Scaleway session (crypto-shreds the KMS-encrypted bucket) and
      all GPU deployment windows before touching local data.
    - Backs up aingel.db first so the row + spend history are recoverable.
    - EU-only projects are hard-deleted immediately (no lingering copy — data
      residency). Non-EU projects are moved to <PROJECTS_ROOT>/.trash/ and
      hard-deleted by the startup reaper after 7 days.
    - Writes an audit line to .trash/_audit.log.
    """
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    running = _project_running_executions(project)
    if running is None:
        return jsonify({
            'error': 'Could not read project database — refusing to delete (it may be locked or corrupt)',
        }), 409
    if running:
        return jsonify({
            'error': 'Project has running executions — stop them before deleting',
            'running': running,
        }), 409

    confirmed = _project_confirmed_tasks(project)
    if confirmed is None:
        return jsonify({
            'error': 'Could not read project database — refusing to delete (it may be locked or corrupt)',
        }), 409
    if confirmed:
        return jsonify({
            'error': 'Project has confirmed (queued) tasks — cancel them before deleting',
            'confirmed': confirmed,
        }), 409

    path = project.get('path') or ''
    eu_only = bool(project.get('eu_only'))
    slug = project.get('slug') or 'project'
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')

    # R3: realpath containment guard — refuse to touch PROJECTS_ROOT itself or
    # any path that resolves outside it (symlink / ../ escapes).
    real_root = os.path.realpath(PROJECTS_ROOT)
    real_path = os.path.realpath(path) if path else ''
    if real_path and real_path != real_root and not real_path.startswith(real_root.rstrip('/') + os.sep):
        return jsonify({'error': 'Refusing to delete: project path escapes PROJECTS_ROOT'}), 400
    if real_path == real_root:
        return jsonify({'error': 'Refusing to delete PROJECTS_ROOT itself'}), 400

    # 1. Close Scaleway session (crypto-shred bucket + KMS key) if enabled.
    if project.get('scw_session_enabled'):
        try:
            res = agent_scw_session.close_session(
                project_id=pid,
                scw_project_id=project.get('scw_project_id'),
                bucket_name=project.get('scw_session_bucket'),
                kms_key_id=project.get('scw_kms_key_id'),
                region=project.get('scw_session_region') or 'fr-par',
            )
            details = res.get('details') or {}
            bucket_ok = str(details.get('bucket', '')).startswith('deleted')
            kms_ok = str(details.get('kms_key', '')).startswith('deleted')
            if eu_only and not (bucket_ok and kms_ok):
                # B1: for EU-only/confidential projects a failed crypto-shred
                # would leave the encrypted bucket/KMS key alive on Scaleway
                # with no DB row to recover it. Abort before touching local data.
                return jsonify({
                    'error': 'SCW session crypto-shred failed — refusing to delete EU project',
                    'details': details,
                }), 502
        except Exception as e:
            print(f'[delete-project] scw session close failed: {e}')
            if eu_only:
                return jsonify({'error': f'SCW session close failed — refusing to delete EU project: {e}'}), 502

    # 2. Close GPU deployment windows (stops hourly billing).
    try:
        agent_scw_deploy.close_all_windows(pid)
    except Exception as e:
        print(f'[delete-project] close_all_windows failed: {e}')

    # 3. Back up aingel.db so the row + spend history are recoverable.
    try:
        _db_path = os.path.join(BASE_DIR, 'aingel.db')
        if os.path.exists(_db_path):
            shutil.copy(_db_path, f'{_db_path}.bak-del-{slug}-{ts}')
    except Exception as e:
        print(f'[delete-project] aingel.db backup failed: {e}')

    # 4. Handle the project folder.
    archived_until = None
    hard_deleted = False
    if real_path and os.path.isdir(real_path):
        if eu_only:
            # B4: write the audit line BEFORE the irreversible hard delete so a
            # compliance trail exists even if the rmtree or later steps fail.
            _audit_delete(project, hard_deleted=True, archived_until=None)
            try:
                shutil.rmtree(real_path)
                hard_deleted = True
            except Exception as e:
                # B4: the pre-audit already logged hard_deleted=1; append a
                # corrective line so the compliance log matches disk reality.
                _audit_delete(project, hard_deleted=False, archived_until=None)
                return jsonify({'error': f'failed to remove folder: {e}'}), 500
        else:
            try:
                os.makedirs(_TRASH_DIR, exist_ok=True)
                dest = _trash_path(slug, ts)
                shutil.move(real_path, dest)
                archived_until = (datetime.now(timezone.utc) + _timedelta(days=_TRASH_RETENTION_DAYS)).isoformat(timespec='seconds')
            except Exception as e:
                return jsonify({'error': f'failed to move folder to trash: {e}'}), 500

    # 5. Clean the central DB (registries, cost rows, projects row).
    try:
        db.delete_project(pid)
    except Exception as e:
        # B2: folder may already be gone/moved — log loudly so the operator can
        # see the stale row. The row is still present, so a retry can recover.
        print(f'[delete-project] FATAL: folder handled but db.delete_project({pid}) raised {e}; row is now stale')
        return jsonify({'error': f'failed to clean database: {e}'}), 500

    # 6. Audit (non-EU only — EU already audited before the hard delete).
    if not eu_only:
        _audit_delete(project, hard_deleted, archived_until)

    return jsonify({'ok': True, 'hard_deleted': hard_deleted, 'archived_until': archived_until})


@app.route('/api/projects/<int:pid>/archive', methods=['POST'])
@require_project_access('owner')
def archive_project_route(pid):
    """Build a non-destructive .aingel.zip archive of a project.

    Non-destructive: syncs bucket-only files into Working Documents first, then
    snapshots the folder + central rows. It does NOT close the Scaleway session
    and does NOT delete anything — the existing DELETE route performs the
    crypto-shred afterwards.

    Reuses the delete route's guards (running executions / confirmed tasks /
    unreadable project.db) so an archive is never taken out from under live
    work, and returns the same 409 shapes for those cases.
    """
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    running = _project_running_executions(project)
    if running is None:
        return jsonify({
            'error': 'Could not read project database — refusing to archive (it may be locked or corrupt)',
        }), 409
    if running:
        return jsonify({
            'error': 'Project has running executions — stop them before archiving',
            'running': running,
        }), 409

    confirmed = _project_confirmed_tasks(project)
    if confirmed is None:
        return jsonify({
            'error': 'Could not read project database — refusing to archive (it may be locked or corrupt)',
        }), 409
    if confirmed:
        return jsonify({
            'error': 'Project has confirmed (queued) tasks — cancel them before archiving',
            'confirmed': confirmed,
        }), 409

    try:
        result = agent_archive.build_archive(project)
    except Exception as e:
        app.logger.exception('archive build failed for project %s', pid)
        return jsonify({'error': f'archive build failed: {e}'}), 500

    return jsonify({
        'ok': True,
        'filename': result['filename'],
        'size': result['size'],
        'download_url': f"/api/projects/{pid}/archive/{result['filename']}",
        # Expose the import cap so the frontend can warn before letting the user
        # upload an archive the server will reject with 413.
        'import_limit': _MAX_IMPORT_BYTES,
        **({'sync_warning': result['sync_warning']} if result.get('sync_warning') else {}),
    })


@app.route('/api/projects/<int:pid>/archive/<path:filename>', methods=['GET'])
@require_project_access('owner')
def download_project_archive_route(pid, filename):
    """Serve a built archive as an attachment.

    Each project's built zips live in their own `<.archives>/<pid>/` subdir, so
    this resolves the filename ONLY inside the requested pid's directory. That
    closes the IDOR where any project that could authorize a download URL could
    fish out another project's archive by its predictable name.

    Hardened like the other project-file routes: the filename must be a bare
    basename (no separators) with no '..' and no NUL, and the resolved path must
    stay strictly inside the per-pid directory. Any miss returns 404 without
    leaking path details.
    """
    # 404 (not 400) for any invalid name per the frozen contract, and to avoid
    # leaking whether a differently-named archive exists. NUL is rejected
    # explicitly because os.path.realpath() raises ValueError on it (a 500).
    if (not filename or filename != os.path.basename(filename)
            or '..' in filename or '\x00' in filename):
        return jsonify({'error': 'archive not found'}), 404
    # A leading/trailing separator would escape/confuse the join; basename
    # equality already excludes them, but be explicit.
    if filename in ('.', '') or os.sep in filename or '/' in filename:
        return jsonify({'error': 'archive not found'}), 404
    pdir = os.path.join(_ARCHIVES_DIR, str(pid))
    path = os.path.join(pdir, filename)
    real_dir = os.path.realpath(pdir)
    real_path = os.path.realpath(path)
    if not (real_path.startswith(real_dir + os.sep)):
        return jsonify({'error': 'archive not found'}), 404
    if not os.path.isfile(real_path):
        return jsonify({'error': 'archive not found'}), 404
    resp = send_file(real_path, mimetype='application/zip', as_attachment=True,
                     download_name=filename, conditional=True)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return resp


@app.route('/api/projects/import', methods=['POST'])
@require_auth
def import_project_route():
    """Restore a project from an uploaded .aingel.zip archive.

    Auth required. Enforces the free-tier project-count quota before touching
    disk. The upload is buffered to a temp file (MAX_CONTENT_LENGTH caps the
    request body; chunked import is future work) and deleted in a finally.
    """
    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return jsonify({'error': 'file field required (multipart .aingel.zip)'}), 400

    # Body-size guard. Flask already rejects > MAX_CONTENT_LENGTH with 413, but a
    # client that streams/chunks can still hand us a large file; cap explicitly.
    max_import = _MAX_IMPORT_BYTES
    cl = request.content_length
    if cl is not None and cl > max_import:
        return jsonify({
            'error': f'archive too large ({cl} bytes > {max_import} byte limit); '
                     'chunked import is not yet supported',
        }), 413

    uploader = g.get('current_user') if auth_enabled() else None
    uploader_id = uploader['id'] if uploader else None

    # Free-tier project-count quota. check_project_create is a no-op when
    # quotas_applicable() is False (auth off, admin, or no user), so calling it
    # unconditionally keeps the auth-off path zero-behavior-change.
    try:
        import agent_quotas
        agent_quotas.check_project_create(uploader_id)
    except agent_quotas.QuotaError as e:
        return jsonify({'error': str(e), 'quota': e.field, 'limit': e.limit}), 507

    import tempfile
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='aingel-import-', suffix='.zip')
        os.close(fd)
        upload.save(tmp_path)
        if os.path.getsize(tmp_path) > max_import:
            return jsonify({'error': 'archive exceeds the size limit'}), 413

        project = agent_archive.restore_archive(
            tmp_path, uploader_id, uploader_user=uploader,
            write_aingel_json=_write_aingel_json)
        return jsonify({'ok': True, 'project': project})
    except agent_archive.ArchiveValidationError as e:
        return jsonify({'error': str(e)}), 400
    except db.ArchiveConflictError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        app.logger.exception('project import failed')
        return jsonify({'error': f'import failed: {e}'}), 500
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _preflight_dep_skips(task_ids):
    """Synchronous pre-flight: which of `task_ids` would be skipped by the
    runtime dependency gate in `_run_seq`? Returns a list of
    `{'task_id', 'title', 'unmet': [{'id','title','status'}]}` for tasks that
    have at least one dependency not in 'done' state.

    The runtime gate (agent_api.py:_run_seq) skips any task whose deps aren't
    all 'done' — silently, leaving the task 'confirmed'. That silence is why
    "Run all (project)" appeared to do nothing: every eligible task was
    skipped, no execution started, no UI feedback. This helper lets the
    run-all endpoints report the skips up front so the frontend can surface
    them instead of looking like the click was ignored.
    """
    skipped = []
    for tid in task_ids:
        deps = db.get_dependencies(tid)
        unmet = [d for d in deps if d.get('status') != 'done']
        if unmet:
            t = db.get_task(tid) or {}
            skipped.append({
                'task_id': tid,
                'title': t.get('title', ''),
                'unmet': [
                    {'id': d['id'], 'title': d.get('title', ''),
                     'status': d.get('status', '')}
                    for d in unmet
                ],
            })
    return skipped


@app.route('/api/projects/<int:pid>/run-all-claude', methods=['POST'])
@require_project_access('member')
def run_all_claude(pid):
    """Run every Claude-model task of this project that the user has ALREADY
    placed in slot 1 (Claude Pro). Pending tasks are auto-confirmed and the
    runner is kicked off; if the queue exceeds one 5h window, the slot
    scheduler auto-resumes the rest at started_at + 5h.

    Unassigned tasks are never auto-moved into slot 1 — column assignment is
    always a manual user action. Tasks in slot 2/3 are likewise left alone.
    Done/running/failed/skip tasks are skipped so we don't re-run them.
    Returns {queued, slot, next_run_at}."""
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    SLOT = 1
    eligible = []
    for t in db.get_tasks(project_id=pid, include_archived=False):
        if t.get('archived'):
            continue
        if t.get('status') not in ('pending', 'confirmed'):
            continue
        model = (t.get('model') or '').lower()
        if not model.startswith('claude'):
            continue
        if t.get('work_session_slot') != SLOT:
            continue  # only act on tasks the user has manually placed in slot 1
        eligible.append(t)

    if not eligible:
        return jsonify({'queued': 0, 'slot': SLOT,
                        'message': 'No Claude tasks in slot 1 for this project. Drag tasks into Claude Pro first.'})

    ordered = sorted(eligible, key=lambda t: t.get('slot_position') or 0)
    for idx, t in enumerate(ordered):
        if t.get('slot_position') != idx or t.get('status') != 'confirmed':
            db.update_task(t['id'], slot_position=idx, status='confirmed')

    # Pre-flight: which of these would be silently skipped by the runtime dep
    # gate? Surface them so the user understands why nothing runs.
    skipped_by_deps = _preflight_dep_skips([t['id'] for t in ordered])

    # If every eligible task is blocked by unmet deps, don't bother kicking a
    # run thread that will just no-op — return a clear message instead.
    if skipped_by_deps and len(skipped_by_deps) == len(ordered):
        names = ', '.join(f"#{s['task_id']} (blocked by #{s['unmet'][0]['id']})" for s in skipped_by_deps[:3])
        return jsonify({
            'queued': 0,
            'slot': SLOT,
            'skipped_by_deps': skipped_by_deps,
            'message': f'All {len(ordered)} task(s) blocked by unmet dependencies: {names}. '
                       f'Resolve the dependency tasks (mark them done) and try again.',
        }), 200

    # Start the 5h window now (unless one is already counting down).
    db.start_work_session(SLOT, force=False)
    db.set_slot_next_run(SLOT, None, None)

    ok, msg = _kick_run_seq(SLOT, [t['id'] for t in ordered])
    if not ok:
        return jsonify({'error': msg, 'queued_for_later': len(ordered)}), 409
    return jsonify({
        'ok': True, 'slot': SLOT, 'queued': len(ordered),
        'skipped_by_deps': skipped_by_deps,
    })

@app.route('/api/projects/<int:pid>/run-all-mistral', methods=['POST'])
@require_project_access('member')
def run_all_mistral(pid):
    """Run every Mistral-model task of this project that the user has ALREADY
    placed in slot 2 (Mistral Pro). Mirrors run-all-claude but targets slot 2
    and Mistral models only. Pending tasks are auto-confirmed."""
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    SLOT = 2
    eligible = []
    for t in db.get_tasks(project_id=pid, include_archived=False):
        if t.get('archived'):
            continue
        if t.get('status') not in ('pending', 'confirmed'):
            continue
        model = (t.get('model') or '').lower()
        # Accept all Vibe-compatible Mistral models (mistral-*, codestral-*,
        # devstral-*, open-mistral-*). Scaleway models (scw-*) are excluded
        # — they run on slot 4, not slot 2.
        if not (model.startswith('mistral') or model.startswith('codestral')
                or model.startswith('devstral') or model.startswith('open-mistral')):
            continue
        if t.get('work_session_slot') != SLOT:
            continue
        eligible.append(t)

    if not eligible:
        return jsonify({'queued': 0, 'slot': SLOT,
                        'message': 'No Vibe-compatible tasks in slot 2 for this project. Drag Mistral/Codestral/Devstral tasks into Mistral Pro first.'})

    ordered = sorted(eligible, key=lambda t: t.get('slot_position') or 0)
    for idx, t in enumerate(ordered):
        if t.get('slot_position') != idx or t.get('status') != 'confirmed':
            db.update_task(t['id'], slot_position=idx, status='confirmed')

    # Pre-flight: which of these would be silently skipped by the runtime dep
    # gate? Surface them so the user understands why nothing runs.
    skipped_by_deps = _preflight_dep_skips([t['id'] for t in ordered])

    # If every eligible task is blocked by unmet deps, don't bother kicking a
    # run thread that will just no-op — return a clear message instead.
    if skipped_by_deps and len(skipped_by_deps) == len(ordered):
        names = ', '.join(f"#{s['task_id']} (blocked by #{s['unmet'][0]['id']})" for s in skipped_by_deps[:3])
        return jsonify({
            'queued': 0,
            'slot': SLOT,
            'skipped_by_deps': skipped_by_deps,
            'message': f'All {len(ordered)} task(s) blocked by unmet dependencies: {names}. '
                       f'Resolve the dependency tasks (mark them done) and try again.',
        }), 200

    db.start_work_session(SLOT, force=False)
    db.set_slot_next_run(SLOT, None, None)

    ok, msg = _kick_run_seq(SLOT, [t['id'] for t in ordered])
    if not ok:
        return jsonify({'error': msg, 'queued_for_later': len(ordered)}), 409
    return jsonify({
        'ok': True, 'slot': SLOT, 'queued': len(ordered),
        'skipped_by_deps': skipped_by_deps,
    })


@app.route('/api/projects/<int:pid>/run-all-scaleway', methods=['POST'])
@require_project_access('member')
def run_all_scaleway(pid):
    """Run every Scaleway-model task of this project that the user has ALREADY
    placed in slot 4 (EU Scaleway). Mirrors run-all-claude/mistral but targets
    slot 4 and Scaleway models only. Pending tasks are auto-confirmed."""
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    SLOT = 4
    eligible = []
    for t in db.get_tasks(project_id=pid, include_archived=False):
        if t.get('archived'):
            continue
        if t.get('status') not in ('pending', 'confirmed'):
            continue
        model = (t.get('model') or '').lower()
        # Scaleway models (scw-*) run on slot 4 (EU Scaleway). Dedicated GPU
        # deployments (scw-dep-*) are excluded — they run via their own window.
        if not model.startswith('scw-') or model.startswith('scw-dep-'):
            continue
        if t.get('work_session_slot') != SLOT:
            continue
        eligible.append(t)

    if not eligible:
        return jsonify({'queued': 0, 'slot': SLOT,
                        'message': 'No Scaleway tasks in slot 4 for this project. Drag scw-* tasks into EU Scaleway first.'})

    ordered = sorted(eligible, key=lambda t: t.get('slot_position') or 0)
    for idx, t in enumerate(ordered):
        if t.get('slot_position') != idx or t.get('status') != 'confirmed':
            db.update_task(t['id'], slot_position=idx, status='confirmed')

    # Pre-flight: which of these would be silently skipped by the runtime dep
    # gate? Surface them so the user understands why nothing runs.
    skipped_by_deps = _preflight_dep_skips([t['id'] for t in ordered])

    # If every eligible task is blocked by unmet deps, don't bother kicking a
    # run thread that will just no-op — return a clear message instead.
    if skipped_by_deps and len(skipped_by_deps) == len(ordered):
        names = ', '.join(f"#{s['task_id']} (blocked by #{s['unmet'][0]['id']})" for s in skipped_by_deps[:3])
        return jsonify({
            'queued': 0,
            'slot': SLOT,
            'skipped_by_deps': skipped_by_deps,
            'message': f'All {len(ordered)} task(s) blocked by unmet dependencies: {names}. '
                       f'Resolve the dependency tasks (mark them done) and try again.',
        }), 200

    db.start_work_session(SLOT, force=False)
    db.set_slot_next_run(SLOT, None, None)

    ok, msg = _kick_run_seq(SLOT, [t['id'] for t in ordered])
    if not ok:
        return jsonify({'error': msg, 'queued_for_later': len(ordered)}), 409
    return jsonify({
        'ok': True, 'slot': SLOT, 'queued': len(ordered),
        'skipped_by_deps': skipped_by_deps,
    })


@app.route('/api/projects/<int:project_id>/skills', methods=['GET'])
@require_project_access('viewer')
def get_project_skills(project_id):
    """Return current skills for a project (DB-backed, grouped by category).
    Falls back to live detection if the DB has none yet."""
    project = next((p for p in db.get_projects() if p['id'] == project_id), None)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    skills = db.get_project_skills(project_id)
    source = 'db'
    if not skills:
        from agent_skills import detect_skills
        detected = detect_skills(project['path'])
        skills = {
            cat: [{'name': n, 'auto_detected': True, 'detected_at': None}
                  for n in items]
            for cat, items in detected.items() if items
        }
        source = 'detected'

    skills_md_path = os.path.join(project['path'], 'Skills.md')
    return jsonify({
        'project_id':     project_id,
        'project_name':   project['name'],
        'skills':         skills,
        'source':         source,
        'skills_md_path': skills_md_path if os.path.exists(skills_md_path) else None,
    })


@app.route('/api/projects/<int:project_id>/skills/refresh', methods=['POST'])
@require_project_access('member')
def refresh_project_skills(project_id):
    """Re-detect skills, mirror to DB, and write Skills.md preserving user edits."""
    project = next((p for p in db.get_projects() if p['id'] == project_id), None)
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    from agent_skills import refresh_skills
    try:
        result = refresh_skills(project_id, project['path'], project['name'])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    return jsonify({
        'ok':             True,
        'project_id':     project_id,
        'skills_md_path': result['path'],
        'detected':       result['detected'],
    })


@app.route('/api/projects/<int:project_id>/skills/manual', methods=['POST'])
@require_project_access('member')
def add_manual_project_skill(project_id):
    """Add a user-authored skill row (category + name)."""
    data = request.json or {}
    category = (data.get('category') or '').strip()
    name     = (data.get('name') or '').strip()
    if not category or not name:
        return jsonify({'error': 'category and name required'}), 400
    db.add_manual_skill(project_id, category, name)
    return jsonify({'ok': True})


@app.route('/api/projects/<int:project_id>/skills/manual', methods=['DELETE'])
@require_project_access('member')
def delete_manual_project_skill(project_id):
    data = request.json or {}
    category = (data.get('category') or '').strip()
    name     = (data.get('name') or '').strip()
    if not category or not name:
        return jsonify({'error': 'category and name required'}), 400
    db.delete_manual_skill(project_id, category, name)
    return jsonify({'ok': True})


# ── Project permissions ──────────────────────────────────────────────────────

@app.route('/api/projects/<int:project_id>/permissions', methods=['GET'])
@require_project_access('viewer')
def get_project_permissions(project_id):
    """Return the permissions view (groups → rules with enabled/auto_added flags)."""
    project = next((p for p in db.get_projects() if p['id'] == project_id), None)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    import agent_permissions as ap
    # Auto-seed defaults on first read so the UI never shows an empty panel.
    if not db.get_project_permissions(project_id):
        ap._seed_defaults_for_project(project_id)
    return jsonify({
        'project_id': project_id,
        'groups':     ap.get_permissions_view(project_id),
    })


@app.route('/api/projects/<int:project_id>/permissions/refresh', methods=['POST'])
@require_project_access('owner')
def refresh_project_permissions(project_id):
    """Rewrite <project>/.claude/settings.json from current DB state."""
    project = next((p for p in db.get_projects() if p['id'] == project_id), None)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    import agent_permissions as ap
    try:
        result = ap.refresh_permissions(project_id, project['path'])
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({
        'ok':            True,
        'project_id':    project_id,
        'settings_path': result['path'],
        'settings':      result['settings'],
    })


@app.route('/api/projects/<int:project_id>/permissions/toggle', methods=['POST'])
@require_project_access('owner')
def toggle_project_permission(project_id):
    """Flip enabled state on an existing rule. Body: {rule, enabled}."""
    data = request.json or {}
    rule    = (data.get('rule') or '').strip()
    enabled = bool(data.get('enabled'))
    if not rule:
        return jsonify({'error': 'rule required'}), 400
    db.toggle_permission(project_id, rule, enabled)
    # Re-emit the settings.json so the on-disk file matches DB state.
    project = next((p for p in db.get_projects() if p['id'] == project_id), None)
    if project and project.get('path'):
        import agent_permissions as ap
        try:
            ap.refresh_permissions(project_id, project['path'])
        except Exception:
            pass
    return jsonify({'ok': True})


@app.route('/api/projects/<int:project_id>/permissions/custom', methods=['POST'])
@require_project_access('owner')
def add_custom_project_permission(project_id):
    """Add a user-authored permission rule. Body: {group_key, rule}."""
    data = request.json or {}
    group_key = (data.get('group_key') or '').strip()
    rule      = (data.get('rule') or '').strip()
    if not group_key or not rule:
        return jsonify({'error': 'group_key and rule required'}), 400
    db.add_custom_permission(project_id, group_key, rule)
    project = next((p for p in db.get_projects() if p['id'] == project_id), None)
    if project and project.get('path'):
        import agent_permissions as ap
        try:
            ap.refresh_permissions(project_id, project['path'])
        except Exception:
            pass
    return jsonify({'ok': True})


@app.route('/api/projects/<int:project_id>/permissions/custom', methods=['DELETE'])
@require_project_access('owner')
def delete_custom_project_permission(project_id):
    """Remove a user-added permission rule. Body: {rule}."""
    data = request.json or {}
    rule = (data.get('rule') or '').strip()
    if not rule:
        return jsonify({'error': 'rule required'}), 400
    db.delete_custom_permission(project_id, rule)
    project = next((p for p in db.get_projects() if p['id'] == project_id), None)
    if project and project.get('path'):
        import agent_permissions as ap
        try:
            ap.refresh_permissions(project_id, project['path'])
        except Exception:
            pass
    return jsonify({'ok': True})


@app.route('/api/projects/<int:project_id>/permissions/test', methods=['POST'])
@require_project_access('owner')
def test_project_permission(project_id):
    """Test a bash command against the project's permission rules.
    Body: {command: "rm -rf /"}.
    Returns {allowed, matched_rule, matched_group, reason}.
    """
    data = request.json or {}
    command = (data.get('command') or '').strip()
    if not command:
        return jsonify({'error': 'command required'}), 400
    
    project = next((p for p in db.get_projects() if p['id'] == project_id), None)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    
    import agent_permissions as ap
    result = ap.test_command(project_id, command)
    return jsonify(result)


@app.route('/api/projects/<int:pid>', methods=['PATCH'])
@require_project_access('owner')
def update_project(pid):
    data = request.json or {}
    if 'aingel_model' in data:
        new_model = (data['aingel_model'] or '').strip() or None
        # EU-only data-residency: aingel_model drives the Autopilot H2/H3/H4
        # hooks, which send task descriptions, referenced-file lists and output
        # summaries to that model. A non-EU model here leaks on every task run.
        # `eu_only` may be changing in this same PATCH — judge against the new value.
        _proj_now = db.get_project(pid) or {}
        if 'eu_only' in data:
            _proj_now = dict(_proj_now, eu_only=1 if data['eu_only'] else 0)
        ok, err = _eu_guard(_proj_now, new_model)
        if new_model and not ok:
            return _eu_reject(_proj_now, new_model, 'set_aingel_model', err)
        db.set_project_field(pid, 'aingel_model', new_model)
        # Keep the AIngel chat model in sync
        proj_now = db.get_project(pid)
        if proj_now and proj_now.get('aingel_chat_id') and new_model:
            db.update_chat(proj_now['aingel_chat_id'], model=new_model)
    # Accept either historical 'budget_monthly' or spec name 'monthly_budget'
    budget = data.get('monthly_budget', data.get('budget_monthly'))
    reset_day = data.get('budget_reset_day') or data.get('reset_day')
    if budget is not None or reset_day is not None:
        db.update_project_budget(
            pid,
            monthly_budget=float(budget) if budget is not None else None,
            reset_day=reset_day,
        )
    # Project-level fields
    if 'project_type' in data:
        db.set_project_field(pid, 'project_type', data['project_type'])
    if 'eu_only' in data:
        db.set_project_field(pid, 'eu_only', 1 if data['eu_only'] else 0)
        # Turning EU-only on must not leave a non-EU aingel_model behind — it
        # would keep feeding Autopilot briefs to a US provider. Swap it to the
        # EU default rather than clearing it, so Autopilot keeps working.
        if data['eu_only'] and 'aingel_model' not in data:
            _p = db.get_project(pid) or {}
            if _p.get('aingel_model') and not _is_eu_model(_p['aingel_model']):
                _eu_model = agent_config.default_model_for(_p)
                db.set_project_field(pid, 'aingel_model', _eu_model)
                if _p.get('aingel_chat_id'):
                    db.update_chat(_p['aingel_chat_id'], model=_eu_model)
    if 'llm_mode' in data:
        db.set_project_field(pid, 'llm_mode', data['llm_mode'])
    if 'execution_type' in data:
        db.set_project_field(pid, 'execution_type', data['execution_type'] or 'standard')
    if 'aingel_name' in data:
        db.set_project_field(pid, 'aingel_name', data['aingel_name'] or None)
    # Phase 7: AIngel Autopilot toggles
    if 'aingel_autopilot' in data:
        _ap_val = 1 if data['aingel_autopilot'] else 0
        # Free-tier gate: cannot enable autopilot
        if _ap_val:
            _u = g.get('current_user') if auth_enabled() else None
            if _u is not None:
                try:
                    import agent_quotas
                    if not agent_quotas.can_use_autopilot(_u['id']):
                        return jsonify({'error': 'Guide autopilot is not available on the free tier.'}), 403
                except Exception:
                    pass
        db.set_project_field(pid, 'aingel_autopilot', _ap_val)
    if 'aingel_mode' in data:
        mode = (data['aingel_mode'] or 'advisory').strip()
        if mode not in ('advisory', 'strict'):
            mode = 'advisory'
        db.set_project_field(pid, 'aingel_mode', mode)
    # RAG library: per-project availability + default corpus (shared factory /opt/RAG)
    if 'use_rag' in data:
        db.set_project_field(pid, 'use_rag', 1 if data['use_rag'] else 0)
    if 'rag_corpus_id' in data:
        db.set_project_field(pid, 'rag_corpus_id', (data['rag_corpus_id'] or 'railway').strip())
    # Sync aingel.json with updated DB state
    updated = db.get_project(pid)
    if updated and updated.get('path'):
        _write_aingel_json(updated['path'], updated)
    return jsonify({'ok': True})


# ── Phase 2: Scaleway session storage ────────────────────────────────────────

@app.route('/api/projects/<int:pid>/scw-session', methods=['POST'])
@require_project_access('owner')
def create_scw_session(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': f'project {pid} not found'}), 404
        if not (agent_config.IS_VAULT or agent_config.eu_only_for(proj)):
            return jsonify({'error': 'Scaleway sessions require IS_VAULT or an EU-only project'}), 403
        if proj.get('scw_session_enabled') and proj.get('scw_project_id'):
            return jsonify({
                'ok': True,
                'already_exists': True,
                'enabled': True,
                'project_id': proj.get('scw_project_id'),
                'bucket': proj.get('scw_session_bucket'),
                'kms_key_id': proj.get('scw_kms_key_id'),
                'region': proj.get('scw_session_region') or 'fr-par',
                'created_at': proj.get('scw_session_created_at'),
            })
        result = agent_scw_session.create_session(
            project_id=pid, project_name=proj.get('name') or f'project-{pid}'
        )
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'session create failed')}), 500
        for k in ('scw_project_id', 'scw_session_bucket', 'scw_kms_key_id',
                  'scw_session_region'):
            if result.get(k) is not None:
                db.set_project_field(pid, k, result[k])
        db.set_project_field(pid, 'scw_session_enabled', 1)
        updated = db.get_project(pid)
        if updated and updated.get('path'):
            _write_aingel_json(updated['path'], updated)
        return jsonify({
            'ok': True,
            'enabled': True,
            'project_id': updated.get('scw_project_id'),
            'bucket': updated.get('scw_session_bucket'),
            'kms_key_id': updated.get('scw_kms_key_id'),
            'region': updated.get('scw_session_region') or 'fr-par',
            'created_at': updated.get('scw_session_created_at'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/scw-session', methods=['DELETE'])
@require_project_access('owner')
def close_scw_session(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': f'project {pid} not found'}), 404
        result = agent_scw_session.close_session(
            project_id=pid,
            scw_project_id=proj.get('scw_project_id'),
            bucket_name=proj.get('scw_session_bucket'),
            kms_key_id=proj.get('scw_kms_key_id'),
            region=proj.get('scw_session_region') or 'fr-par',
        )
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'session close failed')}), 500
        try:
            agent_scw_deploy.close_all_windows(pid)
        except Exception as e:
            print(f'[scw-session] close_all_windows failed: {e}')
        for k in ('scw_session_enabled', 'scw_project_id', 'scw_session_bucket',
                  'scw_kms_key_id', 'scw_session_region', 'scw_session_created_at'):
            try:
                db.set_project_field(pid, k, None if k != 'scw_session_enabled' else 0)
            except Exception:
                pass
        updated = db.get_project(pid)
        if updated and updated.get('path'):
            _write_aingel_json(updated['path'], updated)
        return jsonify({'ok': True, 'details': result.get('details', {})})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/scw-session')
@require_project_access('owner')
def get_scw_session(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': f'project {pid} not found'}), 404
        enabled = bool(proj.get('scw_session_enabled'))
        costs = None
        if enabled and proj.get('scw_project_id'):
            try:
                costs_result = agent_scw_session.pull_costs(
                    scw_project_id=proj['scw_project_id'], project_id=pid,
                    region=proj.get('scw_session_region') or 'fr-par',
                )
                if costs_result.get('ok') and costs_result.get('costs'):
                    costs = costs_result['costs']
            except Exception:
                pass
        return jsonify({
            'enabled': enabled,
            'project_id': proj.get('scw_project_id'),
            'bucket': proj.get('scw_session_bucket'),
            'kms_key_id': proj.get('scw_kms_key_id'),
            'region': proj.get('scw_session_region') or 'fr-par',
            'created_at': proj.get('scw_session_created_at'),
            'costs': costs or [],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/scw-session/upload', methods=['POST'])
@require_project_access('owner')
def upload_scw_session_file(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': f'project {pid} not found'}), 404
        bucket = proj.get('scw_session_bucket')
        region = proj.get('scw_session_region') or 'fr-par'
        key = (request.form.get('key') or '').strip()
        source_path = (request.form.get('path') or '').strip()
        uploaded = request.files.get('file')
        if uploaded and not source_path:
            tmp_dir = os.path.join('/tmp', 'scw-session-uploads', str(pid))
            os.makedirs(tmp_dir, exist_ok=True)
            safe_name = os.path.basename(uploaded.filename or 'upload')
            source_path = os.path.join(tmp_dir, safe_name)
            uploaded.save(source_path)
        if not source_path:
            return jsonify({'error': 'file or path required'}), 400
        if not key:
            key = os.path.basename(source_path)
        result = agent_scw_session.upload_file(
            project_path=source_path, bucket_name=bucket, key=key, region=region,
        )
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'upload failed')}), 500
        # Immediate sync to Working Documents so the file appears in Files > Reference
        # without waiting for the next task execution (the previous UX gap for
        # attachments-33 / Upload folder). Best-effort: upload success is already
        # confirmed, a sync failure must not turn the whole request into a 500.
        sync_synced = 0
        sync_error = None
        try:
            project_path = proj.get('path') or ''
            if project_path and bucket:
                # Prefer per-key download for immediate visibility (avoids full bucket scan).
                # Fall back to full bucket sync if per-key helpers are unavailable.
                wd_candidates = ['Working Documents', 'Working Docs', 'My Docs']
                wd = None
                for name in wd_candidates:
                    cand = os.path.join(project_path, name)
                    if os.path.isdir(cand):
                        wd = cand
                        break
                if not wd:
                    wd = os.path.join(project_path, 'Working Documents')
                    os.makedirs(wd, exist_ok=True)
                dest = os.path.join(wd, key)
                dest_dir = os.path.dirname(dest)
                if dest_dir and not os.path.isdir(dest_dir):
                    os.makedirs(dest_dir, exist_ok=True)
                dl = agent_scw_session.download_file(bucket, key, dest, region=region)
                if dl.get('ok'):
                    sync_synced = 1
                else:
                    sync_error = dl.get('error', 'download failed')
                    # Fallback: full sync (handles bucket-prefix folders)
                    fb = agent_scw_session.sync_bucket_to_working_docs(pid, project_path, region=region)
                    if fb.get('ok'):
                        sync_synced = int(fb.get('synced', 0) or 0)
                        sync_error = None
        except Exception as _e:
            sync_error = str(_e)
        # Notify files catalog change so SSE/file watchers can refresh
        try:
            agent_events.emit(pid, {'type': 'files_changed', 'key': key, 'synced': sync_synced})
        except Exception:
            pass
        resp = {'ok': True, 'key': key, 'size': result.get('size', 0), 'synced': sync_synced}
        if sync_error:
            resp['sync_warning'] = sync_error
        return jsonify(resp)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/scw-session/sync', methods=['POST'])
@require_project_access('owner')
def sync_scw_session_files(pid):
    """Manually sync the SCW bucket to Working Documents.

    Mirrors the executor's pre-run sync but callable on demand after an Upload
    folder operation so Files > Reference is up to date without running a task.
    """
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': f'project {pid} not found'}), 404
        if not proj.get('scw_session_enabled') or not proj.get('scw_session_bucket'):
            return jsonify({'error': 'no active SCW session for this project'}), 400
        project_path = proj.get('path') or ''
        region = proj.get('scw_session_region') or 'fr-par'
        result = agent_scw_session.sync_bucket_to_working_docs(pid, project_path, region=region)
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'sync failed')}), 500
        try:
            agent_events.emit(pid, {'type': 'files_changed', 'synced': int(result.get('synced', 0) or 0)})
        except Exception:
            pass
        return jsonify({'ok': True, 'synced': int(result.get('synced', 0) or 0)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/scw-session/files')
@require_project_access('owner')
def list_scw_session_files(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': f'project {pid} not found'}), 404
        bucket = proj.get('scw_session_bucket')
        region = proj.get('scw_session_region') or 'fr-par'
        prefix = request.args.get('prefix', '')
        result = agent_scw_session.list_files(
            bucket_name=bucket, region=region, prefix=prefix,
        )
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'list failed')}), 500
        return jsonify({'files': result.get('files', [])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Phase 3: GPU deployments ────────────────────────────────────────────────

@app.route('/api/projects/<int:pid>/deployments')
@require_project_access('owner')
def list_deployments(pid):
    try:
        conn = db.get_db()
        rows = conn.execute(
            'SELECT * FROM scw_deployments WHERE project_id=? ORDER BY id DESC',
            (pid,),
        ).fetchall()
        conn.close()
        deployments = [dict(r) for r in rows]
        return jsonify({'deployments': deployments})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/deployments', methods=['POST'])
@require_project_access('owner')
def open_deployment(pid):
    try:
        data = request.json or {}
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': f'project {pid} not found'}), 404
        model_name = (data.get('model_name') or '').strip()
        node_type = (data.get('node_type') or 'L4').strip()
        endpoint_kind = (data.get('endpoint_kind') or 'public').strip()
        idle_delete_minutes = int(data.get('idle_delete_minutes') or 30)
        if not model_name:
            return jsonify({'error': 'model_name required'}), 400
        region = (data.get('region') or proj.get('scw_session_region') or 'fr-par').strip()
        result = agent_scw_deploy.open_window(
            project_id=pid, model_name=model_name, node_type=node_type,
            endpoint_kind=endpoint_kind, idle_delete_minutes=idle_delete_minutes,
            region=region,
        )
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'open_window failed')}), 500
        conn = db.get_db()
        row = conn.execute(
            'SELECT * FROM scw_deployments WHERE scw_deployment_id=?',
            (result.get('deployment_id'),),
        ).fetchone()
        conn.close()
        deployment = dict(row) if row else None
        return jsonify({'ok': True, 'deployment': deployment})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/deployments/<int:dep_id>', methods=['DELETE'])
@require_project_access('owner')
def close_deployment(pid, dep_id):
    try:
        result = agent_scw_deploy.close_window(dep_id)
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'close_window failed')}), 500
        return jsonify({'ok': True, 'final_cost_usd': result.get('final_cost_usd', 0.0)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/deployments/models')
@require_project_access('owner')
def list_deployable_models(pid):
    try:
        proj = db.get_project(pid)
        region = (proj or {}).get('scw_session_region') or 'fr-par'
        models = agent_scw_deploy.list_models(region=region)
        return jsonify({'models': models})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Cross-project GPU run window (Hugging Face Scout Phase 2) ────────────────

def _ensure_hf_roster(project_id, repo_id):
    """Adopting an HF model on a task should also add it to the project's roster
    so the global picker and Settings counts stay consistent with task assignment."""
    if not repo_id:
        return
    try:
        entry = hf_catalog.catalog_entry(repo_id)
        if entry is None:
            return
        model_id = (entry.get('provider_mapping') or '').strip()
        label = entry.get('label') or repo_id
        provider = MODELS.get(model_id, {}).get('provider', '')
        score = float((entry.get('validation') or {}).get('score') or 0.0)
        db.add_project_hf_model(project_id, repo_id, model_id=model_id, label=label,
                                provider=provider, validation_score=score)
    except Exception as e:
        app.logger.warning('ensure_hf_roster failed for %s: %s', repo_id, e)


def _hf_queue_group(allowed_ids=None):
    """Group cross-project HF-assigned tasks by their adopted repo, annotating
    each group with its resolved serving model and servability. When
    `allowed_ids` is set (Phase 3C scoping for non-admins), only tasks in
    those projects are included and empty groups are dropped.

    Groups also carry custom-model import info (single batched query): a
    ``ready`` import makes the group servable even without a serverless
    provider mapping (deployable via a GPU window)."""
    tasks = db.hf_queue_tasks()
    if allowed_ids is not None:
        tasks = [t for t in tasks if t.get('project_id') in allowed_ids]
    imports = _import_map()
    groups = {}
    for t in tasks:
        repo_id = t.get('hf_repo_id') or ''
        if not repo_id:
            continue
        g = groups.setdefault(repo_id, {
            'repo_id': repo_id,
            'label': repo_id,
            'provider_mapping': '',
            'servable': False,
            'import_status': None,
            'import_model_name': None,
            'import_ready': False,
            'tasks': [],
        })
        entry = hf_catalog.catalog_entry(repo_id) or {}
        g['label'] = entry.get('label') or repo_id
        g['provider_mapping'] = (entry.get('provider_mapping') or '').strip()
        imp = imports.get(repo_id) or {}
        g['import_status'] = imp.get('status')
        g['import_model_name'] = imp.get('model_name')
        g['import_ready'] = (imp.get('status') == 'ready')
        g['servable'] = bool(g['provider_mapping']) or g['import_ready']
        g['tasks'].append({
            'id': t['id'],
            'title': t.get('title') or '',
            'status': t.get('status') or '',
            'model': t.get('model') or '',
            'awaiting_model': bool(t.get('awaiting_model')),
            'project_id': t.get('project_id'),
            'project_name': t.get('project_name') or '',
        })
    groups = list(groups.values())
    groups.sort(key=lambda g: (not g['servable'], g['repo_id']))
    return groups


@app.route('/api/hf-queue')
@require_auth
def hf_queue():
    try:
        # Phase 3C: task titles + project names leak membership — non-admins
        # see only their own projects' queued tasks.
        allowed_ids = None
        if auth_enabled():
            allowed = get_user_project_ids(g.current_user)
            if allowed is not None:
                allowed_ids = set(allowed)
        return jsonify({'groups': _hf_queue_group(allowed_ids)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/gpu-window')
@require_auth
def list_gpu_windows():
    try:
        windows = db.get_shared_windows()
        # Phase 3C: windows are shared infra (visible to any logged-in user),
        # but per-project cost attribution is membership-scoped for non-admins.
        if auth_enabled():
            allowed = get_user_project_ids(g.current_user)
            if allowed is not None:
                for w in windows:
                    calls = [c for c in (w.get('calls') or [])
                             if c.get('project_id') in allowed]
                    w['calls'] = calls
                    by_project = {}
                    for c in calls:
                        pid = c.get('project_id')
                        if pid is None:
                            continue
                        by_project[pid] = by_project.get(pid, 0.0) + float(c.get('cost_usd') or 0.0)
                    w['cost_by_project'] = by_project
        return jsonify({'windows': windows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/gpu-window/models')
@require_auth
def list_gpu_window_models():
    """Return the Hugging Face models assigned to tasks (across projects), each
    annotated with its servability and — when servable — the matching Scaleway
    deployable model(s) with node types, hourly cost and size. The GPU window is
    for HF models only: serverless scw-* models need no GPU."""
    try:
        deployable = agent_scw_deploy.list_models(region='fr-par')
        # Reconcile any out-of-band custom models (console imports) so the picker
        # and statuses reflect them without a manual re-import.
        _sync_library_imports(deployable)
        # Phase 3C: the task queue feeding this catalog is membership-scoped
        # for non-admins (same as /api/hf-queue); node catalog stays global.
        allowed_ids = None
        if auth_enabled():
            allowed = get_user_project_ids(g.current_user)
            if allowed is not None:
                allowed_ids = set(allowed)
        groups = _hf_queue_group(allowed_ids)
        out = []
        # NOTE: loop var is `grp`, not `g` — `g` is Flask's request-global
        # used above; reusing it here would shadow it (UnboundLocalError).
        for grp in groups:
            mapping = grp['provider_mapping']
            params_b = _estimate_params_b(grp['repo_id'])
            options = []
            if mapping:
                # Match the resolved scw-* family to deployable Scaleway models.
                # Use the first 3 segments of the serverless name as the family
                # key so e.g. 'qwen3-coder-30b-a3b-instruct' → 'qwen3-coder-30b'
                # matches only the coder variant, not every qwen3 model.
                from agent_router import _SCW_MODEL_MAP
                fam = _SCW_MODEL_MAP.get(mapping, '')
                fam_key = '-'.join(fam.split('-')[:3]) if fam else ''
                for m in deployable:
                    name = m.get('name') or ''
                    if fam_key and fam_key in name:
                        options.append(m)
            else:
                # No catalogue mapping → a fine-tune awaiting self-hosting. Prefer
                # the exact Scaleway model imported for this repo (by name in
                # scw_model_imports); fall back to the name-token heuristic for
                # models imported out-of-band via the Scaleway console.
                imported = db.find_active_model_import(grp['repo_id']) or {}
                imported_name = (imported.get('model_name') or '').lower()
                for m in deployable:
                    if not m.get('custom'):
                        continue
                    name = (m.get('name') or '').lower()
                    if imported_name and name == imported_name:
                        options.insert(0, m)
                    elif _custom_model_matches(grp['repo_id'], m):
                        options.append(m)
            est_node = _node_for_params(params_b)
            est_eur = agent_scw_deploy._hourly_rate(est_node)
            if options:
                est_node, est_eur = _recommended_node_for_options(options)
            out.append({
                'repo_id': grp['repo_id'],
                'label': grp['label'],
                'servable': bool(mapping) or bool(options),
                'provider_mapping': mapping,
                'task_count': len(grp['tasks']),
                'params_b': params_b,
                'estimated_node': est_node,
                'estimated_hourly_eur': est_eur,
                'options': options,
            })
        return jsonify({'models': out})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _estimate_params_b(repo_id):
    """Best-effort parameter count (billions) from a HF repo id, e.g. 'Bielik-7B'
    → 7. Returns 0 when unknown."""
    import re
    m = re.search(r'(\d+(?:\.\d+)?)\s*[bB]', repo_id or '')
    if m:
        return float(m.group(1))
    return 0.0


def _node_for_params(params_b):
    """Minimum Scaleway node type that comfortably fits a model of `params_b`
    billion parameters (4-bit quantised). Advisory only."""
    if params_b <= 0:
        return 'L4'
    if params_b <= 8:
        return 'L4'
    if params_b <= 30:
        return 'L40S'
    if params_b <= 70:
        return 'H100'
    if params_b <= 120:
        return 'H100-2'
    return 'H100-SXM-8'


def _recommended_node_for_options(options):
    """Cheapest in-stock node type among the deployable options, respecting the
    model's real allowed node_types + stock (a param-count heuristic would keep
    recommending L4 for a custom fp32 model that can't run on L4 at all).
    Falls back to the cheapest node regardless of stock. Returns (node, eur/h)."""
    candidates = []
    for m in options:
        for nt in m.get('node_types') or []:
            stock = (m.get('stock_status') or {}).get(nt, '')
            candidates.append((agent_scw_deploy._hourly_rate(nt), nt, stock))
    if not candidates:
        return 'L4', agent_scw_deploy._hourly_rate('L4')
    in_stock = [c for c in candidates if c[2] == 'available']
    pick = (sorted(in_stock, key=lambda c: c[0]) or sorted(candidates, key=lambda c: c[0]))[0]
    return pick[1], pick[0]


def _repo_tokens(repo_id):
    """Normalised model tokens from an HF repo id, e.g. '4x32/Bielik-7B-polish-law'
    → ['bielik', 'polish', 'law']. Quant/format/version suffixes and numeric parts
    are dropped so a Scaleway custom-model name can be matched against them."""
    import re
    seg = (repo_id or '').rsplit('/', 1)[-1].lower()
    seg = re.sub(r'[-_.](gguf|safetensors|q\d[_-]?k[_-]?m|q\d+|int\d+|fp\d+|bf16|awq|gptq)\b', ' ', seg)
    return [t for t in re.split(r'[^a-z0-9]+', seg) if len(t) >= 3 and not t.isdigit()]


def _custom_model_matches(repo_id, model):
    """True when an imported Scaleway custom model's name shares a token with the
    HF repo id. The import step is console-only and the model name is arbitrary,
    so this is a best-effort link between a queued HF fine-tune and the custom
    model that was imported for it."""
    name = (model.get('name') or '').lower()
    tokens = _repo_tokens(repo_id)
    return bool(tokens) and any(t in name for t in tokens)


@app.route('/api/gpu-window', methods=['POST'])
@require_admin
def open_gpu_window():
    try:
        data = request.json or {}
        model_name = (data.get('model_name') or '').strip()
        node_type = (data.get('node_type') or 'L4').strip()
        endpoint_kind = (data.get('endpoint_kind') or 'public').strip()
        idle_delete_minutes = int(data.get('idle_delete_minutes') or 30)
        if not model_name:
            return jsonify({'error': 'model_name required'}), 400
        result = agent_scw_deploy.open_shared_window(
            model_name=model_name, node_type=node_type,
            endpoint_kind=endpoint_kind, idle_delete_minutes=idle_delete_minutes,
            region='fr-par',
        )
        if not result.get('ok'):
            code = result.get('error_code')
            status = 422 if code else 500
            return jsonify({'error': result.get('error', 'open_shared_window failed'),
                            'error_code': code}), status
        conn = db.get_db()
        row = conn.execute(
            'SELECT * FROM scw_deployments WHERE scw_deployment_id=?',
            (result.get('deployment_id'),),
        ).fetchone()
        conn.close()
        return jsonify({'ok': True, 'deployment': dict(row) if row else None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Custom model import (Hugging Face → Scaleway model library, beta) ────────

def _model_name_for_repo(repo_id):
    """Derive a unique-ish Scaleway model name from a HF repo id. Scaleway only
    allows alphanumerics, dots, spaces and dashes, and the name must be unique
    within the Organization/Project."""
    import re
    import uuid
    base = (repo_id or '').strip().rstrip('/').rsplit('/', 1)[-1]
    base = re.sub(r'[^A-Za-z0-9.\- ]+', '-', base).strip('- ')
    if not base:
        base = 'hf-model'
    suffix = uuid.uuid4().hex[:6]
    return f'{base[:48]}-{suffix}'


@app.route('/api/hf-models/verify', methods=['POST'])
@require_auth
def hf_model_verify():
    """Authoritative pre-flight for an HF repo via Scaleway's verify-model
    endpoint. Creates nothing; returns allowed nodes/quantizations/size or a
    friendly rejection (e.g. GGUF/tokenizer-less repos)."""
    data = request.get_json(silent=True) or {}
    repo_id = (data.get('repo_id') or '').strip()
    if not repo_id:
        return jsonify({'error': 'repo_id is required'}), 400
    try:
        result = agent_scw_deploy.verify_hf_model(repo_id)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'error_code': 'error'}), 502
    status = 200 if result.get('ok') else 422
    return jsonify(result), status


@app.route('/api/projects/<int:pid>/hf-models/import', methods=['POST'])
@require_project_access('owner')
def hf_model_import(pid):
    """Import an HF repo into the Scaleway model library, initiated from a project.

    The Scaleway library model is Organization-global, so once imported any
    project can deploy it; but the import is always attributed to the project it
    was started from, and on success the repo is added to that project's roster —
    which makes it visible (cross-project) in every task form. Import is free;
    billing starts only on deployment."""
    data = request.get_json(silent=True) or {}
    repo_id = (data.get('repo_id') or '').strip()
    if not repo_id:
        return jsonify({'error': 'repo_id is required'}), 400
    model_name = (data.get('model_name') or '').strip() or _model_name_for_repo(repo_id)

    # Reuse an already-imported/in-flight model for this repo instead of pulling
    # the same weights again (each import creates a new Scaleway library model).
    if not data.get('model_name'):
        existing = db.find_active_model_import(repo_id)
        if existing:
            # Register synchronously without the HF metadata lookup (no network on
            # the request thread); the ready-path poll enriches the row later.
            _register_import_roster(pid, repo_id, fetch_meta=False)
            # If the row is still in-flight (e.g. the original poller died in a
            # server restart), resume polling so it can't stay stuck forever.
            if (existing.get('scw_model_id')
                    and existing.get('status') in ('preparing', 'downloading')):
                _launch_import_poll(existing['id'], existing['scw_model_id'], pid, repo_id)
            return jsonify({'ok': True, 'import': existing,
                            'model_id': existing.get('scw_model_id'),
                            'status': existing.get('status'), 'reused': True})

    try:
        model = agent_scw_deploy.import_hf_model(
            name=model_name, repo_id=repo_id, project_id=data.get('scw_project_id'))
    except agent_scw_deploy.ScwImportError as e:
        return jsonify({'error': e.friendly, 'error_code': e.kind}), 422
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    scw_model_id = model.get('id') or ''
    status = model.get('status') or 'preparing'
    import_id = db.add_model_import(
        pid, repo_id, model_name, scw_model_id=scw_model_id, status=status,
        size_bytes=model.get('size_bytes'))
    # Adopt immediately so the project roster shows the in-flight status chip
    # (preparing/downloading) rather than only appearing once ready.
    _register_import_roster(pid, repo_id, fetch_meta=False)

    if scw_model_id:
        _launch_import_poll(import_id, scw_model_id, pid, repo_id)
    else:
        # Scaleway returned no model id — nothing to poll, and a 1-hour poll of an
        # empty id would only 404. Record it as failed immediately.
        db.update_model_import(import_id, status='failed',
                               error_message='Scaleway did not return a model id.')

    row = db.get_model_import(import_id)
    return jsonify({'ok': True, 'import': row, 'model_id': scw_model_id,
                    'status': status})


# In-flight import pollers, keyed by scw_model_id, so a second Import click on a
# still-downloading model reuses the running poller instead of spawning a rival
# thread (which could time out and flip the row to 'failed' early).
_IMPORT_POLLS = {}
_IMPORT_POLLS_LOCK = threading.Lock()

# Throttle for _sync_library_imports when it must fetch the Scaleway library
# itself (the None path, hit on every roster/adopted request). A provided payload
# bypasses the throttle. In-flight status is also advanced directly by the poller.
_SYNC_LAST = {'ts': 0.0}
_SYNC_LOCK = threading.Lock()
_SYNC_MIN_INTERVAL = 60.0


def _launch_import_poll(import_id, scw_model_id, project_id, repo_id):
    """Start a daemon thread polling a Scaleway model until ready, then update the
    import row and enrich the roster. Deduplicated per ``scw_model_id`` so the
    reuse path can't spawn a second poller. Wrapped so an unexpected error marks
    the row failed instead of silently killing the thread and stranding it."""
    if not scw_model_id:
        return
    with _IMPORT_POLLS_LOCK:
        existing = _IMPORT_POLLS.get(scw_model_id)
        if existing is not None and existing.is_alive():
            return

    def _poll():
        try:
            ready = agent_scw_deploy.wait_model_ready(scw_model_id)
            if not ready:
                db.update_model_import(import_id, status='failed',
                                       error_message='Import did not complete in time '
                                                     '(model may still be downloading).')
                return
            final = ready.get('status') or 'error'
            db.update_model_import(
                import_id, status=final,
                error_message=(ready.get('error_message') or None) if final == 'error' else None)
            if final == 'ready':
                _register_import_roster(project_id, repo_id, fetch_meta=True)
        except Exception as e:
            app.logger.warning('import poll for %s failed: %s', scw_model_id, e)
            try:
                db.update_model_import(import_id, status='failed',
                                       error_message=f'Import tracking error: {e}')
            except Exception:
                pass
        finally:
            with _IMPORT_POLLS_LOCK:
                if _IMPORT_POLLS.get(scw_model_id) is threading.current_thread():
                    _IMPORT_POLLS.pop(scw_model_id, None)

    t = threading.Thread(target=_poll, daemon=True)
    with _IMPORT_POLLS_LOCK:
        _IMPORT_POLLS[scw_model_id] = t
    t.start()


def _register_import_roster(project_id, repo_id, fetch_meta=True):
    """Adopt an imported repo into a project's roster so the task-form HF picker
    (which reads the global roster cross-project) lists it. Best-effort: the
    Scaleway library model already exists regardless of roster state, so a failed
    Hugging Face metadata lookup must not prevent the roster row. ``fetch_meta``
    is False on request-thread paths to avoid a blocking HF API call."""
    label = repo_id
    score = 0.0
    if fetch_meta:
        try:
            entry = hf_catalog.catalog_entry(repo_id)
            if entry is None:
                entry = hf_catalog.register_model(
                    repo_id, hf_catalog._normalize(hf_catalog.get_model_card(repo_id)))
            label = entry.get('label') or repo_id
            score = float((entry.get('validation') or {}).get('score') or 0.0)
        except Exception as e:
            app.logger.warning('import roster metadata lookup failed for %s: %s', repo_id, e)
    try:
        db.add_project_hf_model(project_id, repo_id, model_id='', label=label,
                                provider='scaleway-deployment', validation_score=score)
    except Exception as e:
        app.logger.warning('register import roster failed for %s: %s', repo_id, e)


@app.route('/api/hf-models/imports', methods=['GET'])
@require_auth
def hf_model_imports():
    """List custom-model import attempts (membership-scoped for non-admins)."""
    try:
        project_id = request.args.get('project_id', type=int)
        if project_id and not db.get_project(project_id):
            return jsonify({'error': f'project {project_id} not found'}), 404
        rows = db.get_model_imports(project_id=project_id)
        if auth_enabled():
            allowed = get_user_project_ids(g.current_user)
            if allowed is not None:
                # A specific non-member project_id → 404 (anti-enumeration);
                # otherwise filter rows to the caller's member projects. Rows
                # with project_id NULL (org-wide imports) are admin-only.
                if project_id is not None and project_id not in allowed:
                    return jsonify({'error': 'not_found'}), 404
                rows = [r for r in rows if r.get('project_id') in allowed]
        return jsonify({'imports': rows})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/hf-models/imports/<int:import_id>', methods=['DELETE'])
@require_admin
def hf_model_import_delete(import_id):
    """Delete an imported model from the Scaleway library (stops it counting
    toward quota) and drop the local import row."""
    row = db.get_model_import(import_id)
    if not row:
        return jsonify({'error': f'import {import_id} not found'}), 404
    if row.get('scw_model_id'):
        result = agent_scw_deploy.delete_model(row['scw_model_id'])
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'delete_model failed')}), 500
    db.remove_model_import(import_id)
    return jsonify({'ok': True})


# In-memory progress for GPU-window runs, keyed by deployment DB id. Each entry
# tracks the queued tasks and their live status so the UI can show RUN actually
# doing something (and the real error) instead of silently failing in background.
_GPU_RUN_STATE = {}
_GPU_RUN_LOCK = threading.Lock()


@app.route('/api/gpu-window/<int:dep_id>/run', methods=['POST'])
@require_admin
def run_gpu_window(dep_id):
    try:
        conn = db.get_db()
        row = conn.execute('SELECT * FROM scw_deployments WHERE id=?', (dep_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'error': f'window {dep_id} not found'}), 404
        dep = dict(row)
        if dep.get('status') != 'ready':
            return jsonify({'error': f'window is not ready (status: {dep.get("status")})'}), 409
        model_id = agent_scw_deploy.get_deployment_model_id(dep['scw_deployment_id'])
        task_ids = [int(x) for x in (request.json or {}).get('task_ids', []) if x]
        if not task_ids:
            return jsonify({'error': 'task_ids required'}), 400

        # Assign each task to the window's model and clear the awaiting flag so
        # run_task will actually execute it.
        assigned = []
        for tid in task_ids:
            task = db.get_task(tid)
            if not task:
                continue
            db.update_task(tid, model=model_id, awaiting_model=0)
            assigned.append(tid)

        # Small-context advisory: a 4096-token fine-tune cannot process a large
        # referenced corpus; run_task refuses pathological batches, so warn here
        # before the user waits (and pays) on a run that will only bounce back.
        warnings = []
        ctx = dep.get('max_context_size')
        if ctx and int(ctx) < 32768:
            warnings.append(
                f"{dep['model_name']} has a {int(ctx)}-token context — tasks that reference "
                'large files may be refused or run very slowly. Use a large-context model '
                '(e.g. scw-qwen3.6-35b) for big-document work.')

        state = {'dep_id': dep_id, 'started_at': datetime.utcnow().isoformat(),
                 'tasks': {tid: {'status': 'queued', 'error': ''} for tid in assigned}}
        with _GPU_RUN_LOCK:
            _GPU_RUN_STATE[dep_id] = state

        def _run():
            for tid in assigned:
                with _GPU_RUN_LOCK:
                    _st = _GPU_RUN_STATE.get(dep_id, {}).get('tasks', {})
                    if tid in _st:
                        _st[tid]['status'] = 'running'
                try:
                    res = run_task(tid)
                except Exception as e:
                    res = None
                    app.logger.warning('gpu-window run task %s failed: %s', tid, e)
                    err = str(e)
                else:
                    err = (res.get('error') if isinstance(res, dict) else '') or ''
                done = isinstance(res, dict) and res.get('status') == 'done'
                with _GPU_RUN_LOCK:
                    _st = _GPU_RUN_STATE.get(dep_id, {}).get('tasks', {})
                    if tid in _st:
                        _st[tid]['status'] = 'done' if done else 'failed'
                        _st[tid]['error'] = err

        threading.Thread(target=_run, daemon=True).start()
        return jsonify({'ok': True, 'queued': len(assigned), 'model_id': model_id,
                        'warnings': warnings})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/gpu-window/<int:dep_id>/run-status')
@require_admin
def gpu_window_run_status(dep_id):
    with _GPU_RUN_LOCK:
        state = _GPU_RUN_STATE.get(dep_id)
    if not state:
        return jsonify({'run': None})
    return jsonify({'run': {
        'dep_id': state.get('dep_id'),
        'started_at': state.get('started_at'),
        'tasks': {str(k): v for k, v in (state.get('tasks') or {}).items()},
    }})


@app.route('/api/gpu-window/<int:dep_id>', methods=['DELETE'])
@require_admin
def close_gpu_window(dep_id):
    try:
        result = agent_scw_deploy.close_window(dep_id)
        if not result.get('ok'):
            return jsonify({'error': result.get('error', 'close_window failed')}), 500
        return jsonify({
            'ok': True,
            'final_cost_usd': result.get('final_cost_usd', 0.0),
            'cost_by_project': result.get('cost_by_project', {}),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── AIngel endpoints ──────────────────────────────────────────────────────────

def _init_aingel(pid, model):
    """Create the AIngel chat for a project and seed it with an opening brief.
    Idempotent — returns existing chat_id if already initialised."""
    import agent_chats as chats_mod
    proj = db.get_project(pid)
    if not proj:
        raise ValueError(f'project {pid} not found')
    if proj.get('aingel_chat_id'):
        return proj['aingel_chat_id']

    _def_fname = resolve_def_filename(proj.get('path', ''))
    attachments = [
        {'kind': 'definition', 'ref': _def_fname,          'label': _def_fname},
        {'kind': 'definition', 'ref': 'GUIDE.md',          'label': 'GUIDE.md'},
        {'kind': 'memory',     'ref': 'project.memory.md', 'label': 'Project Memory'},
    ]
    # Only attach master-spec if it has real content (not just the boilerplate skeleton)
    spec_path = os.path.join(proj.get('path', ''), 'Artifacts', 'master-spec.json')
    try:
        with open(spec_path) as _f:
            _spec = json.load(_f)
        _boilerplate = {'schema_version', '_project_type', 'updated_at'}
        if set(_spec.keys()) - _boilerplate:
            attachments.append({'kind': 'memory', 'ref': 'master-spec.json', 'label': 'Master Spec'})
    except Exception:
        pass
    chat = db.create_chat(
        project_id=pid,
        name='❆ Guide',
        model=model,
        attachments=attachments,
    )
    full = db.get_chat(chat['id'])
    if full and full.get('project_path'):
        path = chats_mod.create_chat_file(full['project_path'], full)
        db.update_chat(chat['id'], file_path=path)

    # Lock the model and store the chat reference
    conn = db.get_db()
    conn.execute(
        'UPDATE projects SET aingel_model=?, aingel_chat_id=? WHERE id=?',
        (model, chat['id'], pid)
    )
    conn.commit()
    conn.close()

    # Seed opening turn
    opening = (
        f"I am the Guide for **{proj['name']}**. I have reviewed the project definition. "
        f"I will be watching the task queue, advising before each run, and reviewing outputs "
        f"against the project goals. My role is strategic oversight — I am not here to write "
        f"code or do mechanical work. Ready."
    )
    chat_reply(chat['id'], opening, model=model)
    return chat['id']


@app.route('/api/projects/<int:pid>/aingel/init', methods=['POST'])
@require_project_access('owner')
def aingel_init(pid):
    """Assign an AIngel model to a project and create its dedicated chat."""
    proj = db.get_project(pid)
    if not proj:
        return jsonify({'error': 'Project not found'}), 404
    if proj.get('aingel_model') and proj.get('aingel_chat_id'):
        return jsonify({'ok': True, 'chat_id': proj['aingel_chat_id'],
                        'note': 'already initialised'}), 200
    data = request.json or {}
    model = (data.get('model') or '').strip() or agent_config.default_model_for(proj)
    ok, err = _eu_guard(proj, model)
    if not ok:
        return _eu_reject(proj, model, 'aingel_init', err)
    try:
        chat_id = _init_aingel(pid, model)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, 'chat_id': chat_id})


# ── Phase 7: AIngel Autopilot endpoints ───────────────────────────────────────

@app.route('/api/tasks/<int:tid>/gate', methods=['POST'])
@require_project_access('member')
def resolve_task_gate(tid):
    """Resolve a task's AIngel gate (set by H2 or H3).

    Body: {action: 'acknowledge' | 'override' | 'discuss' | 'answer'}
      - acknowledge: gate_state → 'closed' (user saw the warning, proceeds with awareness)
      - override:    gate_state → 'open'   (user explicitly overrides; runner will proceed)
      - discuss:     no state change; returns the AIngel chat_id for the UI to open
      - answer:      append the user's clarifying answers to the task description and
                     set gate_state → 'open'. Body: {answers: {q_id: answer_text}}
    """
    task = db.get_task(tid)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    data = request.get_json(silent=True) or {}
    action = (data.get('action') or '').strip()
    if action not in ('acknowledge', 'override', 'discuss', 'answer'):
        return jsonify({'error': 'action must be acknowledge|override|discuss|answer'}), 400

    proj = db.get_project(task['project_id']) if task.get('project_id') else None
    chat_id = (proj or {}).get('aingel_chat_id')

    if action == 'discuss':
        return jsonify({'ok': True, 'chat_id': chat_id, 'gate_state': task.get('gate_state')})

    if action == 'answer':
        answers = data.get('answers') or {}
        if not isinstance(answers, dict) or not answers:
            return jsonify({'error': 'answers must be a non-empty object of {q_id: text}'}), 400
        # Build a "User clarifications" block and append it to the description.
        lines = []
        for qid, text in answers.items():
            text = (text or '').strip()
            if not text:
                continue
            lines.append(f"- {qid}: {text}")
        if not lines:
            return jsonify({'error': 'answers must contain at least one non-empty answer'}), 400
        # Append the clarification block to the description. If one was already
        # appended by a prior answer, replace it (idempotency) rather than stack
        # duplicate blocks.
        marker = '**User clarifications (Guide pre-run check):**'
        current = (task.get('description') or '').rstrip()
        idx = current.find('\n\n' + marker)
        if idx != -1:
            current = current[:idx]
        clar_block = f"\n\n{marker}\n" + "\n".join(lines)
        new_desc = current + clar_block
        db.update_task(tid, project_path=task.get('path'),
                       description=new_desc,
                       gate_state='open',
                       gate_reason='',
                       gate_source='',
                       gate_decided_at=datetime.utcnow().isoformat())
        return jsonify({'ok': True, 'gate_state': 'open', 'chat_id': chat_id})

    new_state = 'closed' if action == 'acknowledge' else 'open'
    db.update_task(tid, project_path=task.get('path'),
                   gate_state=new_state,
                   gate_decided_at=datetime.utcnow().isoformat())
    return jsonify({'ok': True, 'gate_state': new_state, 'chat_id': chat_id})


@app.route('/api/projects/<int:pid>/aingel-overview', methods=['GET'])
@require_project_access('viewer')
def get_aingel_overview(pid):
    """Return the rolling AIngel overview markdown (H4 output).

    Returns 404 if the overview hasn't been generated yet. The dashboard can
    trigger a refresh by running any task on the project (H4 fires after H3).
    """
    proj = db.get_project(pid)
    if not proj or not proj.get('path'):
        return jsonify({'error': 'Project not found'}), 404
    overview_path = db.overview_file_path(proj['path'])
    if not os.path.exists(overview_path):
        return jsonify({'error': 'no overview yet', 'markdown': ''}), 404
    try:
        with open(overview_path, 'r', encoding='utf-8', errors='replace') as f:
            md = f.read()
        return jsonify({'markdown': md, 'path': overview_path,
                        'updated_at': datetime.utcfromtimestamp(os.path.getmtime(overview_path)).isoformat()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/aingel-overview/refresh', methods=['POST'])
@require_project_access('member')
def refresh_aingel_overview(pid):
    """Manually trigger an H4 overview refresh for a project."""
    proj = db.get_project(pid)
    if not proj:
        return jsonify({'error': 'Project not found'}), 404
    if not proj.get('aingel_model'):
        return jsonify({'error': 'Project has no aingel_model configured'}), 400
    # Free-tier gate: H4 overview is an autopilot feature.
    _u = g.get('current_user') if auth_enabled() else None
    if _u is not None:
        try:
            import agent_quotas
            if not agent_quotas.can_use_autopilot(_u['id']):
                return jsonify({'error': 'Guide overview is not available on the free tier.'}), 403
        except Exception:
            pass
    # Run synchronously — caller waits for the markdown.
    import threading as _threading
    from agent_executor import _aingel_overview_async
    _aingel_overview_async(proj, proj.get('path'))
    return jsonify({'ok': True, 'note': 'overview refresh attempted'})


@app.route('/api/projects/<int:pid>/budget', methods=['GET'])
@require_project_access('viewer')
def get_project_budget(pid):
    """Return current budget snapshot (resets the period if needed)."""
    snap = db.get_project_budget(pid)
    if not snap:
        return jsonify({'error': 'Project not found'}), 404
    budget = snap.get('budget_monthly') or 0.0
    spend  = snap.get('current_month_spend') or 0.0
    remaining = (budget - spend) if budget else None
    pct = (spend / budget * 100) if budget else None
    return jsonify({
        'project_id':          snap['id'],
        'project_name':        snap.get('name'),
        'monthly_budget':      budget or None,
        'current_month_spend': round(spend, 6),
        'budget_reset_day':    snap.get('budget_reset_day') or 1,
        'budget_month':        snap.get('budget_month'),
        'remaining':           round(remaining, 6) if remaining is not None else None,
        'percent_used':        round(pct, 1) if pct is not None else None,
        'exceeded':            bool(budget and spend >= budget),
    })


@app.route('/api/projects/<int:pid>/budget', methods=['PUT'])
@require_project_access('owner')
def put_project_budget(pid):
    """Set monthly_budget and/or budget_reset_day. Pass null to clear budget."""
    data = request.json or {}
    if not any(k in data for k in ('monthly_budget', 'budget_monthly', 'budget_reset_day', 'reset_day')):
        return jsonify({'error': 'monthly_budget or budget_reset_day required'}), 400
    budget = data.get('monthly_budget', data.get('budget_monthly'))
    reset_day = data.get('budget_reset_day') or data.get('reset_day')
    if reset_day is not None:
        try:
            day = int(reset_day)
        except (TypeError, ValueError):
            return jsonify({'error': 'budget_reset_day must be an integer 1-28'}), 400
        if day < 1 or day > 28:
            return jsonify({'error': 'budget_reset_day must be 1-28'}), 400
    db.update_project_budget(
        pid,
        monthly_budget=float(budget) if (budget is not None and budget != '') else (0.0 if budget == '' else None),
        reset_day=reset_day,
    )
    return get_project_budget(pid)


@app.route('/api/tasks/<int:tid>/estimate', methods=['GET'])
@require_project_access('viewer')
def estimate_task(tid):
    """Estimate a task's cost and its impact on the project's monthly budget."""
    task = db.get_task(tid)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    tokens = task.get('estimated_tokens') or 50000
    cost = estimate_cost(task['model'], tokens)
    snap = db.get_project_budget(task['project_id']) or {}
    budget = snap.get('budget_monthly') or 0.0
    spend  = snap.get('current_month_spend') or 0.0
    impact = (cost / budget * 100) if budget else None
    after  = (spend + cost) if budget else None
    return jsonify({
        'task_id':         tid,
        'model':           task['model'],
        'estimated_tokens': tokens,
        'estimated_cost':   cost,
        'budget': {
            'monthly_budget':      budget or None,
            'current_month_spend': round(spend, 6),
            'spend_after':         round(after, 6) if after is not None else None,
            'remaining_after':     round(budget - after, 6) if after is not None else None,
            'impact_percent':      round(impact, 1) if impact is not None else None,
            'would_exceed':        bool(budget and after is not None and after > budget),
            'exceeded_now':        bool(budget and spend >= budget),
        },
    })


# ── Admin: backfill estimated_tokens ──────────────────────────────────────────

@app.route('/api/admin/backfill-estimates', methods=['POST'])
@require_admin
def backfill_estimates():
    """Walk every task whose estimated_tokens is still the default (50000 or 0)
    and replace it with a real computed estimate from estimate_task_prompt_tokens.
    Intended as a one-shot after adding the auto-estimation hook to create_task —
    safe to re-run; only touches rows still at the default."""
    rows = db.get_tasks(include_archived=False)
    rows = [r for r in rows if (r.get('estimated_tokens') or 50000) in (0, 50000)]
    updated, failed = 0, 0
    for row in rows:
        tid = row['id']
        try:
            est = estimate_task_prompt_tokens(tid)
            if 'error' in est:
                failed += 1
                continue
            computed = (est.get('total_input_tokens') or 0) + (est.get('estimated_output_tokens') or 0)
            if computed > 0:
                db.update_task(tid, estimated_tokens=computed)
                updated += 1
            else:
                failed += 1
        except Exception as e:
            app.logger.warning(f"Backfill failed for task {tid}: {e}")
            failed += 1
    return jsonify({'scanned': len(rows), 'updated': updated, 'failed': failed})


# ── Admin: backfill project permissions ───────────────────────────────────────

@app.route('/api/admin/backfill-permissions', methods=['POST'])
@require_admin
def backfill_permissions():
    """Seed any missing permission groups (e.g. D_bash_proj) for every
    non-archived project, then regenerate .claude/settings.json and
    .vibe/config.toml from the updated DB state.
    Safe to re-run: INSERT OR IGNORE never clobbers existing rows."""
    import agent_permissions as ap
    projects = [p for p in db.get_projects() if not p.get('archived')]
    seeded, failed = 0, 0
    for p in projects:
        pid, path = p['id'], p.get('path', '')
        if not path:
            failed += 1
            continue
        try:
            ap.refresh_permissions(pid, path)
            seeded += 1
        except Exception as e:
            app.logger.warning(f"backfill-permissions failed for project {pid}: {e}")
            failed += 1
    return jsonify({'scanned': len(projects), 'seeded': seeded, 'failed': failed})


# ── Pre-run cost estimation ───────────────────────────────────────────────────

@app.route('/api/estimate', methods=['POST'])
@require_auth
def estimate_prompt():
    """Estimate token count and cost for a chat message or task — no AI call made."""
    data    = request.get_json() or {}
    chat_id = data.get('chat_id')
    task_id = data.get('task_id')
    # Phase 3C: body-param route — checked in-handler (a decorator would turn
    # the missing-key 400 below into a 404). Viewer of the referenced object.
    if auth_enabled():
        if chat_id is not None:
            try:
                chat = db.get_chat(int(chat_id))
            except (TypeError, ValueError):
                chat = None
            if not chat:
                return jsonify({'error': f'Chat {chat_id} not found'}), 404
            ref_pid = chat.get('project_id')
        elif task_id is not None:
            try:
                task = db.get_task(int(task_id))
            except (TypeError, ValueError):
                task = None
            if not task:
                return jsonify({'error': f'Task {task_id} not found'}), 404
            ref_pid = task.get('project_id')
        else:
            return jsonify({'error': 'Provide chat_id or task_id'}), 400
        if not has_project_access(g.current_user, ref_pid, 'viewer'):
            return jsonify({'error': 'not_found'}), 404
    if chat_id is not None:
        result = estimate_chat_prompt_tokens(int(chat_id), data.get('message', ''))
    elif task_id is not None:
        result = estimate_task_prompt_tokens(int(task_id))
    else:
        return jsonify({'error': 'Provide chat_id or task_id'}), 400
    if 'error' in result:
        return jsonify(result), 404
    return jsonify(result)


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.route('/api/tasks')
@require_auth
def list_tasks():
    status           = request.args.get('status')
    project_id       = request.args.get('project_id', type=int)
    include_archived = request.args.get('include_archived', 'true').lower() in ('1', 'true', 'yes')
    work_session_slot = request.args.get('work_session_slot', type=int)
    tasks = db.get_tasks(status=status, project_id=project_id,
                         include_archived=include_archived,
                         work_session_slot=work_session_slot)
    # Phase 3B: in auth mode, scope the list to the caller's member projects.
    # A specific non-member project_id → 404 (anti-enumeration); otherwise
    # filter rows by project_id. Global admins (allowed is None) see all.
    if auth_enabled():
        allowed = get_user_project_ids(g.current_user)
        if allowed is not None:
            if project_id is not None and project_id not in allowed:
                return jsonify({'error': 'not_found'}), 404
            tasks = [t for t in tasks if t.get('project_id') in allowed]
    # Attach estimated_cost if not set
    for t in tasks:
        if not t['estimated_cost']:
            t['estimated_cost'] = estimate_cost(t['model'], t['estimated_tokens'] or 50000)
        # Lane B: ensure context_refs is always a list (hydrate if DB returned JSON string)
        if 'context_refs' not in t or t['context_refs'] is None:
            t['context_refs'] = []
        elif isinstance(t['context_refs'], str):
            try:
                import json as _json
                parsed = _json.loads(t['context_refs'])
                t['context_refs'] = parsed if isinstance(parsed, list) else []
            except Exception:
                t['context_refs'] = []
    return jsonify(tasks)


@app.route('/api/tasks', methods=['POST'])
@require_project_access('member')
def create_task():
    data = request.json or {}
    if not data.get('project_id') or not data.get('title'):
        return jsonify({'error': 'project_id and title required'}), 400
    proj = db.get_project(data['project_id'])
    if not proj:
        return jsonify({'error': 'Project not found'}), 404
    # Resolve the default against the project's own policy: on an EU-only project
    # the global DEFAULT_MODEL is Anthropic, so defaulting to it and *then*
    # guarding rejected every task created without an explicit model — blaming a
    # model the user never chose. Only an explicit non-EU choice is refused.
    model = (data.get('model') or '').strip() or agent_config.default_model_for(proj)
    ok, err = _eu_guard(proj, model)
    if not ok:
        return _eu_reject(proj, model, 'create_task', err)
    user_tokens = data.get('estimated_tokens')
    try:
        execution_type = _norm_task_execution_type(data.get('execution_type'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    # Validate context_refs BEFORE creating the task so a malformed payload can't
    # leave an orphaned task row behind (a client retry would then duplicate it).
    if 'context_refs' in data:
        pre_crefs = data.get('context_refs')
        pre_crefs = [] if pre_crefs is None else pre_crefs
        if not isinstance(pre_crefs, list) or any(not isinstance(c, str) for c in pre_crefs):
            return jsonify({'error': 'context_refs must be an array of strings'}), 400
    task_id = db.create_task(
        project_id=data['project_id'],
        title=data['title'],
        description=data.get('description', ''),
        model=model,
        priority=data.get('priority', 5),
        phase_name=data.get('phase_name', 'Notebook'),
        estimated_tokens=int(user_tokens) if user_tokens else 50000,
        role_id=data.get('role_id') or None,
        project_path=proj.get('path'),
        corpus_id=data.get('corpus_id') or None,
        requires_rag=1 if data.get('requires_rag') else 0,
        created_by=(g.current_user or {}).get('id') if auth_enabled() else None,
        execution_type=execution_type,
    )
    # Hugging Face Scout: record an assigned HF model + awaiting-self-host flag
    # when supplied at creation time.
    if data.get('hf_repo_id') or data.get('awaiting_model'):
        db.update_task(task_id, project_path=proj.get('path'),
                       hf_repo_id=data.get('hf_repo_id') or None,
                       awaiting_model=1 if data.get('awaiting_model') else 0)
    # The user's own text before "Guide my prompt" rewrote it, so an added
    # step (e.g. the unrequested email in task #10001148) can be traced.
    if (data.get('original_description') or '').strip():
        db.update_task(task_id, project_path=proj.get('path'),
                       original_description=data['original_description'])
    if data.get('hf_repo_id'):
        _ensure_hf_roster(data['project_id'], data['hf_repo_id'])
    # Lane B: explicit context refs
    if 'context_refs' in data:
        try:
            crefs = data.get('context_refs')
            if crefs is None:
                crefs = []
            if not isinstance(crefs, list) or any(not isinstance(c, str) for c in crefs):
                return jsonify({'error': 'context_refs must be an array of strings'}), 400
            ok = db.set_task_context_refs(task_id, proj.get('path'), crefs)
            if not ok:
                return jsonify({'error': 'failed to save context_refs'}), 500
        except Exception as _e:
            app.logger.warning(f"set_task_context_refs failed for task {task_id}: {_e}")
    # If the user didn't supply an estimate, replace the 50k default with a real
    # computed estimate (input context + expected output). See
    # estimate_task_prompt_tokens for the breakdown source.
    if not user_tokens:
        try:
            est = estimate_task_prompt_tokens(task_id)
            if 'error' not in est:
                computed = (est.get('total_input_tokens') or 0) + (est.get('estimated_output_tokens') or 0)
                if computed > 0:
                    db.update_task(task_id, estimated_tokens=computed)
        except Exception as e:
            app.logger.warning(f"Auto-estimation failed for task {task_id}: {e}")
    return jsonify({'id': task_id, 'ok': True}), 201


def _is_claude_model(model_id):
    return (model_id or '').startswith('claude-')


def _is_mistral_model(model_id):
    m = model_id or ''
    return (m.startswith('mistral-') or m.startswith('open-mistral')
            or m.startswith('codestral') or m.startswith('devstral'))


def _is_mistral_ocr_model(model_id):
    return (model_id or '').startswith('mistral-ocr')


def _is_scaleway_model(model_id):
    from agent_config import MODELS, dynamic_models
    m = (model_id or '')
    if m.startswith('scw-dep-'):
        return True
    merged = dynamic_models()
    return merged.get(m, {}).get('provider') in ('scaleway', 'scw_deploy') or \
           MODELS.get(m, {}).get('provider') == 'scaleway'


def _is_ollama_model(model_id):
    from agent_config import MODELS
    return MODELS.get(model_id or '', {}).get('provider') == 'ollama'


def _is_mistral_pro_eligible(model_id):
    """All Mistral models are eligible for the Pro subscription column (slot 2)
    when running through the Vibe CLI, which uses the Pro (VIBE) key."""
    m = model_id or ''
    if not _is_mistral_model(m):
        return False
    return True


def _model_label(model_id, dep_labels=None):
    """Return a human-friendly label for an execution's model, or None.

    Dedicated Scaleway GPU deployments store `scw-dep-<uuid>` in the model
    field (required for routing — do NOT change the stored value). Resolve
    the label from the in-memory dynamic_models() map so the Execution Log
    shows e.g. "Dedicated Polish_Law_Bielik (L40S)" instead of the raw UUID.

    `dep_labels` is an already-built dynamic_models() dict; pass it to avoid
    re-querying per row. If omitted, dynamic_models() is called once here."""
    mid = model_id or ''
    if not mid.startswith('scw-dep-'):
        return None
    labels = dep_labels if dep_labels is not None else agent_config.dynamic_models()
    return labels.get(mid, {}).get('label')


# The EU boundary primitives live in agent_config so agent_router, agent_executor
# and agent_overseer can reach them too — agent_router.route() is the real choke
# point. These are thin aliases kept for the API-layer call sites, which reject
# early with a 400 rather than letting the request reach the router.
_is_eu_model = agent_config.is_eu_model
_eu_guard = agent_config.eu_guard


def _eu_reject(proj, model_id, caller, err):
    """Audit an API-level EU refusal and return the 400 response.

    Nothing left the host here — the request was stopped before the router — but
    the attempt belongs in the same log as the router's refusals so the audit
    trail is a complete record of every refusal, not just the late ones."""
    agent_config.eu_audit((proj or {}).get('path') if isinstance(proj, dict) else None,
                          model=model_id,
                          provider=MODELS.get(model_id or '', {}).get('provider', ''),
                          allowed=False, caller=f'api:{caller}', reason=err)
    return jsonify({'error': err}), 400


def _validate_slot_for_model(slot, model_id):
    """Return (ok, error_message) for assigning a model to a kanban column.
    Column rules: 1=Claude Pro (claude only), 2=Mistral Pro (mistral, all Pro-covered),
    3=PAYG (any non-Scaleway, non-Ollama), 4=EU Scaleway (scaleway models only),
    5=Ollama Cloud (ollama models only), NULL=Unassigned (any).
    Special: mistral-ocr-* is Mistral EU-operated, API-only (no CLI), so it is
    allowed in 2 (Mistral Pro), 3 (PAYG) or 4 (EU Scaleway, Vault-friendly) and
    on the Vault it is exempt from the slot-4-only lock.

    All columns are available on every instance; per-project eu_only governs
    model residency via eu_guard at assign/run time, not slot availability."""
    if _is_mistral_ocr_model(model_id):
        if slot in (None, 2, 3, 4):
            return True, None
        return False, 'Mistral OCR belongs in Mistral Pro (slot 2), PAYG (slot 3) or EU Scaleway (slot 4)'
    if _is_scaleway_model(model_id) and slot not in (None, 4):
        return False, 'Scaleway models (scw-*) belong in EU Scaleway column (slot 4)'
    if _is_ollama_model(model_id) and slot not in (None, 5):
        return False, 'Ollama models (oll-*) belong in the Ollama Cloud column (slot 5)'
    if slot is None or slot == 3:
        return True, None
    if slot == 1 and not _is_claude_model(model_id):
        return False, 'Claude Pro column only accepts Claude models'
    if slot == 2:
        if not _is_mistral_model(model_id):
            return False, 'Mistral Pro column only accepts Mistral models'
        if not _is_mistral_pro_eligible(model_id):
            return False, 'This Mistral model is not covered by the Pro subscription — use PAYG'
    if slot == 4 and not _is_scaleway_model(model_id):
        return False, 'EU Scaleway column only accepts Scaleway models (scw-*)'
    if slot == 5 and not _is_ollama_model(model_id):
        return False, 'Ollama Cloud column only accepts Ollama models (oll-*)'
    if slot not in (1, 2, 3, 4, 5, None):
        return False, f'invalid column slot: {slot}'
    return True, None


def _append_to_column(tid, slot):
    """Place task at the bottom of the destination column's slot_position."""
    # Get max slot_position across all project DBs for this slot
    max_pos_val = -1
    for _pid, pp in db._all_project_paths():
        try:
            pconn = db.get_project_db(pp)
            row = pconn.execute(
                'SELECT COALESCE(MAX(slot_position), -1) AS m FROM tasks '
                'WHERE work_session_slot=? AND COALESCE(archived,0)=0', (slot,)
            ).fetchone()
            pconn.close()
            if row and (row['m'] or -1) > max_pos_val:
                max_pos_val = row['m'] or -1
        except Exception:
            pass
    db.update_task(tid, work_session_slot=slot, slot_position=int(max_pos_val) + 1)


def _list_working_doc_names(project_path):
    """Return working-doc files as paths relative to the project root.

    Paths, not extracted content — used by the H2a pre-run clarification check
    so the overseer can see what files exist (and where) without triggering the
    slow pdfplumber extraction that hangs on large projects (task #10001117).
    Relative paths (e.g. ``CUPT/II Etap/foo.pdf``) rather than bare basenames
    so the overseer can confirm a nested reference in the task description
    instead of asking a spurious "is this the same file?" clarification
    (regression: task #10001145 — a nested-path reference couldn't be matched
    against a flat basename list).
    """
    if not project_path or not os.path.isdir(project_path):
        return []
    try:
        from agent_tools import _WORKING_DOC_FOLDERS
    except Exception:
        _WORKING_DOC_FOLDERS = ('My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs')
    paths = []
    for folder in list(_WORKING_DOC_FOLDERS) + [os.path.join('Artifacts', 'outputs')]:
        wd = os.path.join(project_path, folder)
        for f, full in agent_files.list_files_under(wd, max_depth=3):
            paths.append(os.path.relpath(full, project_path))
    return paths


def _run_h2a_precheck_async(tid, project_path):
    """Background H2a clarification check for a just-confirmed task.

    Runs the lightweight overseer question-check and writes the result to the
    task's gate columns. Never blocks the confirm response. If the check fails
    or the project has no autopilot, it no-ops (task stays runnable)."""
    try:
        task = db.get_task(tid)
        if not task:
            return
        proj = db.get_project(task['project_id'])
        if not proj or not proj.get('aingel_autopilot'):
            # Autopilot was toggled off between the optimistic hold (set at
            # confirm time) and this thread running. Clear the gate so the task
            # isn't left stuck in hold/H2a forever.
            db.update_task(tid, project_path=project_path,
                           gate_state='open',
                           gate_source='',
                           gate_reason='',
                           gate_decided_at=datetime.utcnow().isoformat())
            return
        # Free-tier gate: autopilot features (H2a precheck) not available.
        try:
            import agent_quotas
            _owner = agent_quotas.resolve_owner_id(task, proj)
            if _owner and not agent_quotas.can_use_autopilot(_owner):
                db.update_task(tid, project_path=project_path,
                               gate_state='open', gate_source='',
                               gate_reason='', gate_decided_at=datetime.utcnow().isoformat())
                return
        except Exception:
            pass
        import agent_overseer as overseer
        file_names = _list_working_doc_names(project_path)
        result = overseer.pre_check_questions(task, proj, file_names)
        questions = result.get('questions') or []
        gate_state = result.get('gate_state') or ('hold' if questions else 'open')
        reason = result.get('reason') or ('Clarifying questions pending' if questions else '')
        # When the check clears the task (no questions), also clear the
        # gate_source/gate_reason so the optimistic "reviewing…" hold set at
        # confirm time doesn't leave a stale banner on the card.
        source = 'H2a' if gate_state == 'hold' else ''
        db.update_task_h2_gate(
            tid,
            h2_json=json.dumps(result, ensure_ascii=False),
            gate_state=gate_state,
            gate_reason=reason,
            gate_decided_at=datetime.utcnow().isoformat(),
            gate_source=source,
            project_path=project_path,
        )
        # Notify the UI so the questions appear on the card.
        proj_id = task.get('project_id')
        if proj_id is not None:
            db._emit_safe(proj_id, {'type': 'task_changed', 'task_id': tid})
    except Exception as e:
        app.logger.warning('H2a precheck failed for task %s: %s', tid, e)


@app.route('/api/tasks/<int:tid>', methods=['GET'])
@require_project_access('viewer')
def get_task_detail(tid):
    """Return a single task with AIngel brief if available."""
    task = db.get_task(tid)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    # aingel_brief is already populated by _task_query via the executions subquery in project.db
    if not task.get('aingel_brief'):
        task['aingel_brief'] = None
    # Lane B: ensure context_refs is list
    if 'context_refs' not in task or task['context_refs'] is None:
        task['context_refs'] = []
    elif isinstance(task['context_refs'], str):
        try:
            import json as _json
            parsed = _json.loads(task['context_refs'])
            task['context_refs'] = parsed if isinstance(parsed, list) else []
        except Exception:
            task['context_refs'] = []
    return jsonify(task)


# Fields a client may edit on a task. The DB layer also filters kwargs, but
# ``project_path`` is a named parameter of ``db.update_task`` (not a kwarg), so
# a client sending ``project_path`` would otherwise redirect the UPDATE to an
# arbitrary project's database. Filter here, before any route logic runs.
_TASK_CLIENT_FIELDS = frozenset({
    'title', 'description', 'status', 'model', 'priority',
    'estimated_tokens', 'phase_name', 'work_session_slot', 'archived',
    'slot_position', 'handoff_context', 'role_id', 'stale',
    'original_description', 'gate_state', 'gate_reason',
    'gate_source', 'gate_decided_at', 'gate_report_json',
    'gate_report_h2_json', 'hf_repo_id', 'awaiting_model',
    'corpus_id', 'requires_rag', 'context_refs', 'execution_type',
})

# Per-task processing types. Empty/None = a normal run.
_TASK_EXECUTION_TYPES = ('research', 'deployment')


def _norm_task_execution_type(value):
    """'' / None → None; 'research' / 'deployment' → itself; else ValueError."""
    v = (value or '').strip().lower()
    if not v or v == 'standard':
        return None
    if v not in _TASK_EXECUTION_TYPES:
        raise ValueError(f"execution_type must be one of: none, {', '.join(_TASK_EXECUTION_TYPES)}")
    return v


@app.route('/api/tasks/<int:tid>', methods=['PATCH'])
@require_project_access('member')
def update_task(tid):
    raw = request.json or {}
    if not isinstance(raw, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400
    data = {k: v for k, v in raw.items() if k in _TASK_CLIENT_FIELDS}
    task = db.get_task(tid)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    if 'execution_type' in data:
        try:
            data['execution_type'] = _norm_task_execution_type(data['execution_type'])
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

    # Lane B: handle context_refs update via normalized helper
    if 'context_refs' in data:
        crefs = data.pop('context_refs')
        if crefs is None:
            crefs = []
        if not isinstance(crefs, list) or any(not isinstance(c, str) for c in crefs):
            return jsonify({'error': 'context_refs must be an array of strings'}), 400
        ok = db.set_task_context_refs(tid, task.get('path'), crefs)
        if not ok:
            return jsonify({'error': 'failed to update context_refs'}), 500
        # Refresh task for downstream logic (gate etc)
        task = db.get_task(tid) or task

    if data.get('hf_repo_id'):
        _ensure_hf_roster(task['project_id'], data['hf_repo_id'])

    # A reset (→ pending) or an edited description invalidates any queued
    # handoff left over from a previous run sequence.
    if (task.get('handoff_context') or '').strip() and \
            (data.get('status') == 'pending' or 'description' in data):
        db.clear_task_handoff(tid)

    # EU-only data-residency: a model change may not introduce a non-EU model.
    # Normalise in place so the resolved model is what the cost estimate, the
    # slot validation below and the stored row all agree on. Clearing the model
    # resolves to the project's default rather than being refused as ''.
    if 'model' in data:
        _tproj = db.get_project(task['project_id'])
        data['model'] = ((data.get('model') or '').strip()
                         or agent_config.default_model_for(_tproj))
        ok, err = _eu_guard(_tproj, data['model'])
        if not ok:
            return _eu_reject(_tproj, data['model'], 'update_task_model', err)

    # Recalculate estimated_cost if model or tokens changed
    if 'model' in data or 'estimated_tokens' in data:
        model  = data.get('model', task['model'])
        tokens = data.get('estimated_tokens', task['estimated_tokens'] or 50000)
        data['estimated_cost'] = estimate_cost(model, tokens)

    # Invariant: a task may not be `confirmed` without a provider slot.
    # The UI assigns a slot to confirm; this guard rejects raw status-only
    # confirmations that would leave the task slot-less (and invisible in
    # the merged Board view).
    if data.get('status') == 'confirmed':
        resolved_slot = data.get('work_session_slot', task.get('work_session_slot'))
        if resolved_slot in (None, '', 'null'):
            return jsonify({'error': 'Cannot confirm a task without assigning it to a provider slot.'}), 400

    # If only the model is changing (no explicit slot change in this PATCH) and
    # the new model is incompatible with the current column, push the task back
    # to Unassigned rather than leave it in an illegal state.
    if 'model' in data and 'work_session_slot' not in data and task.get('work_session_slot'):
        ok, _ = _validate_slot_for_model(task['work_session_slot'], data['model'])
        if not ok:
            data['work_session_slot'] = None

    # Kanban column assignment: validate model↔slot and apply
    # auto-status / auto-append-bottom side effects.
    if 'work_session_slot' in data:
        raw = data.pop('work_session_slot')
        slot = None if raw in (None, '', 'null') else int(raw)
        new_model = data.get('model', task['model'])
        ok, err = _validate_slot_for_model(slot, new_model)
        if not ok:
            return jsonify({'error': err}), 400

        # Block non-Unassigned slots for tasks with no description/prompt
        if slot is not None and not (task.get('description') or '').strip():
            return jsonify({'error': 'Cannot assign a slot to a task with no description. Add a description first.'}), 400

        if data:  # apply remaining fields first
            db.update_task(tid, **data)

        # 'skip' is deliberately NOT protected: explicitly assigning a slot to a
        # skipped task revives it (sets 'confirmed') so it re-enters the run
        # flow. Unassigning leaves a 'skip' task untouched — the `slot is None`
        # branch below only resets 'confirmed' -> 'pending'.
        protected = {'running', 'done', 'failed'}
        current_status = task['status']
        if slot is None:
            db.update_task(tid, work_session_slot=None, slot_position=0)
            if current_status == 'confirmed':
                db.update_task(tid, status='pending')
                # Clear any optimistic H2a hold / stale gate so the reverted task
                # doesn't carry a "reviewing…" banner into the pending state.
                db.update_task(tid, project_path=task.get('path'),
                               gate_state='open',
                               gate_source='',
                               gate_reason='')
        else:
            _append_to_column(tid, slot)
            if current_status not in protected:
                db.update_task(tid, status='confirmed')
                # H2a: when the task is confirmed on an autopilot project, set an
                # optimistic gate hold synchronously so the runner skips it and
                # the Run buttons disable while the lightweight clarification
                # check runs. The async check then either keeps the hold (real
                # questions found) or clears to open (task is clear). Without the
                # optimistic hold there is a 2-15s race where the task could run
                # ungated before H2a completes.
                try:
                    _proj = db.get_project(task['project_id']) if task.get('project_id') else None
                    _pp = (task.get('path') or '')
                    if _proj and _proj.get('aingel_autopilot'):
                        db.update_task(tid, project_path=_pp,
                                       gate_state='hold',
                                       gate_source='H2a',
                                       gate_reason='Guide is reviewing this task…',
                                       gate_decided_at=datetime.utcnow().isoformat())
                        threading.Thread(
                            target=_run_h2a_precheck_async,
                            args=(tid, _pp),
                            daemon=True,
                        ).start()
                except Exception as _h2a_err:
                    app.logger.warning('H2a trigger failed for task %s: %s', tid, _h2a_err)
            # Slot-overflow: if the estimate doesn't fit the window's remaining
            # budget, overflow to PAYG (slot 3 accepts every model).
            promoted = db.auto_promote_task(tid) if slot in (1, 2) else None
            if promoted and promoted.get('moved'):
                return jsonify({'ok': True, 'auto_promoted': promoted})
        return jsonify({'ok': True})

    db.update_task(tid, **data)
    return jsonify({'ok': True})


@app.route('/api/tasks/<int:tid>', methods=['DELETE'])
@require_project_access('member')
def delete_task(tid):
    db.delete_task(tid)
    return jsonify({'ok': True})


@app.route('/api/tasks/<int:tid>/redo', methods=['POST'])
@require_project_access('member')
def redo_task(tid):
    """Reset a done/failed/skip task back to pending.

    Clears cost, gate reports, handoff, slot; flags downstream dependents stale.
    Past executions stay linked (history preserved).
    """
    if not db.get_task(tid):
        return jsonify({'error': 'Task not found'}), 404
    updated = db.redo_task(tid)
    if updated is None:
        return jsonify({'error': 'Task not found'}), 404
    return jsonify(updated)


@app.route('/api/tasks/<int:tid>/copy', methods=['POST'])
@require_project_access('member')
def copy_task(tid):
    """Clone a task into a new pending task.

    Body: { "title"?: string, "copy_deps"?: boolean }
    Returns the newly created task.
    """
    if not db.get_task(tid):
        return jsonify({'error': 'Task not found'}), 404
    data = request.json or {}
    new_task = db.copy_task(tid,
                            new_title=data.get('title'),
                            copy_deps=bool(data.get('copy_deps', False)))
    if new_task is None:
        return jsonify({'error': 'Failed to copy task'}), 500
    return jsonify(new_task), 201


@app.route('/api/tasks/import', methods=['POST'])
@require_admin
def import_tasks():
    # Imports tasks from GUIDE.md for all projects. DB is the source of truth.
    results = import_all_projects()
    return jsonify(results)


# ── Task dependencies ──────────────────────────────────────────────────────────

@app.route('/api/tasks/<int:tid>/dependencies', methods=['GET'])
@require_project_access('viewer')
def get_task_dependencies(tid):
    if not db.get_task(tid):
        return jsonify({'error': 'Task not found'}), 404
    deps = db.get_dependencies(tid)
    dependents = db.get_dependents(tid)
    return jsonify({'depends_on': deps, 'depended_on_by': dependents})


@app.route('/api/tasks/<int:tid>/dependencies', methods=['POST'])
@require_project_access('member')
def add_task_dependency(tid):
    if not db.get_task(tid):
        return jsonify({'error': 'Task not found'}), 404
    data = request.json or {}
    depends_on_id = data.get('depends_on_id')
    if not depends_on_id:
        return jsonify({'error': 'depends_on_id required'}), 400
    depends_on_id = int(depends_on_id)
    if depends_on_id == tid:
        return jsonify({'error': 'A task cannot depend on itself'}), 400
    if not db.get_task(depends_on_id):
        return jsonify({'error': f'Dependency task {depends_on_id} not found'}), 404
    spec_keys = data.get('spec_keys', [])
    db.add_dependency(tid, depends_on_id, spec_keys)
    return jsonify({'ok': True}), 201


@app.route('/api/tasks/<int:tid>/dependencies/<int:did>', methods=['DELETE'])
@require_project_access('member')
def remove_task_dependency(tid, did):
    db.remove_dependency(tid, did)
    return jsonify({'ok': True})


@app.route('/api/projects/<int:pid>/dependencies', methods=['GET'])
@require_project_access('viewer')
def get_project_dependencies(pid):
    return jsonify(db.get_project_dependencies(pid))


# ── Hugging Face Scout — discovery + adoption ────────────────────────────────


def _hf_eu_servable(provider_mapping):
    """EU-servable when the resolved provider is Scaleway or Mistral. Empty
    (needs self-hosting) and oll-* (US) are not EU-servable today."""
    m = provider_mapping or ''
    return (m.startswith('scw-') or m.startswith('mistral-')
            or m.startswith('codestral-') or m.startswith('devstral-'))


def _hf_candidate(repo_row, imports_map=None):
    """Build a Counselor HF candidate from a roster row + its catalogue entry.

    `repo_row` is a row from project_hf_models; `entry` is the matching
    hf_models.json record. Returns a dict the frontend renders alongside the
    three catalogue picks, with servability + limitations surfaced honestly."""
    repo_id = repo_row['repo_id']
    entry = hf_catalog.catalog_entry(repo_id) or {}
    mapping = (repo_row.get('model_id') or entry.get('provider_mapping') or '').strip()
    pipeline_tag = entry.get('pipeline_tag') or ''
    validation = entry.get('validation') or {
        'score': repo_row.get('validation_score') or 0.0,
        'reasons': [], 'servable': False,
    }
    eff = dict(entry)
    eff['provider_mapping'] = mapping
    servable = hf_catalog.candidate_servable(eff)
    imp = (imports_map or {}).get(repo_id) or {}
    return {
        'repo_id': repo_id,
        'label': repo_row.get('label') or entry.get('label') or repo_id,
        'pipeline_tag': pipeline_tag,
        'language': entry.get('language') or [],
        'tags': entry.get('tags') or [],
        'license': entry.get('license') or '',
        'provider_mapping': mapping,
        'validation': validation,
        'task_model': bool(validation.get('task_model')) if 'task_model' in validation
                      else hf_catalog._task_model(eff),
        'servable': servable,
        'import_status': imp.get('status'),
        'import_model_name': imp.get('model_name'),
        'import_error': imp.get('error_message'),
        'import_ready': imp.get('status') == 'ready',
        'limitations': hf_catalog.limitations_for(eff),
        'hf_url': f'https://huggingface.co/{repo_id}',
    }


def _import_map():
    """{repo_id: latest import row} for annotating roster candidates without a
    query per row. Rows come newest-first, so the first wins."""
    out = {}
    try:
        for r in db.get_model_imports():
            out.setdefault(r['repo_id'], r)
    except Exception:
        pass
    return out


def _sync_library_imports(deployable=None):
    """Reconcile custom models present in the Scaleway library but missing from
    ``scw_model_imports`` (imported via the console, or before AIngel tracked
    imports). For every repo referenced by an adopted roster or a queued HF task
    with no import row, match a live custom model and backfill the row so chips,
    the GPU-window picker and status all light up.

    Best-effort and cheap: reuses the ``list_models`` payload the caller already
    fetched. Returns the number of rows backfilled."""
    try:
        repos = {}  # repo_id -> project_id (first project that references it)
        for r in db.get_all_hf_models():
            repos.setdefault(r['repo_id'], r.get('project_id'))
        for grp in _hf_queue_group():
            repos.setdefault(grp['repo_id'], None)
        rows = db.get_model_imports()
        existing = {r['repo_id'] for r in rows}
        pending = {repo: pid for repo, pid in repos.items()
                   if repo and repo not in existing}
        # Any tracked row warrants a (throttled) reconcile: ready rows confirm the
        # model still exists, in-flight/error/failed rows may have advanced on
        # Scaleway (resuming a restart-stalled import or a late-completed one).
        needs_reconcile = any(r.get('scw_model_id') for r in rows)
        if not pending and not needs_reconcile:
            return 0
        if deployable is None:
            # Steady-state guard: a ready row is the normal resting state, so this
            # path would otherwise hit Scaleway on every roster/adopted request.
            # Throttle the self-fetch; callers that already fetched (the gpu-window
            # picker) pass the payload and are never throttled.
            now = time.time()
            with _SYNC_LOCK:
                if now - _SYNC_LAST['ts'] < _SYNC_MIN_INTERVAL:
                    return 0
                _SYNC_LAST['ts'] = now
            deployable = agent_scw_deploy.list_models(region='fr-par')
        customs = [m for m in (deployable or []) if m.get('custom')]
        by_id = {m.get('id'): m for m in customs if m.get('id')}
        # Reconcile rows against the live library:
        #   * any row whose model is present → advance its status (ready/error),
        #     healing in-flight rows stranded by a mid-import restart AND error/
        #     failed rows whose Scaleway import later turned ready.
        #   * ready/in-flight row whose model is gone → error ("re-import it").
        for r in rows:
            status = r.get('status')
            mid = r.get('scw_model_id')
            m = by_id.get(mid)
            if m is not None:
                live = m.get('status') or 'ready'
                if live in ('ready', 'error') and live != status:
                    db.update_model_import(
                        r['id'], status=live,
                        error_message=(m.get('error_message') or None) if live == 'error' else None)
                continue
            if status in ('ready', 'preparing', 'downloading'):
                if agent_scw_deploy.model_exists(mid) is False:
                    gone = ('Model disappeared from the Scaleway library — re-import it.'
                            if status in ('preparing', 'downloading')
                            else 'Model no longer exists in the Scaleway library — re-import it.')
                    db.update_model_import(r['id'], status='error', error_message=gone)
                    app.logger.info('reconcile: import row %s marked error (model gone)', r['id'])
        if not pending or not customs:
            return 0
        added = 0
        for repo_id, project_id in pending.items():
            exact = None
            tok = None
            base = repo_id.rsplit('/', 1)[-1].lower().replace('_', '-')
            for m in customs:
                if not _custom_model_matches(repo_id, m):
                    continue
                tok = tok or m
                if base and base in (m.get('name') or '').lower().replace('_', '-'):
                    exact = exact or m
            match = exact or tok
            if not match:
                continue
            if db.ensure_model_import(
                    project_id, repo_id, match.get('name'), match.get('id'),
                    match.get('status') or 'ready', size_bytes=match.get('size_bytes')):
                added += 1
        if added:
            app.logger.info('sync_library_imports backfilled %d import row(s)', added)
        return added
    except Exception as e:
        app.logger.warning('sync_library_imports failed: %s', e)
        return 0


@app.route('/api/hf/search', methods=['GET'])
@require_auth
def hf_search():
    q = request.args.get('q', '')
    pipeline_tag = request.args.get('pipeline_tag', '')
    language = request.args.get('language', '')
    try:
        limit = int(request.args.get('limit', 20))
    except (TypeError, ValueError):
        limit = 20
    try:
        results = hf_catalog.search_models(q=q, pipeline_tag=pipeline_tag,
                                           language=language, limit=limit)
    except Exception as e:
        return jsonify({'error': f'Hugging Face search failed: {e}'}), 502
    for r in results:
        r['eu'] = _hf_eu_servable(r.get('provider_mapping'))
        r['hf_url'] = f"https://huggingface.co/{r.get('repo_id', '')}"
    return jsonify({'results': results})


@app.route('/api/hf/adopted', methods=['GET'])
@require_auth
def hf_adopted():
    """Cross-project roster of adopted Hugging Face models, for the task form's
    persistent HF picker (no per-project re-adoption required).
    Phase 3C: deduped catalog rows carry no project attribution, so any
    logged-in user may read it — no membership filtering needed."""
    try:
        _sync_library_imports()
        cands = [_hf_candidate(r, _import_map()) for r in db.get_all_hf_models()]
        cands.sort(key=lambda c: (not c['servable'],
                                  -(c['validation'].get('score') or 0.0)))
        return jsonify({'models': cands})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/hf/register', methods=['POST'])
@require_project_access('member')
def hf_register():
    data = request.get_json(silent=True) or {}
    project_id = data.get('project_id')
    repo_id = (data.get('repo_id') or '').strip()
    set_default = bool(data.get('set_default'))
    if not project_id or not repo_id:
        return jsonify({'error': 'project_id and repo_id are required'}), 400
    proj = db.get_project(project_id)
    if not proj:
        return jsonify({'error': 'Project not found'}), 404

    try:
        entry = hf_catalog.catalog_entry(repo_id)
        if entry is None:
            norm = hf_catalog._normalize(hf_catalog.get_model_card(repo_id))
            entry = hf_catalog.register_model(repo_id, norm)
    except Exception as e:
        return jsonify({'error': f'Hugging Face lookup failed: {e}'}), 502

    model_id = (entry.get('provider_mapping') or '').strip()
    validation = entry.get('validation') or {}
    label = data.get('label') or entry.get('label') or repo_id
    provider = MODELS.get(model_id, {}).get('provider', '')

    if model_id and not _is_eu_model(model_id) and agent_config.eu_only_for(proj):
        return _eu_reject(proj, model_id, 'hf_register',
                          f"'{model_id}' is not EU-compliant — cannot adopt it "
                          f"for an EU-only project.")

    db.add_project_hf_model(project_id, repo_id, model_id=model_id, label=label,
                            provider=provider,
                            validation_score=float(validation.get('score') or 0.0))

    if set_default:
        if not model_id:
            return jsonify({'error': 'Cannot set as default: this model needs '
                                     'self-hosting (no serving path yet).'}), 400
        db.set_project_field(project_id, 'default_model', model_id)

    return jsonify({'ok': True, 'repo_id': repo_id, 'model_id': model_id,
                    'is_default': bool(set_default and model_id)})


@app.route('/api/projects/<int:pid>/hf-models', methods=['GET'])
@require_project_access('viewer')
def project_hf_models(pid):
    proj = db.get_project(pid)
    if not proj:
        return jsonify({'error': 'Project not found'}), 404
    # Reconcile/backfill imports so console-imported models adopted in this project
    # show their status here without waiting for another surface to run the sync.
    _sync_library_imports()
    default_model = (proj.get('default_model') or '').strip()
    imports_map = _import_map()
    roster = []
    for r in db.get_project_hf_models(pid):
        cand = _hf_candidate(r, imports_map)
        roster.append({
            'repo_id': r['repo_id'],
            'model_id': r['model_id'],
            'label': r['label'],
            'provider': r['provider'],
            'validation_score': r['validation_score'],
            'pipeline_tag': cand['pipeline_tag'],
            'limitations': cand['limitations'],
            'hf_url': cand['hf_url'],
            'is_default': bool(default_model and r['model_id'] == default_model),
            'import_status': cand.get('import_status'),
            'import_model_name': cand.get('import_model_name'),
            'import_error': cand.get('import_error'),
            'import_ready': cand.get('import_ready', False),
            'import_id': (imports_map.get(r['repo_id']) or {}).get('id'),
        })
    return jsonify({'models': roster, 'default_model': default_model})


@app.route('/api/projects/<int:pid>/hf-models/<path:repo_id>', methods=['DELETE'])
@require_project_access('member')
def project_hf_model_delete(pid, repo_id):
    proj = db.get_project(pid)
    if not proj:
        return jsonify({'error': 'Project not found'}), 404
    target = next((r for r in db.get_project_hf_models(pid)
                   if r['repo_id'] == repo_id), None)
    if target and (proj.get('default_model') or '') == target['model_id']:
        db.set_project_field(pid, 'default_model', '')
    db.remove_project_hf_model(pid, repo_id)
    return jsonify({'ok': True})


# ── Counselor AI — model recommendation ──────────────────────────────────────


import model_profiles as _mp


def _score_model(model_id, model_cfg, task_cfg, *, estimated_tokens,
                 budget_remaining, tight_budget):
    """Score one model for a task. Lower is better.

    Returns three independent signals so the recommender can pick a model per
    goal instead of collapsing everything into one blended rank:

      * ``quality_score`` — capability only (tier + strength match + hard
        capability gates). Ignores cost entirely. Lowest = most likely to
        deliver the result.
      * ``balanced_score`` — quality + cost penalties. Lowest = best
        quality-per-dollar.
      * ``gate_penalty`` — the sum of hard capability-gate penalties (vision,
        tools, context). Zero means the model can actually do the job; a
        non-zero value means it is missing a required capability.

    Objective capabilities come from agent_config + model_caps (live), so the
    JSON knowledge base only carries the subjective tier/strength judgement.
    """
    from model_caps import model_capabilities
    provider = model_cfg.get('provider', '')
    prof = _mp.profile_for(model_id, provider)
    caps = model_capabilities(model_id)
    tier = prof.get('tier', 'mid')
    strengths = prof.get('strengths', [])
    wants = task_cfg.get('wants', [])

    matched = [w for w in wants if w in strengths]
    ctx = caps.get('context_window') or 0

    quality = float(_mp.tier_weight(tier))
    quality -= 0.22 * min(len(matched), 4)        # refine within the tier band
    gate_penalty = 0.0

    reason_bits = [f'{tier} tier']
    if matched:
        reason_bits.append('strong at ' + ', '.join(matched[:3]))

    # ── hard capability gates ────────────────────────────────────────────────
    if task_cfg.get('needs_vision') and not caps.get('vision'):
        quality += 3.0
        gate_penalty += 3.0
        reason_bits.append('no vision (task needs images)')

    # Agentic file tasks need a model that can call tools. Anthropic/OpenAI run
    # agentically via native/bash file access even without the API `tools` flag.
    if task_cfg.get('needs_tools') and not caps.get('tools') \
            and provider not in ('anthropic', 'openai'):
        quality += 2.0
        gate_penalty += 2.0
        reason_bits.append('no tool use')

    # Context-window fit (keep 10% headroom for the response).
    fits = (estimated_tokens <= ctx * 0.9) if ctx else True
    if not fits:
        quality += 4.0
        gate_penalty += 4.0
        reason_bits.append(f'{ctx // 1000}k context too small for this task')
    elif ctx and estimated_tokens > 0:
        # Graduated headroom bonus: a model with a much larger context window than
        # the prompt needs is rewarded on a log scale, capped at -1.0 so it refines
        # within the tier band but never overrides it. A 256k model on a 50k prompt
        # gains ~0.5 over a 128k model; on a 100k prompt the gap widens.
        headroom = ctx / estimated_tokens
        bonus = min(1.0, 0.5 * math.log2(headroom))
        if bonus > 0:
            quality -= bonus
            reason_bits.append(f'{ctx // 1000}k context')

    # ── cost ─────────────────────────────────────────────────────────────────
    cost_in = model_cfg.get('cost_input', 0.0)
    cost_out = model_cfg.get('cost_output', 0.0)
    est_cost = (cost_in * agent_config.ESTIMATE_INPUT_RATIO
                + cost_out * agent_config.ESTIMATE_OUTPUT_RATIO) * estimated_tokens / 1_000_000

    balanced = quality
    if est_cost == 0.0:
        reason_bits.append('free (subscription)')
    if tight_budget and est_cost > 0.05:
        balanced += 10.0
        reason_bits.append('over a tight budget')

    if budget_remaining > 0:
        balanced += min(est_cost / budget_remaining * 3, 3.0)
    elif est_cost > 0:
        balanced += 3.0

    reason = ' · '.join(reason_bits)
    return quality, balanced, gate_penalty, est_cost, matched, tier, reason


@app.route('/api/tasks/<int:tid>/recommend-model', methods=['POST'])
@require_project_access('member')
def recommend_model(tid):
    """Counselor: recommend three models for a task, one per goal.

    Returns three distinct models (deduped), each tagged with a ``category``:

      * ``best_outcome`` — highest capability (lowest ``quality_score``),
        ignoring cost. The model most likely to deliver the result.
      * ``balanced`` — best quality-per-dollar (lowest ``balanced_score``).
      * ``best_price`` — cheapest model that still passes every hard capability
        gate (``gate_penalty == 0``), so it can actually do the job.

    Respects the project's EU-only policy (non-EU models are excluded, not just
    penalised) and its remaining monthly budget."""
    from model_caps import model_capabilities
    task = db.get_task(tid)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    estimated_tokens = task.get('estimated_tokens') or 50000
    # Use the real prompt size (project context + memory + skills + task text)
    # so the context-window factor actually differentiates between models. The
    # stored estimate is a user-entered guess that defaults to 50k and fits every
    # model's window, which would leave the context factor permanently inert.
    try:
        est = estimate_task_prompt_tokens(tid)
        if isinstance(est, dict) and est.get('total_input_tokens'):
            estimated_tokens = max(int(est['total_input_tokens']), 1)
    except Exception:
        pass  # fall back to the stored estimate on any failure

    prompt_text = (task.get('title', '') or '') + ' ' + (task.get('description') or '')
    task_type = _mp.detect_task_type(prompt_text)
    task_cfg = dict(_mp.task_config(task_type))

    # Language factor: if the prompt is non-English or mixes languages, a
    # multilingual model matters. Inject it into the task's wanted strengths so
    # the existing strength-match scoring rewards multilingual models.
    languages = _mp.detect_languages(prompt_text)
    is_multilingual = len(languages) > 1 or (languages and languages[0] != 'en')
    if is_multilingual:
        wants = list(task_cfg.get('wants', []))
        if 'multilingual' not in wants:
            wants.append('multilingual')
        task_cfg['wants'] = wants

    proj = db.get_project(task['project_id'])
    eu_only = agent_config.eu_only_for(proj)

    snap = db.get_project_budget(task['project_id'])
    monthly_budget = float(snap.get('budget_monthly') or 0.0) if snap else 0.0
    spend = float(snap.get('current_month_spend') or 0.0) if snap else 0.0
    budget_remaining = max(monthly_budget - spend, 0.0) if monthly_budget else 999.0
    tight_budget = budget_remaining < 0.10

    scored = []
    for model_id, model_cfg in MODELS.items():
        if eu_only and not _is_eu_model(model_id):
            continue  # EU-only project: non-EU models are not offered at all
        quality, balanced, gate_penalty, est_cost, matched, tier, reason = _score_model(
            model_id, model_cfg, task_cfg,
            estimated_tokens=estimated_tokens,
            budget_remaining=budget_remaining, tight_budget=tight_budget)
        scored.append({
            'model':          model_id,
            'label':          model_cfg.get('label', model_id),
            'task_type':      task_type,
            'reason':         reason,
            'estimated_cost': round(est_cost, 5),
            'quality_score':  round(quality, 3),
            'balanced_score': round(balanced, 3),
            'gate_penalty':   round(gate_penalty, 3),
            'tier':           tier,
            'strengths':      matched,
            'eu':             _is_eu_model(model_id),
            'provider':       model_cfg.get('provider', ''),
            'context_window': model_capabilities(model_id).get('context_window'),
        })

    # Fold in adopted Hugging Face models whose provider mapping resolves to an
    # existing engine. Specialised models that need self-hosting are not offered
    # here — they appear only in the Settings roster until a serving path exists.
    scored_model_ids = {s['model'] for s in scored}
    for hf in db.get_project_hf_models(task['project_id']):
        hf_model_id = (hf.get('model_id') or '').strip()
        if not hf_model_id or hf_model_id not in MODELS:
            continue
        if hf_model_id in scored_model_ids:
            continue  # already ranked as a catalogue model (base alias)
        if eu_only and not _is_eu_model(hf_model_id):
            continue
        quality, balanced, gate_penalty, est_cost, matched, tier, reason = _score_model(
            hf_model_id, MODELS[hf_model_id], task_cfg,
            estimated_tokens=estimated_tokens,
            budget_remaining=budget_remaining, tight_budget=tight_budget)
        scored.append({
            'model':          hf_model_id,
            'label':          hf.get('label') or hf_model_id,
            'task_type':      task_type,
            'reason':         reason,
            'estimated_cost': round(est_cost, 5),
            'quality_score':  round(quality, 3),
            'balanced_score': round(balanced, 3),
            'gate_penalty':   round(gate_penalty, 3),
            'tier':           tier,
            'strengths':      matched,
            'eu':             _is_eu_model(hf_model_id),
            'provider':       MODELS[hf_model_id].get('provider', ''),
            'context_window': model_capabilities(hf_model_id).get('context_window'),
            'source':         'hf',
            'repo_id':        hf.get('repo_id'),
        })

    def _pick(key, candidates):
        """Lowest ``key`` wins; ties broken by lower cost then higher capability."""
        return min(candidates, key=lambda x: (x[key], x['estimated_cost'], x['quality_score']))

    def _justify(category, rec):
        """One-line human justification for why this model was chosen."""
        cost = rec['estimated_cost']
        cost_txt = 'free (subscription)' if cost == 0.0 else f'~${cost:.4f}'
        if category == 'best_outcome':
            return (f"Highest capability for this task ({rec['tier']} tier) — "
                    f"most likely to deliver the result. Est. {cost_txt}.")
        if category == 'best_price':
            return (f"Cheapest model that still meets the task's requirements. "
                    f"Est. {cost_txt}.")
        return (f"Best balance of capability and cost. Est. {cost_txt}.")

    # 1. Best outcome — highest capability, cost ignored.
    best_outcome = _pick('quality_score', scored)

    # 2. Best price — cheapest model that passes every hard capability gate,
    #    excluding the best-outcome pick so the two never collapse into one.
    eligible = [s for s in scored
                if s['gate_penalty'] == 0.0 and s['model'] != best_outcome['model']]
    if not eligible:
        eligible = [s for s in scored if s['model'] != best_outcome['model']]
    best_price = _pick('estimated_cost', eligible)

    # 3. Balanced — best quality-per-dollar, excluding the two already chosen.
    remaining = [s for s in scored
                 if s['model'] not in (best_outcome['model'], best_price['model'])]
    balanced = _pick('balanced_score', remaining) if remaining else best_outcome

    picks = []
    for category, rec in (('best_outcome', best_outcome),
                           ('balanced', balanced),
                           ('best_price', best_price)):
        rec = dict(rec)
        rec['category'] = category
        rec['justification'] = _justify(category, rec)
        picks.append(rec)

    # Adopted Hugging Face models for this project, surfaced alongside the three
    # catalogue picks so the user can assign a specialised model to the task.
    # Servable first, then by validation score.
    hf_candidates = [_hf_candidate(r)
                     for r in db.get_all_hf_models()]
    hf_candidates.sort(key=lambda c: (not c['servable'],
                                      -(c['validation'].get('score') or 0.0)))

    return jsonify({
        'task_type':        task_type,
        'task_type_label':  task_cfg.get('label', task_type.replace('_', ' ')),
        'eu_only':          eu_only,
        'budget_remaining': round(budget_remaining, 4),
        'estimated_prompt_tokens': estimated_tokens,
        'languages':        languages,
        'is_multilingual':  is_multilingual,
        'recommendations':  picks,
        'hf_candidates':    hf_candidates,
        'profiles_reviewed': _mp.META.get('last_reviewed'),
    })


# ── Execute ───────────────────────────────────────────────────────────────────

@app.route('/api/execute', methods=['POST'])
@require_project_access('member')
def execute():
    data = request.json or {}
    task_id = data.get('task_id')
    if not task_id:
        return jsonify({'error': 'task_id is required'}), 400
    result = run_task(task_id)
    # Spend is recorded centrally in route(); here we only surface the current
    # budget snapshot so the UI can show it.
    if isinstance(result, dict) and result.get('status') == 'done':
        tid = result.get('task_id')
        if tid:
            task = db.get_task(tid)
            if task:
                snap = db.get_project_budget(task['project_id'])
                if snap:
                    result['budget'] = {
                        'monthly_budget':      snap.get('budget_monthly') or None,
                        'current_month_spend': round(snap.get('current_month_spend') or 0.0, 6),
                        'exceeded':            bool((snap.get('budget_monthly') or 0)
                                                    and (snap.get('current_month_spend') or 0)
                                                        >= (snap.get('budget_monthly') or 0)),
                    }
        # Auto-chain: a single-task run (/api/execute) doesn't go through the
        # slot batch runner, so finishing it never starts the next task — the
        # user had to press "Run all" again after every task. When this task
        # just finished 'done', kick a slot batch for any dependents that are
        # now ready (confirmed, in the same slot, all deps satisfied). This
        # closes the gap between single-task runs and the slot auto-batch.
        chained = _maybe_autochain_dependents(tid or task_id)
        if chained:
            result['autochained'] = chained
    return jsonify(result)


def _maybe_autochain_dependents(task_id):
    """After `task_id` finishes done, kick a slot batch for its ready
    dependents. Returns the list of task ids that were queued, or [] if none.

    A dependent is "ready" when: it's `confirmed`, it lives in a work-session
    slot, all its dependencies are `done`, and that slot isn't already
    running. We kick one batch per affected slot (collecting all ready
    dependents in that slot, not just the direct ones, so a whole freed
    chain runs in one go). The slot's 5h window is not reset — the batch
    runs within the existing window or the scheduler picks it up at the
    next window reset.
    """
    if not task_id:
        return []
    try:
        dependents = db.get_dependents(task_id)
    except Exception as e:
        app.logger.warning('autochain: get_dependents(%s) failed: %s', task_id, e)
        return []
    if not dependents:
        return []

    # Group ready dependents by slot.
    by_slot = {}
    for dep in dependents:
        if dep.get('status') != 'confirmed':
            continue
        slot = dep.get('work_session_slot')
        if slot is None:
            continue
        # All of this dependent's deps must be done.
        deps = db.get_dependencies(dep['id'])
        if any(d.get('status') != 'done' for d in deps):
            continue
        by_slot.setdefault(slot, []).append(dep)

    queued = []
    for slot, deps in by_slot.items():
        if _slot_run_thread(slot) is not None:
            continue  # slot batch already in progress; it'll pick these up
        # Collect ALL confirmed, dep-satisfied tasks in this slot (not just the
        # direct dependents) so a freed chain runs together in slot_position
        # order — matches what "Run all (project)" would queue.
        ready = []
        for t in db.get_column_tasks(slot, statuses=('confirmed',)):
            tdeps = db.get_dependencies(t['id'])
            if tdeps and any(d.get('status') != 'done' for d in tdeps):
                continue
            ready.append(t)
        if not ready:
            continue
        ready.sort(key=lambda t: t.get('slot_position') or 0)
        ok, msg = _kick_run_seq(slot, [t['id'] for t in ready])
        if ok:
            queued.extend([t['id'] for t in ready])
            app.logger.info('autochain: slot %s queued %s after #%s done',
                            slot, [t['id'] for t in ready], task_id)
        else:
            app.logger.info('autochain: slot %s not kicked (%s) after #%s done',
                            slot, msg, task_id)
    return queued


# ── Executions ────────────────────────────────────────────────────────────────

def _expand_context_used(e):
    """Replace the raw context_used_json column with context_used /
    context_binary / context_review_needed fields for the UI."""
    raw = e.pop('context_used_json', None)
    used, binary, review = [], [], False
    if raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                if isinstance(obj.get('used'), list):
                    used = obj['used']
                if isinstance(obj.get('binary'), list):
                    binary = obj['binary']
                review = bool(obj.get('review_needed'))
        except Exception:
            pass
    e['context_used'] = used
    e['context_binary'] = binary
    e['context_review_needed'] = review


@app.route('/api/executions')
@require_auth
def list_executions():
    import time as _t
    _t0 = _t.time()
    limit = request.args.get('limit', 50, type=int)
    executions = db.get_executions(limit)
    # Phase 3B: in auth mode, scope to the caller's member projects. The
    # `WHERE chat_id IS NULL` invariant lives in db.get_executions() and is
    # untouched — this only ADDs project filtering. Admins see all.
    if auth_enabled():
        allowed = get_user_project_ids(g.current_user)
        if allowed is not None:
            executions = [e for e in executions if e.get('project_id') in allowed]
    _t1 = _t.time()
    # timing log for stuck diagnosis (OCR 41 PDFs caused 49s)
    if _t1 - _t0 > 1.0:
        app.logger.warning('list_executions slow: get_executions %.2fs limit=%s n=%s', _t1-_t0, limit, len(executions))
    # aingel_brief is already fetched from project.db in get_executions(); just expose it
    # Build the dedicated-deployment label map once per request (in-memory) so
    # scw-dep-<uuid> model ids render as a friendly name, not the raw UUID.
    dep_labels = agent_config.dynamic_models()
    # Cache projects once — was N+1 (db.get_projects() per execution, 100× aingel.db scan = 1.4s)
    _all_projects = db.get_projects()
    _proj_by_id = {p['id']: p for p in _all_projects}
    # Batch has_output_file checks: scan both flat and slug subdirs (Lane B), not 100× stat
    _outputs_by_proj = {}
    for pid, proj in _proj_by_id.items():
        p = proj.get('path') if proj else None
        if p:
            try:
                odir = os.path.join(p, 'Artifacts', 'outputs')
                if not os.path.isdir(odir):
                    _outputs_by_proj[pid] = set()
                else:
                    found = set()
                    for entry in os.listdir(odir):
                        full = os.path.join(odir, entry)
                        if os.path.isfile(full) and entry.startswith('exec-'):
                            found.add(entry)
                        elif os.path.isdir(full):
                            try:
                                for sf in os.listdir(full):
                                    if sf.startswith('exec-'):
                                        found.add(sf)
                            except Exception:
                                pass
                    # fallback walk for deeper nesting
                    if not found:
                        for _, _, files in os.walk(odir):
                            for f in files:
                                if f.startswith('exec-'):
                                    found.add(f)
                    _outputs_by_proj[pid] = found
            except Exception:
                _outputs_by_proj[pid] = set()
    for e in executions:
        e['aingel_review'] = e.get('aingel_brief') or None
        e['model_label'] = _model_label(e.get('model', ''), dep_labels)
        _expand_context_used(e)
        pid = e.get('project_id')
        if pid:
            e['has_output_file'] = f'exec-{e["id"]}-output.md' in _outputs_by_proj.get(pid, set())
        else:
            e['has_output_file'] = False
    _t2 = _t.time()
    if _t2 - _t1 > 1.0:
        app.logger.warning('list_executions post-process %.2fs for %s execs', _t2-_t1, len(executions))
    return jsonify(executions)


@app.route('/api/executions/<int:exec_id>', methods=['GET'])
@require_project_access('viewer')
def get_execution_detail(exec_id):
    """Return a single execution with AIngel review if available."""
    result = _get_exec_context(exec_id)
    if not result:
        return jsonify({'error': 'Execution not found'}), 404
    result['aingel_review'] = result.get('aingel_brief') or None
    result['model_label'] = _model_label(result.get('model', ''))
    _expand_context_used(result)
    return jsonify(result)


@app.route('/api/executions/<int:eid>/cancel', methods=['POST'])
@require_auth
def cancel_execution(eid):
    """Cancel a running execution: SIGTERM the child process, mark the row
    failed, and reset the task to pending so it can be re-run."""
    # Phase 3B: path var is `eid`, which _resolve_pid does not handle — gate
    # in-handler via the execution registry. Missing/non-member → 404.
    if auth_enabled():
        _exec = db.get_execution(eid)
        _pid = _exec.get('project_id') if _exec else None
        if _pid is None or not has_project_access(g.current_user, _pid, 'member'):
            return jsonify({'error': 'not_found'}), 404
    info = db.get_execution_pid(eid)
    if not info:
        return jsonify({'error': 'Execution not found'}), 404
    if info['status'] != 'running':
        return jsonify({'error': f'Execution is {info["status"]}, not running'}), 409
    # Kill the child process if we have its PID
    if info.get('child_pid'):
        import os as _os, signal as _signal, time as _time
        try:
            _os.kill(info['child_pid'], _signal.SIGTERM)
            _time.sleep(1)
            # Check if still alive; SIGKILL if needed
            try:
                _os.kill(info['child_pid'], 0)
                _os.kill(info['child_pid'], _signal.SIGKILL)
            except ProcessLookupError:
                pass  # already gone
        except ProcessLookupError:
            pass  # already gone
        except Exception as _e:
            print(f'[cancel] could not kill PID {info["child_pid"]}: {_e}', file=sys.stderr)
    # Mark the execution failed and clear the PID
    db.finish_execution(eid, 'failed',
                        error_message='Cancelled by user',
                        project_path=info['project_path'])
    # Reset task to pending so it can be re-run
    if info.get('task_id'):
        db.update_task(info['task_id'], status='pending',
                       project_path=info['project_path'])
    # SSE notification
    proj_id = db._resolve_project_id(exec_id=eid, project_path=info['project_path'])
    if proj_id is not None:
        db._emit_safe(proj_id, {'type': 'execution_changed',
                                'exec_id': eid, 'task_id': info.get('task_id')})
        if info.get('task_id'):
            db._emit_safe(proj_id, {'type': 'task_changed', 'task_id': info['task_id']})
    return jsonify({'ok': True, 'exec_id': eid, 'status': 'cancelled'})


@app.route('/api/executions/<int:exec_id>/progress')
@require_project_access('viewer')
def execution_progress(exec_id):
    """SSE endpoint: stream live token/cost progress for a running execution.
    Emits every 3s while status=running; final event when done/failed.
    Works for both task executions (chat_id IS NULL) and chat executions.
    """
    import time as _time

    # Resolve project_path once before entering the SSE loop
    _exec_reg = db._get_exec_reg(exec_id)
    _exec_project_path = _exec_reg['project_path'] if _exec_reg else None

    def _generate():
        import json as _json
        start = _time.time()
        while True:
            row = None
            if _exec_project_path:
                try:
                    pconn = db.get_project_db(_exec_project_path)
                    row = pconn.execute(
                        'SELECT status, tokens_input, tokens_output, cost_usd, started_at, chat_id '
                        'FROM executions WHERE id=?', (exec_id,)
                    ).fetchone()
                    pconn.close()
                except Exception:
                    pass

            if not row:
                yield f'data: {_json.dumps({"error": "not found"})}\n\n'
                return

            elapsed_s = round(_time.time() - start, 1)
            tok_in    = row['tokens_input']  or 0
            tok_out   = row['tokens_output'] or 0
            api_cost  = row['cost_usd']      or 0.0
            status    = row['status']
            chat_id   = row['chat_id']

            payload = {
                'tokens_in':    tok_in,
                'tokens_out':   tok_out,
                'elapsed_s':    elapsed_s,
                'est_api_cost': round(api_cost, 4),
                'status':       status,
                'chat_id':      chat_id,
            }

            if status not in ('running', 'pending'):
                payload['done']        = True
                payload['final_tokens'] = tok_in + tok_out
                payload['final_cost']   = round(api_cost, 4)
                yield f'data: {_json.dumps(payload)}\n\n'
                return

            yield f'data: {_json.dumps(payload)}\n\n'
            _time.sleep(3)

    return Response(
        _generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/api/chats/<int:cid>/stream')
@require_project_access('viewer')
def chat_stream(cid):
    """SSE endpoint: stream execution progress for the latest execution of a chat.
    The frontend can connect to this after sending a message to get real-time updates.
    """
    import time as _time

    # Resolve project_path once before entering the SSE loop
    _chat_reg = db._get_chat_reg(cid)
    _chat_project_path = _chat_reg['project_path'] if _chat_reg else None

    def _generate():
        import json as _json
        start = _time.time()
        last_exec_id = None
        while True:
            row = None
            if _chat_project_path:
                try:
                    pconn = db.get_project_db(_chat_project_path)
                    row = pconn.execute(
                        'SELECT id, status, tokens_input, tokens_output, cost_usd, started_at '
                        'FROM executions WHERE chat_id=? ORDER BY id DESC LIMIT 1',
                        (cid,)
                    ).fetchone()
                    pconn.close()
                except Exception:
                    pass

            if not row:
                # No execution yet – keep waiting
                yield f'data: {_json.dumps({"status": "waiting", "elapsed_s": round(_time.time() - start, 1)})}\n\n'
                _time.sleep(2)
                continue

            exec_id = row['id']
            if last_exec_id is None:
                last_exec_id = exec_id

            elapsed_s = round(_time.time() - start, 1)
            tok_in    = row['tokens_input']  or 0
            tok_out   = row['tokens_output'] or 0
            api_cost  = row['cost_usd']      or 0.0
            status    = row['status']

            payload = {
                'exec_id':      exec_id,
                'tokens_in':    tok_in,
                'tokens_out':   tok_out,
                'elapsed_s':    elapsed_s,
                'est_api_cost': round(api_cost, 4),
                'status':       status,
            }

            if status not in ('running', 'pending'):
                payload['done']        = True
                payload['final_tokens'] = tok_in + tok_out
                payload['final_cost']   = round(api_cost, 4)
                yield f'data: {_json.dumps(payload)}\n\n'
                return

            yield f'data: {_json.dumps(payload)}\n\n'
            _time.sleep(2)

    return Response(
        _generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/api/chats/<int:cid>/reply-status')
@require_project_access('viewer')
def chat_reply_status(cid):
    """Return the status of the latest execution for a chat.
    The frontend can poll this endpoint to check if the reply is ready.
    """
    chat_execs = db.get_chat_executions(cid)
    if not chat_execs:
        return jsonify({'status': 'no_execution', 'exec_id': None})

    row = chat_execs[-1]  # most recent
    return jsonify({
        'exec_id':      row['id'],
        'status':       row['status'],
        'tokens_in':    row['tokens_input'] or 0,
        'tokens_out':   row['tokens_output'] or 0,
        'cost_usd':     row['cost_usd'] or 0.0,
        'started_at':   row['started_at'],
        'finished_at':  row['finished_at'],
        'error':        row['error_message'] or '',
        'done':         row['status'] not in ('running', 'pending'),
    })


@app.route('/api/cost-summary')
@require_auth
def cost_summary():
    # Phase 3C: non-admins get member-project costs only (same scoping as
    # the dashboard); global admins keep the unfiltered aggregates.
    if auth_enabled():
        allowed = get_user_project_ids(g.current_user)
        if allowed is not None:
            projects = [p for p in db.get_projects() if p.get('id') in allowed]
            return jsonify(_scoped_cost_summary(projects))
    summary = db.get_cost_summary()
    by_model = db.get_cost_by_model()
    return jsonify({**summary, 'by_model': by_model})


# ── Memory ────────────────────────────────────────────────────────────────────

@app.route('/api/memory/<int:pid>')
@require_project_access('viewer')
def get_memory(pid):
    project = next((p for p in db.get_projects() if p['id'] == pid), None)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    path = project['path']
    import datetime as _dt
    definitions = []
    for fname in get_def_files_for_project(path):
        fpath = os.path.join(path, fname)
        if os.path.exists(fpath):
            st = os.stat(fpath)
            definitions.append({
                'name':     fname,
                'path':     fpath,
                'size':     st.st_size,
                'modified': _dt.datetime.fromtimestamp(st.st_mtime).isoformat(),
            })
    import agent_permissions as ap
    if not db.get_project_permissions(pid):
        ap._seed_defaults_for_project(pid)
    git_setting = project.get('git_enabled')  # None=auto, 0=off, 1=on
    # Build the unified files catalog once, then derive the deliverables
    # view + by_task grouping from it so the new Files browser and the
    # legacy tabs share the same source of truth.
    files_catalog = fcat.list_project_files(path, pid)
    try:
        folders_catalog = fcat.list_project_folders(path, pid)
    except Exception:
        folders_catalog = []
    by_task = _enrich_task_titles(fcat.group_deliverables_by_task(files_catalog), pid)
    return jsonify({
        'project':      read_memory(path, 'project'),
        'files':        _phase_memory_metadata(pid, path),
        'working_docs': mem.list_working_docs(path),
        'deliverables': [f for f in files_catalog if f['category'] in ('deliverable', 'code', 'output')],
        'by_task':      by_task,
        'files_catalog': {
            'files':     files_catalog,
            'count':     len(files_catalog),
            'by_category': _count_by_category(files_catalog),
            'folders':   folders_catalog,
        },
        'definitions':  definitions,
        'permissions':  ap.get_permissions_view(pid),
        'roles':        db.get_roles(pid),
        'spec':         _get_spec_data(path),
        'git': {
            'setting':  git_setting,
            'enabled':  agit.resolve_enabled(git_setting, path),
            'is_repo':  agit.is_repo(path),
            'is_software': agit.looks_like_software(path),
        },
    })


def _count_by_category(files):
    from collections import Counter
    c = Counter(f['category'] for f in files)
    return dict(c)


def _enrich_task_titles(groups, pid):
    """Fill in task_title for each group from the central task registry."""
    tasks_by_id = {t['id']: t for t in db.get_tasks(project_id=pid)} or {}
    for g in groups:
        if g.get('task_id') is not None and g['task_id'] in tasks_by_id:
            g['task_title'] = tasks_by_id[g['task_id']].get('title', f'Task #{g["task_id"]}')
    return groups


@app.route('/api/projects/<int:pid>/files')
@require_project_access('viewer')
def get_project_files(pid):
    """Full classified file catalog for the Files browser.

    Returns every file in the project, with category, hidden flag, and task
    attribution where known. Used by the Files tab to render the tree,
    category sections, and grouped-by-task deliverables view.
    """
    project = next((p for p in db.get_projects() if p['id'] == pid), None)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    path = project['path']
    files = fcat.list_project_files(path, pid)
    try:
        folders = fcat.list_project_folders(path, pid)
    except Exception:
        folders = []
    groups = _enrich_task_titles(fcat.group_deliverables_by_task(files), pid)
    # file_tags enrichment — top-level map for quick lookup
    try:
        tags_map = db.get_file_tags(path) or {}
    except Exception:
        tags_map = {}
    return jsonify({
        'files': files,
        'count': len(files),
        'by_category': _count_by_category(files),
        'by_task': groups,
        'tags': tags_map,
        'folders': folders,
    })


# ── Unified file manager backend (Phase 1) ─────────────────────────────────

_UPLOAD_CHUNK_SIZE = 20 * 1024 * 1024  # 20 MB
_UPLOAD_MAX_SIZE = 500 * 1024 * 1024   # 500 MB sanity cap
_UPLOAD_ID_RE = re.compile(r'^[a-f0-9-]{8,64}$')

def _files_emit_changed(pid):
    try:
        agent_events.emit(pid, {'type': 'files_changed'})
    except Exception:
        pass


@app.route('/api/projects/<int:pid>/files/upload', methods=['POST'])
@require_project_access('member')
def upload_project_files(pid):
    """Unified upload. Multipart with repeated 'file' fields and optional 'keys' JSON array.
    Writes to Working Documents/<key> and mirrors to SCW bucket best-effort.
    """
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        if not project_path or not os.path.isdir(project_path):
            return jsonify({'error': 'Project path not found'}), 404

        # Collect files
        files = request.files.getlist('file')
        # Fallback: also check 'files' key
        if not files:
            files = request.files.getlist('files')
        if not files or all(not f.filename for f in files):
            return jsonify({'error': 'no files uploaded'}), 400

        # Keys: JSON array in form 'keys' or repeated 'key'
        keys = []
        keys_raw = request.form.get('keys')
        if keys_raw:
            try:
                parsed = json.loads(keys_raw)
                if isinstance(parsed, list):
                    keys = [str(k).strip() for k in parsed]
            except Exception:
                keys = [k.strip() for k in keys_raw.split(',') if k.strip()]
        if not keys:
            # per-file keys: key, key0, etc. or single 'key'
            single = (request.form.get('key') or '').strip()
            if single:
                keys = [single]
            else:
                # try indexed
                for i in range(len(files)):
                    k = (request.form.get(f'key{i}') or '').strip()
                    if k:
                        keys.append(k)
        # If keys still empty or shorter than files, use filename + webkitRelativePath fallback
        # webkitRelativePath is sent as webkitRelativePath or keys contain slashes already
        final_keys = []
        for idx, f in enumerate(files):
            k = keys[idx] if idx < len(keys) and keys[idx] else ''
            if not k:
                # try webkitRelativePath per file if client sent it
                wk = (request.form.get(f'webkitRelativePath_{idx}') or request.form.get('webkitRelativePath') or '').strip()
                if wk:
                    k = wk
                else:
                    k = (f.filename or f'upload_{idx}').strip()
            # Normalize: strip leading slashes, handle webkitRelativePath may include top folder
            k = k.replace('\\', '/').lstrip('/')
            final_keys.append(k)

        # SCW mirror setup
        bucket = proj.get('scw_session_bucket') if proj.get('scw_session_enabled') else None
        region = proj.get('scw_session_region') or 'fr-par'
        has_bucket = bool(bucket)

        # Determine writable root for validation
        out = []
        for f, key in zip(files, final_keys):
            rel, norm_key, is_prefixed = _normalize_upload_key(key)
            # Validate via agent_files
            try:
                if not agent_files._is_writable_rel(rel):
                    return jsonify({'error': f'forbidden or not writable: {key}'}), 400
                # Resolve and write
                abs_path = agent_files._safe_resolve(project_path, rel)
            except ValueError as ve:
                return jsonify({'error': str(ve)}), 400

            parent = os.path.dirname(abs_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            try:
                f.save(abs_path)
            except Exception as e:
                return jsonify({'error': f'failed to save {key}: {e}'}), 500
            size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
            # Free-tier storage quota — stock-based, charged to the PROJECT
            # OWNER (bytes land in and persist against their project).
            _up_user = g.get('current_user') if auth_enabled() else None
            if _up_user is not None:
                try:
                    import agent_quotas
                    _storage_owner = (proj or {}).get('owner_id') or _up_user['id']
                    agent_quotas.check_storage(_storage_owner, size)
                except agent_quotas.QuotaError as e:
                    os.remove(abs_path)
                    return jsonify({'error': str(e), 'quota': e.field, 'limit': e.limit}), 429
            if has_bucket:
                synced, sync_error = _mirror_file_to_scw(abs_path, bucket, region, norm_key, is_prefixed)
            else:
                synced, sync_error = False, None
            entry = {'rel': rel, 'key': key, 'size': size, 'synced': synced}
            if sync_error:
                entry['sync_warning'] = sync_error
            out.append(entry)

        _files_emit_changed(pid)
        return jsonify({'ok': True, 'files': out})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _mirror_file_to_scw(abs_path, bucket, region, norm_key, is_prefixed):
    """Mirror a file to SCW bucket — shared between single and chunked upload.
    Returns (synced: bool, sync_error: str|None)."""
    bucket_key = norm_key
    if is_prefixed:
        for pref in agent_files._WRITABLE_VARIANTS:
            if norm_key == pref:
                bucket_key = ''
                break
            if norm_key.startswith(pref + '/'):
                bucket_key = norm_key[len(pref) + 1:]
                break
    else:
        bucket_key = norm_key
    try:
        res = agent_scw_session.upload_file(
            project_path=abs_path, bucket_name=bucket, key=bucket_key, region=region
        )
        if res.get('ok'):
            return True, None
        return False, res.get('error', 'upload failed')
    except Exception as e:
        return False, str(e)


def _normalize_upload_key(key):
    """Normalize a raw upload key to (rel, norm_key, is_prefixed).
    Handles bare keys like 'folder/a.txt' and prefixed 'Working Documents/...'."""
    norm_key = (key or '').replace('\\', '/').lstrip('/')
    is_prefixed = any(norm_key == f or norm_key.startswith(f + '/') for f in agent_files._WRITABLE_VARIANTS)
    rel = norm_key if is_prefixed else f"Working Documents/{norm_key}"
    rel = rel.replace('//', '/')
    return rel, norm_key, is_prefixed


@app.route('/api/projects/<int:pid>/files/upload-chunk', methods=['POST'])
@require_project_access('member')
def upload_chunk(pid):
    """Chunked upload — receive one slice (20 MB) of a large file."""
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        if not project_path or not os.path.isdir(project_path):
            return jsonify({'error': 'Project path not found'}), 404

        # Extract form fields
        upload_id = (request.form.get('upload_id') or '').strip().lower()
        chunk_index_raw = request.form.get('chunk_index', '')
        total_chunks_raw = request.form.get('total_chunks', '')
        key = (request.form.get('key') or '').strip()

        if not upload_id or not _UPLOAD_ID_RE.match(upload_id):
            return jsonify({'error': 'invalid upload_id'}), 400
        try:
            chunk_index = int(chunk_index_raw)
            total_chunks = int(total_chunks_raw)
        except Exception:
            return jsonify({'error': 'chunk_index and total_chunks must be integers'}), 400
        if total_chunks <= 0 or total_chunks > 1024:
            return jsonify({'error': 'total_chunks out of range (1..1024)'}), 400
        if chunk_index < 0 or chunk_index >= total_chunks:
            return jsonify({'error': 'chunk_index out of range'}), 400
        if total_chunks * _UPLOAD_CHUNK_SIZE > _UPLOAD_MAX_SIZE + _UPLOAD_CHUNK_SIZE:
            # allow slight overage due to last chunk smaller; check > 500 MB + one chunk
            return jsonify({'error': 'total size exceeds cap (500 MB)'}), 400
        if not key:
            return jsonify({'error': 'key required'}), 400
        # Validate key maps to writable rel
        rel, norm_key, is_prefixed = _normalize_upload_key(key)
        if not agent_files._is_writable_rel(rel):
            return jsonify({'error': f'forbidden or not writable: {key}'}), 400
        # Validate _safe_resolve doesn't raise (path traversal etc.)
        try:
            agent_files._safe_resolve(project_path, rel)
        except ValueError as ve:
            return jsonify({'error': str(ve)}), 400

        f = request.files.get('file')
        if not f:
            return jsonify({'error': 'missing file blob'}), 400

        # Ensure per-project .uploads/<upload_id> dir exists
        uploads_base = os.path.join(project_path, '.uploads', upload_id)
        try:
            os.makedirs(uploads_base, exist_ok=True)
        except Exception as e:
            return jsonify({'error': f'failed to create uploads dir: {e}'}), 500

        # Early rejection before writing: use Content-Length if available to avoid filling disk
        if request.content_length and request.content_length > _UPLOAD_CHUNK_SIZE + 4 * 1024 * 1024:
            return jsonify({'error': f'chunk too large: Content-Length {request.content_length} > 20 MB'}), 400
        try:
            cl = getattr(f, 'content_length', None)
            if cl and cl > _UPLOAD_CHUNK_SIZE + 1024 * 1024:
                return jsonify({'error': f'chunk too large: {cl} > 20 MB'}), 400
        except Exception:
            pass
        # Write chunk to zero-padded file
        chunk_name = f"{chunk_index:06d}"
        chunk_path = os.path.join(uploads_base, chunk_name)
        # Use _safe_resolve containment for chunk_path implicitly via project_path containment
        # Ensure chunk_path stays inside uploads_base (prevent traversal via crafted upload_id already validated)
        try:
            f.save(chunk_path)
        except Exception as e:
            return jsonify({'error': f'failed to save chunk {chunk_index}: {e}'}), 500
        size = os.path.getsize(chunk_path) if os.path.exists(chunk_path) else 0
        if size > _UPLOAD_CHUNK_SIZE + 1024 * 1024:
            # Too large single chunk — client exceeded expectation
            try:
                os.remove(chunk_path)
            except Exception:
                pass
            return jsonify({'error': f'chunk too large: {size} > 20 MB'}), 400
        return jsonify({'ok': True, 'chunk': chunk_index, 'received': size})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/files/upload-complete', methods=['POST'])
@require_project_access('member')
def upload_complete(pid):
    """Complete a chunked upload — assemble chunks and finalize the file."""
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        if not project_path or not os.path.isdir(project_path):
            return jsonify({'error': 'Project path not found'}), 404

        data = request.get_json(silent=True) or {}
        upload_id = (data.get('upload_id') or '').strip().lower()
        key = (data.get('key') or '').strip()
        total_chunks = data.get('total_chunks')
        total_size = data.get('total_size')

        if not upload_id or not _UPLOAD_ID_RE.match(upload_id):
            return jsonify({'error': 'invalid upload_id'}), 400
        if not key:
            return jsonify({'error': 'key required'}), 400
        try:
            total_chunks = int(total_chunks)
        except Exception:
            return jsonify({'error': 'total_chunks must be integer'}), 400
        if total_chunks <= 0 or total_chunks > 1024:
            return jsonify({'error': 'total_chunks out of range'}), 400
        if total_chunks * _UPLOAD_CHUNK_SIZE > _UPLOAD_MAX_SIZE + _UPLOAD_CHUNK_SIZE:
            return jsonify({'error': 'total size exceeds cap (500 MB)'}), 400

        rel, norm_key, is_prefixed = _normalize_upload_key(key)
        if not agent_files._is_writable_rel(rel):
            return jsonify({'error': f'forbidden or not writable: {key}'}), 400
        try:
            abs_path = agent_files._safe_resolve(project_path, rel)
        except ValueError as ve:
            return jsonify({'error': str(ve)}), 400

        uploads_base = os.path.join(project_path, '.uploads', upload_id)
        if not os.path.isdir(uploads_base):
            return jsonify({'error': 'upload not found — missing chunks'}), 400

        # Verify all chunk files exist
        missing = []
        for i in range(total_chunks):
            chunk_path = os.path.join(uploads_base, f"{i:06d}")
            if not os.path.isfile(chunk_path):
                missing.append(i)
        if missing:
            return jsonify({'error': f'missing chunks: {missing}'}), 400

        # Assemble by concatenating in order to a temp file
        assembled_tmp = os.path.join(uploads_base, '_assembled.tmp')
        try:
            with open(assembled_tmp, 'wb') as out_f:
                for i in range(total_chunks):
                    chunk_path = os.path.join(uploads_base, f"{i:06d}")
                    with open(chunk_path, 'rb') as cf:
                        shutil.copyfileobj(cf, out_f)
        except Exception as e:
            # Cleanup on error: remove .uploads/<id>
            try:
                shutil.rmtree(uploads_base, ignore_errors=True)
            except Exception:
                pass
            return jsonify({'error': f'failed to assemble: {e}'}), 500

        # Verify assembled size
        try:
            assembled_size = os.path.getsize(assembled_tmp)
        except Exception:
            assembled_size = 0
        if assembled_size > _UPLOAD_MAX_SIZE:
            try:
                os.remove(assembled_tmp)
                shutil.rmtree(uploads_base, ignore_errors=True)
            except Exception:
                pass
            return jsonify({'error': f'assembled file exceeds cap: {assembled_size} > {_UPLOAD_MAX_SIZE}'}), 400
        if total_size is not None:
            try:
                expected = int(total_size)
                if expected != assembled_size:
                    try:
                        os.remove(assembled_tmp)
                        shutil.rmtree(uploads_base, ignore_errors=True)
                    except Exception:
                        pass
                    return jsonify({'error': f'size mismatch for {key}: expected {expected} got {assembled_size} — upload corrupted, please retry'}), 400
            except ValueError:
                # non-integer total_size — ignore, use assembled_size as truth
                pass

        # Ensure parent dirs and move assembled file to final location
        parent = os.path.dirname(abs_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        try:
            # Use shutil.move to handle cross-device if needed; assembled is on same fs normally
            shutil.move(assembled_tmp, abs_path)
        except Exception as e:
            try:
                shutil.rmtree(uploads_base, ignore_errors=True)
            except Exception:
                pass
            return jsonify({'error': f'failed to move assembled file: {e}'}), 500

        size = os.path.getsize(abs_path) if os.path.exists(abs_path) else assembled_size

        # Free-tier storage quota — stock-based, charged to the PROJECT OWNER
        _up_user = g.get('current_user') if auth_enabled() else None
        if _up_user is not None:
            try:
                import agent_quotas
                _storage_owner = (proj or {}).get('owner_id') or _up_user['id']
                agent_quotas.check_storage(_storage_owner, size)
            except agent_quotas.QuotaError as e:
                os.remove(abs_path)
                # The rejected upload's staged chunks are service garbage —
                # without this they linger in .uploads/<id>/ forever (a
                # breach test leaked ~40MB into the project and counted
                # against the owner's stock).
                shutil.rmtree(uploads_base, ignore_errors=True)
                return jsonify({'error': str(e), 'quota': e.field, 'limit': e.limit}), 429

        # Mirror to SCW bucket if has_bucket
        bucket = proj.get('scw_session_bucket') if proj.get('scw_session_enabled') else None
        region = proj.get('scw_session_region') or 'fr-par'
        has_bucket = bool(bucket)
        synced = False
        sync_error = None
        if has_bucket:
            synced, sync_error = _mirror_file_to_scw(abs_path, bucket, region, norm_key, is_prefixed)

        # Cleanup .uploads/<upload_id> dir
        try:
            shutil.rmtree(uploads_base, ignore_errors=True)
        except Exception:
            pass

        _files_emit_changed(pid)
        entry = {'ok': True, 'rel': rel, 'key': key, 'size': size, 'synced': synced}
        if sync_error:
            entry['sync_warning'] = sync_error
        return jsonify(entry)
    except Exception as e:
        # Cleanup on unexpected error
        try:
            uid = (request.get_json(silent=True) or {}).get('upload_id', '')
            if uid and _UPLOAD_ID_RE.match(str(uid).lower()):
                shutil.rmtree(os.path.join(project_path, '.uploads', str(uid).lower()), ignore_errors=True)
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/files/mkdir', methods=['POST'])
@require_project_access('member')
def mkdir_project_file(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        data = request.get_json(silent=True) or {}
        rel = (data.get('path') or data.get('rel') or '').strip()
        if not rel:
            return jsonify({'error': 'path required'}), 400
        try:
            abs_path = agent_files.mkdir(project_path, rel)
        except ValueError as ve:
            return jsonify({'error': str(ve)}), 400
        _files_emit_changed(pid)
        return jsonify({'ok': True, 'path': rel, 'abs': abs_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/files/move', methods=['POST'])
@require_project_access('member')
def move_project_file(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        data = request.get_json(silent=True) or {}
        src = (data.get('src') or '').strip()
        dst = (data.get('dst') or '').strip()
        if not src or not dst:
            return jsonify({'error': 'src and dst required'}), 400
        try:
            agent_files.move(project_path, src, dst)
        except ValueError as ve:
            return jsonify({'error': str(ve)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        _files_emit_changed(pid)
        return jsonify({'ok': True, 'src': src, 'dst': dst})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/files/copy', methods=['POST'])
@require_project_access('member')
def copy_project_file(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        data = request.get_json(silent=True) or {}
        src = (data.get('src') or '').strip()
        dst = (data.get('dst') or '').strip()
        if not src or not dst:
            return jsonify({'error': 'src and dst required'}), 400
        try:
            agent_files.copy(project_path, src, dst)
        except ValueError as ve:
            return jsonify({'error': str(ve)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        _files_emit_changed(pid)
        return jsonify({'ok': True, 'src': src, 'dst': dst})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/files/delete', methods=['POST'])
@require_project_access('member')
def delete_project_files(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        data = request.get_json(silent=True) or {}
        paths = data.get('paths') or data.get('path') or []
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not paths:
            return jsonify({'error': 'paths required'}), 400
        paths = [str(p).strip() for p in paths if str(p).strip()]
        if not paths:
            return jsonify({'error': 'paths required'}), 400
        try:
            trashed = agent_files.delete(project_path, paths)
        except ValueError as ve:
            return jsonify({'error': str(ve)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        _files_emit_changed(pid)
        return jsonify({'ok': True, 'trashed': trashed})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/files/restore', methods=['POST'])
@require_project_access('member')
def restore_project_file(pid):
    """Restore a soft-deleted file from the vault trash back to its original
    Working-Docs location and clear its delete-tombstone (a deliberate undelete).
    Mirrors the 409 message 'restore it from trash if you intend to bring it back'."""
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        data = request.get_json(silent=True) or {}
        rel = data.get('rel') or data.get('path') or ''
        rel = str(rel).strip()
        if not rel:
            return jsonify({'error': 'rel/path required'}), 400
        try:
            res = agent_files.restore(project_path, rel)
        except ValueError as ve:
            return jsonify({'error': str(ve)}), 400
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        _files_emit_changed(pid)
        return jsonify({'ok': True, 'rel': rel})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/files/tags', methods=['GET'])
@require_project_access('viewer')
def get_project_file_tags(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        tags = db.get_file_tags(project_path) or {}
        return jsonify({'tags': tags})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/projects/<int:pid>/files/tags', methods=['POST'])
@require_project_access('member')
def set_project_file_tag(pid):
    try:
        proj = db.get_project(pid)
        if not proj:
            return jsonify({'error': 'Project not found'}), 404
        project_path = proj.get('path') or ''
        data = request.get_json(silent=True) or {}
        rel = (data.get('path') or data.get('rel') or '').strip()
        if not rel:
            return jsonify({'error': 'path required'}), 400
        # Validate rel is writable or at least not forbidden and under project
        # For tagging, we allow any file that exists? But spec says writable variants only.
        # Enforce writable check but allow tagging any existing file under writable root for Phase 1.
        if not agent_files._is_writable_rel(rel):
            # Also allow tagging if file already exists under writable root? For now reject.
            return jsonify({'error': 'path must be under Working Documents'}), 400
        tags = data.get('tags')
        if tags is None:
            tags = []
        if not isinstance(tags, list):
            return jsonify({'error': 'tags must be array'}), 400
        note = data.get('note')
        if note is not None and not isinstance(note, str):
            return jsonify({'error': 'note must be string'}), 400
        # Normalize tags
        tags = [str(t).strip() for t in tags if str(t).strip()]
        ok = db.set_file_tag(project_path, rel, tags, note)
        if not ok and not tags and not (note or '').strip():
            # was delete case — still ok
            pass
        elif not ok:
            return jsonify({'error': 'failed to set tags'}), 500
        _files_emit_changed(pid)
        return jsonify({'ok': True, 'path': rel, 'tags': tags, 'note': note or ''})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/projects/<int:pid>/git', methods=['POST'])
@require_project_access('owner')
def set_project_git(pid):
    """Set git validation mode for a project. body: {mode: 'auto'|'on'|'off'}."""
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    mode = (request.json or {}).get('mode', 'auto')
    value = {'auto': None, 'on': 1, 'off': 0}.get(mode)
    if mode not in ('auto', 'on', 'off'):
        return jsonify({'error': "mode must be auto, on, or off"}), 400
    db.set_project_git_enabled(pid, value)
    path = project['path']
    enabled = agit.resolve_enabled(value, path)
    created = None
    # Turning it on (or auto-resolving to on) initializes the repo eagerly so the
    # first task doesn't pay init cost and the user sees it's tracked immediately.
    if enabled and not agit.is_repo(path):
        created = agit.ensure_repo(path, project.get('name', ''))
    # Keep aingel.json in sync with new git_enabled value
    updated = db.get_project(pid)
    if updated and updated.get('path'):
        _write_aingel_json(updated['path'], updated)
    return jsonify({'ok': True, 'mode': mode, 'enabled': enabled,
                    'is_repo': agit.is_repo(path), 'init': created})


def read_memory(path, level, phase_name=None):
    return mem.read_memory(path, level=level, phase_name=phase_name)


def _execution_output_path(project_path, exec_id):
    return os.path.join(project_path, 'Artifacts', 'outputs', f'exec-{exec_id}-output.md')


def _find_execution_output_file(project_path, exec_id):
    """Find exec output file, supporting legacy flat and Lane B slug layouts."""
    legacy = _execution_output_path(project_path, exec_id)
    if os.path.exists(legacy):
        return legacy
    base = os.path.join(project_path, 'Artifacts', 'outputs')
    if not os.path.isdir(base):
        return None
    target = f'exec-{exec_id}-output.md'
    # quick one-level scan of slug subdirs
    try:
        for entry in os.listdir(base):
            sub = os.path.join(base, entry)
            if os.path.isdir(sub):
                cand = os.path.join(sub, target)
                if os.path.exists(cand):
                    return cand
    except Exception:
        pass
    for root, _, files in os.walk(base):
        if target in files:
            return os.path.join(root, target)
    return None


def _read_execution_content(project_path, exec_id, fallback=''):
    output_file = _find_execution_output_file(project_path, exec_id)
    if output_file and os.path.exists(output_file):
        with open(output_file, encoding='utf-8', errors='ignore') as f:
            return f.read().strip(), output_file
    return chats_mod.normalize_cli_result_text((fallback or '').strip()), None


def _memory_scope_guidance(level):
    if level == 'project':
        return (
            'Project memory is for stable cross-phase facts only: architecture decisions, '
            'durable constraints, APIs, schemas, important lessons, or project-wide decisions. '
            'Omit temporary working notes and transcript-like detail.'
        )
    if level == 'phase':
        return (
            'Phase memory is for phase-relevant implementation decisions, outputs, unresolved '
            'questions, and facts future tasks in the same phase need.'
        )
    if level == 'workflow':
        return (
            'Workflow memory is for session-specific findings, temporary working context, '
            'active hypotheses, and next-step notes.'
        )
    return 'Keep only durable, actionable memory.'


def _strip_markdown_fence(text):
    text = (text or '').strip()
    m = re.match(r'^```(?:markdown|md)?\s*\n(.*?)\n```\s*$', text, re.DOTALL)
    return (m.group(1).strip() if m else text)


def _fallback_memory_delta(source_text):
    """Return a local memory delta when model compaction is unavailable."""
    source_text = (source_text or '').strip()
    if len(source_text) <= 12000:
        return source_text
    return source_text[:12000].rstrip() + '\n\n[Truncated: compaction unavailable.]'


def _compact_for_memory(source_text, *, level, model, project_path, source_label=''):
    """Ask the selected model for a compact, scoped memory delta."""
    source_text = (source_text or '').strip()
    if not source_text:
        return {'error': 'No content selected'}
    requested_model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if requested_model == 'codex-chatgpt' or requested_model.startswith('codex-'):
        return {
            'content': _fallback_memory_delta(source_text),
            'tokens_in': 0,
            'tokens_out': 0,
            'cost_usd': 0.0,
            'model': requested_model,
            'fallback': True,
            'fallback_error': 'Codex CLI is skipped for memory compaction.',
        }
    compaction_model = requested_model
    prompt = f"""You are compacting SuperAgent output into durable memory.

Target scope: {level}
Scope rules: {_memory_scope_guidance(level)}
Source: {source_label or 'selected content'}

Return only a concise Markdown memory delta. Use short bullets. Preserve concrete filenames,
APIs, schemas, decisions, open questions, and follow-up tasks when they matter. Do not include
chat filler, greetings, full transcripts, raw logs, or broad summaries that will not help future work.
If there is nothing worth remembering for this scope, return exactly:
No durable memory delta.

Selected content:

{source_text[:60000]}
"""
    try:
        text, tok_in, tok_out, cost = route(
            compaction_model,
            prompt,
            MEMORY_COMPACT_MAX_TOKENS,
            project_path=project_path,
        )
    except Exception as e:
        return {
            'content': _fallback_memory_delta(source_text),
            'tokens_in': 0,
            'tokens_out': 0,
            'cost_usd': 0.0,
            'model': compaction_model,
            'fallback': True,
            'fallback_error': f'Compaction failed: {e}',
        }
    compacted = _strip_markdown_fence(text)
    if compacted.strip().lower() == 'no durable memory delta.':
        compacted = ''
    return {
        'content': compacted.strip(),
        'tokens_in': tok_in,
        'tokens_out': tok_out,
        'cost_usd': cost,
        'model': compaction_model,
    }


def _extract_chat_selection(transcript, selection):
    """Extract requested chat content."""
    transcript = transcript or ''
    if isinstance(selection, str):
        sel_type = selection
        payload = {}
    elif isinstance(selection, dict):
        sel_type = selection.get('type') or selection.get('kind') or 'last_assistant'
        payload = selection
    else:
        sel_type = 'last_assistant'
        payload = {}

    if sel_type == 'whole_chat':
        return transcript.strip()

    if sel_type in ('range', 'selected_range'):
        provided = (payload.get('text') or '').strip()
        if provided:
            return provided
        if isinstance(payload.get('start'), int) and isinstance(payload.get('end'), int):
            start = max(0, payload['start'])
            end = min(len(transcript), max(start, payload['end']))
            return transcript[start:end].strip()
        if isinstance(payload.get('start_line'), int) and isinstance(payload.get('end_line'), int):
            lines = transcript.splitlines()
            start = max(1, payload['start_line']) - 1
            end = min(len(lines), max(start + 1, payload['end_line']))
            return '\n'.join(lines[start:end]).strip()
        return ''

    bodies = _assistant_messages(transcript)
    return bodies[0].strip() if bodies else ''


def _phase_memory_metadata(project_id, project_path):
    """Return memory metadata enriched with task-derived phase names."""
    phase_names_by_slug = {}
    for t in db.get_tasks(project_id=project_id):
        phase_name = (t.get('phase_name') or '').strip()
        if not phase_name:
            continue
        slug = mem._phase_slug(phase_name)
        phase_names_by_slug.setdefault(slug, [])
        if phase_name not in phase_names_by_slug[slug]:
            phase_names_by_slug[slug].append(phase_name)

    files = []
    for f in mem.list_memory_files(project_path):
        if f.get('file_type') == 'memory' and f.get('level') == 'phase':
            slug = f['name'].replace('.memory.md', '')
            names = phase_names_by_slug.get(slug, [])
            f['phase_slug'] = slug
            f['phase_name'] = names[0] if len(names) == 1 else ''
            f['phase_names'] = names
        files.append(f)
    return files


def _extract_spec_delta(text):
    """Return a JSON dict from a ## Spec Delta fenced block in text, or None."""
    m = re.search(r'##\s+Spec\s+Delta[^\n]*\n\s*```json\s*\n(.*?)\n\s*```',
                  text, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    try:
        return json.loads(m.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _deep_merge_dicts(base, delta):
    result = dict(base)
    for k, v in delta.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge_dicts(result[k], v)
        else:
            result[k] = v
    return result


def _spec_merge_inplace(project_path, delta):
    """Deep-merge delta into Artifacts/master-spec.json.
    Returns list of top-level changed_keys, or None on error."""
    artifacts_dir = os.path.join(project_path, 'Artifacts')
    spec_path = os.path.join(artifacts_dir, 'master-spec.json')
    try:
        os.makedirs(artifacts_dir, exist_ok=True)
        existing = {}
        if os.path.exists(spec_path):
            with open(spec_path, encoding='utf-8') as f:
                existing = json.load(f)
        changed_keys = [k for k in delta if existing.get(k) != delta[k]]
        merged = _deep_merge_dicts(existing, delta)
        merged['schema_version'] = existing.get('schema_version', 0) + 1
        merged['updated_at'] = datetime.utcnow().isoformat()
        with open(spec_path, 'w', encoding='utf-8') as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        return changed_keys
    except Exception as e:
        app.logger.warning(f'[spec_merge] failed: {e}')
        return None


def _flag_stale_by_spec_keys(task_id, changed_keys):
    """Flag dependent tasks stale only where dep spec_keys overlap changed_keys.
    Tasks with empty spec_keys match all keys (generic dependency)."""
    dependents = db.get_dependents(task_id)
    changed = set(changed_keys)
    flagged = 0
    for dep in dependents:
        if dep.get('status') in ('done', 'skip'):
            continue
        try:
            dep_keys = set(json.loads(dep.get('dep_spec_keys') or '[]'))
        except (json.JSONDecodeError, ValueError):
            dep_keys = set()
        if not dep_keys or dep_keys & changed:
            db.update_task(dep['id'], stale=1)
            flagged += 1
    return flagged


def _spec_aware_flag_stale(task_id, project_path, output_text):
    """Merge spec delta from output; flag dependents with spec_keys precision.
    Falls back to generic flag_stale_dependents when no delta found.
    Returns (stale_flagged, changed_keys)."""
    delta = _extract_spec_delta(output_text or '')
    changed_keys = None
    if delta:
        changed_keys = _spec_merge_inplace(project_path, delta)
        if changed_keys is not None:
            return _flag_stale_by_spec_keys(task_id, changed_keys), changed_keys
    return db.flag_stale_dependents(task_id), changed_keys


def _get_spec_data(project_path):
    spec_path = os.path.join(project_path, 'Artifacts', 'master-spec.json')
    if not os.path.exists(spec_path):
        return {'exists': False, 'content': None}
    try:
        with open(spec_path, encoding='utf-8') as f:
            return {'exists': True, 'content': json.load(f)}
    except Exception:
        return {'exists': True, 'content': {}}


def _get_exec_context(exec_id):
    """Replacement for raw SQL JOINs on executions+tasks+projects.
    Returns a flat dict with all fields needed by memory/approval/review endpoints,
    or None if exec_id is not found."""
    exec_data = db.get_execution(exec_id)
    if not exec_data:
        return None
    row = dict(exec_data)
    project_path = row.get('project_path', '')
    # Enrich with task fields when the execution has a task
    task_id = row.get('task_id')
    if task_id:
        task = db.get_task(task_id, project_path)
        if task:
            row['title']            = task.get('title', '')
            row['task_title']       = task.get('title', '')
            row['task_ext_id']      = task.get('external_id', '')
            row['external_id']      = task.get('external_id', '')
            row['task_project_id']  = task.get('project_id')
            row['task_phase_name']  = task.get('phase_name', '')
            row['description']      = task.get('description', '')
            row['model']            = row.get('model') or task.get('model', '')
            row['priority']         = task.get('priority', 5)
            row['phase_name']       = task.get('phase_name', '')
    # Enrich with chat fields when the execution has a chat
    chat_id = row.get('chat_id')
    if chat_id:
        chat = db.get_chat(chat_id, project_path)
        if chat:
            row['chat_name']        = chat.get('name', '')
            row['chat_project_id']  = chat.get('project_id')
            row['chat_phase_name']  = chat.get('phase_name', '')
            row.setdefault('phase_name', chat.get('phase_name', ''))
    return row


@app.route('/api/memory/approve', methods=['POST'])
@require_project_access('member')
def approve_memory():
    """Append a compacted execution memory delta to project, phase, or workflow memory."""
    data      = request.json or {}
    exec_id   = data.get('exec_id')
    level     = data.get('level', 'phase')   # 'project', 'phase', or 'workflow'
    mode      = (data.get('mode') or 'compact').strip()
    model     = (data.get('model') or '').strip()

    if level not in ('project', 'phase', 'workflow'):
        return jsonify({'error': 'level must be project, phase, or workflow'}), 400
    if mode not in ('compact', 'raw'):
        return jsonify({'error': 'mode must be compact or raw'}), 400

    row = _get_exec_context(exec_id)
    if not row:
        return jsonify({'error': 'Execution not found'}), 404
    content, source_file = _read_execution_content(
        row['project_path'], exec_id, fallback=row.get('output_summary') or ''
    )
    row_for_phase = dict(row)
    row_for_phase['phase_name'] = row.get('task_phase_name') or row.get('chat_phase_name') or ''
    phase_name = mem.phase_from_task(row_for_phase) or ''
    task_title = row.get('task_title') or row.get('chat_name') or f'Execution #{exec_id}'

    # Git-backed task? Its value is the committed code change, independent of any
    # prose memory delta. Computed up front so it survives an empty compaction.
    git_branch = (row.get('git_branch') or '').strip()
    git_commit = (row.get('git_commit') or '').strip()
    git_merge_commit = (row.get('git_merge_commit') or '').strip()
    mem_status = (row.get('memory_status') or '').strip()
    # If the execution was rejected, the git branch was discarded — don't try
    # to merge it. Allow approve to proceed as a text-only approval.
    git_branch_exists = False
    if git_branch and agit.is_repo(row['project_path']):
        try:
            import subprocess as _sp
            _r = _sp.run(['git', 'rev-parse', '--verify', git_branch],
                         capture_output=True, text=True, cwd=row['project_path'])
            git_branch_exists = _r.returncode == 0
        except Exception:
            git_branch_exists = False
    git_repo   = (
        bool(row.get('task_id') and git_branch and git_commit and git_branch_exists)
        and not git_merge_commit
        and agit.is_repo(row['project_path'])
    )

    if not content:
        return jsonify({'error': 'No output to approve'}), 400

    write_content = content
    compaction = None
    if mode == 'compact':
        compaction = _compact_for_memory(
            content,
            level=level,
            model=(model or row.get('model')
                   or agent_config.default_model_for(
                       db.get_project_by_path(row['project_path']))),
            project_path=row['project_path'],
            source_label=f'execution #{exec_id}: {task_title}',
        )
        if compaction.get('error'):
            return jsonify({'error': compaction['error']}), 502
        write_content = compaction.get('content') or ''
        if not write_content:
            # Nothing durable to add to memory. For a git-backed task that's fine —
            # approving still means "accept this change", so merge the branch and
            # mark approved. Only fail outright when there is nothing to do at all.
            if git_repo:
                git_merge = agit.merge_task_branch(row['project_path'], git_branch, row.get('task_id'))
                if not git_merge or not git_merge.get('ok'):
                    return jsonify({
                        'error': (git_merge or {}).get('error') or 'Git merge failed',
                        'git_merge': git_merge,
                    }), 409
                if git_merge and git_merge.get('commit'):
                    db.update_execution_git(exec_id, merge_commit=git_merge['commit'])
                db.update_execution_memory_status(exec_id, f'approved:{level}')
                stale_flagged_git = 0
                if row.get('task_id'):
                    stale_flagged_git, spec_delta_changed_keys = _spec_aware_flag_stale(
                        row['task_id'], row['project_path'], content
                    )
                    mem.update_guide_task(row['project_path'], task_title, phase_name)
                resp = {
                    'ok': True, 'memory_file': None, 'level': level, 'mode': mode,
                    'compaction': compaction, 'git_merge': git_merge,
                    'stale_flagged': stale_flagged_git,
                    'spec_delta_changed_keys': spec_delta_changed_keys,
                    'note': 'No durable memory delta — code change merged to main.',
                }
                if git_merge and git_merge.get('warning'):
                    resp['warning'] = git_merge['warning']
                return jsonify(resp)
            return jsonify({'error': 'Compaction found no durable memory delta'}), 422

    # Git validation: approving a git-backed execution merges its task branch
    # into the project's default branch (the code change is now accepted). Do
    # this before writing memory/status so a failed merge leaves approval state
    # untouched.
    git_merge = None
    if git_repo:
        git_merge = agit.merge_task_branch(row['project_path'], git_branch, row.get('task_id'))
        if not git_merge or not git_merge.get('ok'):
            return jsonify({
                'error': (git_merge or {}).get('error') or 'Git merge failed',
                'git_merge': git_merge,
            }), 409
        if git_merge and git_merge.get('commit'):
            db.update_execution_git(exec_id, merge_commit=git_merge['commit'])

    # Workflow memory key: legacy task executions carry session_id, chat
    # executions carry chat_id (same keying as promote_chat_memory).
    workflow_id = (row.get('session_id') or row.get('chat_id')) if level == 'workflow' else None
    path = mem.append_to_memory(
        row['project_path'], write_content, level=level,
        phase_name=phase_name if level == 'phase' else None,
        workflow_id=workflow_id,
        task_title=task_title, exec_id=exec_id,
    )
    db.update_execution_memory_status(exec_id, f'approved:{level}')

    # Propagate staleness: merge any spec delta from the output, then flag
    # dependent tasks stale (filtered by spec_keys when delta found).
    stale_flagged = 0
    if row.get('task_id'):
        stale_flagged, spec_delta_changed_keys = _spec_aware_flag_stale(
            row['task_id'], row['project_path'], content
        )

    # Auto-update GUIDE.md: flip ⬛ → ✅ for the matching task (progress display)
    guide_updated = False
    if row.get('task_id'):
        guide_updated = mem.update_guide_task(
            row['project_path'], task_title, phase_name
        )

    response = {
        'ok': True,
        'memory_file': path,
        'level': level,
        'mode': mode,
        'compaction': compaction,
        'approved_source': source_file or 'output_summary',
        'guide_updated': guide_updated,
        'git_merge': git_merge,
        'stale_flagged': stale_flagged,
        'spec_delta_changed_keys': spec_delta_changed_keys,
    }
    if git_merge and git_merge.get('warning'):
        response['warning'] = git_merge['warning']
    return jsonify(response)


@app.route('/api/memory/revert', methods=['POST'])
@require_project_access('member')
def revert_memory():
    """Undo an execution memory approval and make it approvable again."""
    data = request.json or {}
    exec_id = data.get('exec_id')
    if exec_id is None:
        return jsonify({'error': 'exec_id required'}), 400

    row = _get_exec_context(exec_id)
    if not row:
        return jsonify({'error': 'Execution not found'}), 404

    status = row.get('memory_status') or 'pending'
    if not status.startswith('approved:'):
        return jsonify({'error': f'Execution memory status is not approved: {status}'}), 400

    level = status.split(':', 1)[1].split(':', 1)[0] or 'phase'
    if level not in ('project', 'phase', 'workflow'):
        return jsonify({'error': f'Cannot revert unknown memory level: {level}'}), 400

    row_for_phase = dict(row)
    row_for_phase['phase_name'] = row.get('task_phase_name') or ''
    phase_name = mem.phase_from_task(row_for_phase) or ''
    # Same keying as approval: legacy session_id first, then chat_id.
    workflow_id = (row.get('session_id') or row.get('chat_id')) if level == 'workflow' else None

    # Git-backed approval: undo the merge with an inverse commit first. If a later
    # task built on this change, git can't cleanly invert it — surface that and
    # change nothing, rather than half-reverting.
    git_revert = None
    revert_target = (row.get('git_merge_commit') or row.get('git_commit') or '').strip()
    if revert_target and agit.is_repo(row['project_path']):
        git_revert = agit.revert_merge(row['project_path'], revert_target)
        if not git_revert.get('ok'):
            return jsonify({'error': git_revert.get('error') or 'Git revert failed',
                            'git_revert': git_revert}), 409

    # Memory removal is best-effort: a git-only approval (no durable memory delta)
    # has no memory entry to remove, which is not an error.
    result = mem.remove_memory_entry(
        row['project_path'],
        exec_id,
        level=level,
        phase_name=phase_name if level == 'phase' else None,
        workflow_id=workflow_id,
    )

    db.update_execution_memory_status(exec_id, 'pending')
    return jsonify({
        'ok': True,
        'exec_id': exec_id,
        'level': level,
        'memory_file': result.get('path'),
        'memory_status': 'pending',
        'memory_removed': bool(result.get('removed')),
        'git_revert': git_revert,
    })


@app.route('/api/memory/reject', methods=['POST'])
@require_project_access('member')
def reject_memory():
    """Reject execution output and requeue task with feedback.

    The original task is set back to 'pending' so it re-appeears on the board.
    If feedback is provided, it's prepended to the task description so the next
    run sees it. A new task is NOT created — the original is recycled.
    """
    data     = request.json or {}
    exec_id  = data.get('exec_id')
    feedback = (data.get('feedback') or '').strip()

    row = _get_exec_context(exec_id)
    if not row:
        return jsonify({'error': 'Execution not found'}), 404

    db.update_execution_memory_status(exec_id, 'rejected')

    # Git validation: rejecting a git-backed execution discards its task branch,
    # restoring the project's default branch to its exact pre-task state.
    git_discard = None
    git_branch = (row.get('git_branch') or '').strip()
    if git_branch and agit.is_repo(row['project_path']):
        git_discard = agit.discard_task_branch(row['project_path'], git_branch)

    # Re-queue the original task: set it back to 'pending' so it appears on the
    # board again. If feedback is provided, prepend it to the description.
    task_id = row.get('task_id')
    if task_id:
        if feedback:
            new_desc = row.get('description') or ''
            new_desc = f'[Feedback from previous run]\n{feedback}\n\n[Original instructions]\n{new_desc}'.strip()
            db.update_task(task_id, status='pending', description=new_desc,
                           project_path=row.get('project_path'))
        else:
            db.update_task(task_id, status='pending',
                           project_path=row.get('project_path'))

    return jsonify({'ok': True, 'task_id': task_id, 'message': 'Task re-queued with feedback',
                    'git_discard': git_discard})


@app.route('/api/memory/skip', methods=['POST'])
@require_project_access('member')
def skip_memory():
    """Skip an execution output and mark its task skipped."""
    data    = request.json or {}
    exec_id = data.get('exec_id')
    reason  = (data.get('reason') or '').strip()

    row = _get_exec_context(exec_id)
    if not row:
        return jsonify({'error': 'Execution not found'}), 404

    memory_status = 'skipped'
    if reason:
        memory_status += f': {reason[:120]}'
    db.update_execution_memory_status(exec_id, memory_status)
    db.update_task(row['task_id'], status='skip')

    return jsonify({
        'ok': True,
        'exec_id': exec_id,
        'task_id': row['task_id'],
        'memory_status': memory_status,
        'task_status': 'skip',
    })


@app.route('/api/memory/fork', methods=['POST'])
@require_project_access('member')
def fork_memory():
    """Fork an execution into a requeued task variant with its own task chat."""
    data     = request.json or {}
    exec_id  = data.get('exec_id')
    feedback = (data.get('feedback') or '').strip()
    topic    = (data.get('topic') or '').strip()

    row = _get_exec_context(exec_id)
    if not row:
        return jsonify({'error': 'Execution not found'}), 404

    phase_name = row.get('phase_name') or ''
    fork_topic = topic or f'Fork of exec #{exec_id}: {row["title"]}'

    original_desc = row.get('description') or ''
    fork_note = f'[Forked from execution #{exec_id}]'
    if feedback:
        fork_note += f'\n{feedback}'
    new_desc = f'{fork_note}\n\n[Original instructions]\n{original_desc}'.strip()
    new_task_id = db.create_task(
        project_id=row['project_id'],
        title=row['title'],
        description=new_desc,
        model=row['model'],
        priority=row['priority'],
        phase_name=phase_name,
        project_path=row.get('project_path'),
    )

    # Give the fork a task-scoped chat (new chat system) as its conversation home.
    chat = db.create_chat(row['project_id'], fork_topic, phase_name=phase_name,
                          task_id=new_task_id, model=row['model'],
                          project_path=row.get('project_path'))
    chat_path = ''
    if row.get('project_path'):
        chat_full = db.get_chat(chat['id']) or chat
        chat_path = chats_mod.create_chat_file(row['project_path'], chat_full)
        db.update_chat(chat['id'], file_path=chat_path)
        chats_mod.append_system_note(
            chat_path, f'Forked from execution #{exec_id} (task #{row["task_id"]}).'
        )

    memory_status = f'forked:chat:{chat["id"]}:task:{new_task_id}'
    db.update_execution_memory_status(exec_id, memory_status)

    return jsonify({
        'ok': True,
        'exec_id': exec_id,
        'source_task_id': row['task_id'],
        'new_task_id': new_task_id,
        'new_chat_id': chat['id'],
        'chat_path': chat_path,
        'phase_name': phase_name,
        'memory_status': memory_status,
    })


@app.route('/api/memory/file')
@require_auth
def read_memory_file():
    path = request.args.get('path', '')
    if auth_enabled():
        # Phase 3B: reverse-map the absolute path to its owning project, then
        # viewer-gate it. Unresolvable (outside every project) or non-member
        # → 404 (anti-enumeration). Global admins bypass the membership check.
        owner_pid = _project_id_for_file(path)
        if owner_pid is None or not has_project_access(g.current_user, owner_pid, 'viewer'):
            return jsonify({'error': 'not_found'}), 404
    if not path or not _is_allowed_project_file(path):
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(path):
        return jsonify({'content': ''})
    try:
        with open(path, encoding='utf-8', errors='ignore') as f:
            return jsonify({'content': f.read()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _project_id_for_file(path):
    """Owning project id for an absolute path, or None when unresolvable.

    Mirrors _is_allowed_project_file's folder allow-list: only files under a
    project's Artifacts / Working Docs / ... folders resolve to that project.
    Both sides are realpath'd so symlinks can't escape the prefix match."""
    try:
        requested = os.path.realpath(os.path.abspath(path))
    except Exception:
        return None
    if not requested:
        return None
    allowed_folder_names = ('Artifacts', 'Working Docs', 'Working Documents', 'working-docs', 'docs')
    for project in db.get_projects():
        project_path = project.get('path')
        if not project_path:
            continue
        for folder_name in allowed_folder_names:
            root = os.path.realpath(os.path.join(project_path, folder_name))
            if requested == root or requested.startswith(root + os.sep):
                return project.get('id')
    return None


def _is_allowed_project_file(path):
    return _project_id_for_file(path) is not None


# Extensions that need text-extraction preview (sourced from the shared
# agent_filecat module so browser and server agree on which formats are
# non-renderable). .doc is included for legacy Word docs via antiword.
_NON_RENDERABLE_EXTS = fcat.NON_RENDERABLE_EXTS


def _resolve_project_file(pid, rel_path):
    """Resolve <project>/<rel_path> safely. Returns absolute path or None."""
    project = db.get_project(pid) if hasattr(db, 'get_project') else None
    if project is None:
        for p in db.get_projects():
            if p.get('id') == pid:
                project = p
                break
    if not project:
        return None
    project_root = os.path.realpath(project['path'])
    candidate = os.path.realpath(os.path.join(project_root, rel_path))
    # Must stay inside the project tree. Anything within the project (root-level
    # deliverables, scripts/, logs/, Artifacts/, Working Docs/...) is served —
    # the containment check is the security boundary.
    if candidate != project_root and not candidate.startswith(project_root + os.sep):
        return None
    if candidate == project_root:
        return None  # the project folder itself is never served
    if os.path.isfile(candidate):
        if _is_forbidden_path(os.path.basename(candidate)):
            return None
        return candidate
    # Fallback 1: the frontend Exec Log hardcodes 'Working Documents' for bare
    # diffstat filenames, but the actual folder may be 'Working Docs' (or
    # 'working-docs' / 'docs'). When the direct path misses, re-resolve the
    # basename across all Working-Docs folder variants so the link works
    # regardless of which variant the project uses. This also covers files
    # that were moved between folders after the diffstat was recorded.
    rel = os.path.relpath(candidate, project_root)
    parts = rel.split(os.sep)
    if len(parts) == 2 and parts[0] in ('Working Docs', 'Working Documents', 'working-docs', 'docs'):
        basename = parts[1]
        if not _is_forbidden_path(basename):
            for folder in ('Working Docs', 'Working Documents', 'working-docs', 'docs'):
                alt = os.path.realpath(os.path.join(project_root, folder, basename))
                if alt.startswith(project_root + os.sep) and os.path.isfile(alt) and not _is_forbidden_path(os.path.basename(alt)):
                    return alt
    # Fallback 2: files routinely move after the diffstat that produced the
    # Exec Log link was recorded (e.g. a bare root filename later tidied into
    # Working Docs/). git rename detection also abbreviates paths with a ".../"
    # or "..." prefix, which strips the leading directory. As a last resort,
    # locate the file anywhere in the project by its basename so the link
    # resolves to its current location.
    base = os.path.basename(rel).lstrip('.').lstrip('/')
    if base and not _is_forbidden_path(base) and not _is_forbidden_path(os.path.basename(rel)):
        found = _find_file_by_basename(project_root, base)
        if found and not _is_forbidden_path(os.path.basename(found)):
            return found
    return None


_SKIP_WALK_DIRS = {'.git', '__pycache__', 'node_modules', 'venv', '.venv',
                   '.claude', '.vibe', 'dist', '.trash'}


def _find_file_by_basename(project_root, basename):
    """Locate a file anywhere under project_root whose name ends with the
    given (already dot-stripped) basename. Prefers an exact case-insensitive
    basename match and, among ties, the most recently modified file."""
    target = basename.lower()
    if not target:
        return None
    # Same blocklist as _files_safe_join / _is_forbidden_path — the basename
    # fallback must not serve project.db, .env, *.db, etc.
    if _is_forbidden_path(basename):
        return None
    exact, partial = [], []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_WALK_DIRS]
        for fn in filenames:
            if _is_forbidden_path(fn):
                continue
            fl = fn.lower()
            if fl == target:
                exact.append(os.path.join(dirpath, fn))
            elif fl.endswith(target):
                partial.append(os.path.join(dirpath, fn))
    pool = exact or partial
    if not pool:
        return None
    pool.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return pool[0]


@app.route('/files/<int:pid>/<path:rel_path>')
@require_project_access('viewer')
def serve_project_file(pid, rel_path):
    """Serve a Defs/Artifacts/Working-Docs file raw, with Content-Type set so
    Chrome (and Markdown-viewer extensions) can render it in a new tab."""
    abs_path = _resolve_project_file(pid, rel_path)
    if not abs_path or not os.path.isfile(abs_path):
        abort(404)
    ext = os.path.splitext(abs_path)[1].lower()
    if ext == '.md':
        # text/markdown is what Markdown-viewer extensions match on.
        return _send_project_file_safely(abs_path, mimetype='text/markdown')
    if ext == '.txt' or ext == '.log':
        return _send_project_file_safely(abs_path, mimetype='text/plain; charset=utf-8')
    # Mime guess covers .pdf, images, .json, …; .html/.svg/.xml/.js are forced
    # to download (stored XSS on the app origin otherwise).
    return _send_project_file_safely(abs_path)


# ── Office → PDF preview via headless LibreOffice ───────────────────────────
# Converts non-renderable office files to PDF on the server so the browser's
# native PDF viewer can render them inline (target="_blank" anchors need no
# frontend change). Falls back to text extraction if soffice is absent or
# conversion fails. Data never leaves the Vault.

_PDF_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "aingel-pdf-preview")
_PDF_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "aingel-soffice")
_PDF_CONVERT_LOCK = threading.Lock()
_PDF_CACHE_MAX_FILES = 50
_PDF_CONVERT_TIMEOUT = 60  # seconds per file
_PDF_MAX_SOURCE_BYTES = 50 * 1024 * 1024  # >50 MB → skip conversion, use text fallback


def _pdf_cache_path(abs_path):
    """Deterministic cache filename keyed on abs_path + mtime + size."""
    import hashlib
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    h = hashlib.sha256(abs_path.encode()).hexdigest()[:16]
    safe = f"{h}_{int(st.st_mtime)}_{st.st_size}.pdf"
    return os.path.join(_PDF_CACHE_DIR, safe)


def _prune_pdf_cache():
    """Keep at most _PDF_CACHE_MAX_FILES PDFs on disk (oldest first)."""
    try:
        files = [os.path.join(_PDF_CACHE_DIR, f) for f in os.listdir(_PDF_CACHE_DIR)]
        files = [p for p in files if os.path.isfile(p)]
    except OSError:
        return
    if len(files) <= _PDF_CACHE_MAX_FILES:
        return
    files.sort(key=lambda p: os.path.getmtime(p))
    for p in files[: len(files) - _PDF_CACHE_MAX_FILES]:
        try:
            os.remove(p)
        except OSError:
            pass


def _convert_to_pdf(abs_path):
    """Convert an office file to PDF via headless LibreOffice.

    Returns the absolute path to the cached PDF on success, or None on any
    failure (caller should fall back to text extraction). Thread-safe via a
    global lock because soffice is single-instance per profile.
    """
    import hashlib  # noqa: F811 — also imported in _pdf_cache_path
    import shutil
    import subprocess
    import tempfile
    import glob

    # Size guard — huge spreadsheets can be slow / OOM
    try:
        if os.path.getsize(abs_path) > _PDF_MAX_SOURCE_BYTES:
            print(f"[preview] skip PDF convert (too large): {abs_path}")
            return None
    except OSError:
        return None

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None

    cache_path = _pdf_cache_path(abs_path)
    if not cache_path:
        return None

    # Fast path — already cached
    if os.path.isfile(cache_path):
        return cache_path

    # Ensure dirs exist
    try:
        os.makedirs(_PDF_CACHE_DIR, exist_ok=True)
        os.makedirs(_PDF_PROFILE_DIR, exist_ok=True)
    except OSError:
        pass

    with _PDF_CONVERT_LOCK:
        # Double-check after acquiring lock (another thread may have filled it)
        if os.path.isfile(cache_path):
            return cache_path

        tmpdir = tempfile.mkdtemp(prefix="aingel-lo-")
        try:
            profile_url = "file://" + os.path.abspath(_PDF_PROFILE_DIR)
            cmd = [
                soffice,
                "--headless",
                "--nologo",
                "--norestore",
                "--nolockcheck",
                "--invisible",
                f"-env:UserInstallation={profile_url}",
                "--convert-to", "pdf:writer_pdf_Export",
                "--outdir", tmpdir,
                abs_path,
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_PDF_CONVERT_TIMEOUT,
            )
            # soffice exits 0 on success even if it warns on stdout
            if res.returncode != 0:
                print(f"[preview] soffice failed ({res.returncode}): {res.stderr[:400]}")
                return None

            # LibreOffice names the output <basename>.pdf inside outdir
            pdfs = glob.glob(os.path.join(tmpdir, "*.pdf"))
            if not pdfs:
                # Fallback: expected name
                expected = os.path.join(tmpdir, os.path.splitext(os.path.basename(abs_path))[0] + ".pdf")
                if os.path.isfile(expected):
                    pdfs = [expected]
            if not pdfs:
                print(f"[preview] soffice produced no PDF for {abs_path} (stdout: {res.stdout[:300]})")
                return None

            # Pick the largest PDF if multiple (should be one)
            src_pdf = max(pdfs, key=lambda p: os.path.getsize(p) if os.path.isfile(p) else 0)
            if not os.path.isfile(src_pdf) or os.path.getsize(src_pdf) == 0:
                return None

            # Atomically move into cache
            try:
                shutil.move(src_pdf, cache_path)
            except OSError:
                # Cross-device move fallback
                shutil.copy2(src_pdf, cache_path)
                try:
                    os.remove(src_pdf)
                except OSError:
                    pass
            _prune_pdf_cache()
            return cache_path if os.path.isfile(cache_path) else None
        except subprocess.TimeoutExpired:
            print(f"[preview] soffice timeout for {abs_path}")
            return None
        except Exception as e:
            print(f"[preview] convert error for {abs_path}: {e}")
            return None
        finally:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass


@app.route('/api/preview/<int:pid>/<path:rel_path>')
@require_project_access('viewer')
def preview_project_file(pid, rel_path):
    """Full-fidelity preview for office files.

    Tries headless LibreOffice → PDF first so the browser's native PDF viewer
    renders the document inline (all preview links are target="_blank" anchors
    — no frontend change needed). Falls back to text extraction when soffice
    is unavailable, the file is too large, or conversion fails.

    Data stays on the Vault — no third-party viewer.
    See Plans/Document viewing/Document viewing - plan (Phase 1.5).
    """
    abs_path = _resolve_project_file(pid, rel_path)
    if not abs_path or not os.path.isfile(abs_path):
        return jsonify({'error': 'File not found'}), 404

    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in _NON_RENDERABLE_EXTS:
        # For renderable formats, redirect to the raw file endpoint
        return jsonify({'error': f'{ext} files are renderable — use /files/{pid}/{rel_path}'}), 400

    # Try full-fidelity PDF via LibreOffice
    pdf_path = _convert_to_pdf(abs_path)
    if pdf_path and os.path.isfile(pdf_path):
        # Inline PDF — browser renders natively in new tab
        return send_file(pdf_path, mimetype='application/pdf', conditional=True)

    # Fallback: text-extracted preview
    text = _extract_text_for_preview(abs_path, ext)
    if not text:
        return jsonify({'error': 'Could not extract text from this file'}), 422

    # Wrap in a simple header so the browser shows context
    basename = os.path.basename(abs_path)
    header = f'# {basename}\n# (text-extracted preview — formatting may be lost)\n\n'
    return Response(header + text, mimetype='text/plain; charset=utf-8')


def _extract_text_for_preview(abs_path, ext):
    """Dispatch to the right extractor based on file extension. Best-effort:
    if the format isn't supported, returns None so the caller can 422."""
    from prompt_builder import (
        _extract_docx_text, _extract_xlsx_text, _extract_pptx_text,
    )
    try:
        if ext in ('.docx', '.odt', '.doc'):
            return _extract_docx_text(abs_path)
        if ext in ('.xlsx', '.ods'):
            return _extract_xlsx_text(abs_path)
        if ext in ('.pptx', '.odp'):
            return _extract_pptx_text(abs_path)
    except Exception:
        return None
    return None


@app.route('/api/execution/output/<int:exec_id>')
@require_project_access('viewer')
def get_execution_output(exec_id):
    """Serve the full output file for an execution."""
    exec_data = db.get_execution(exec_id)
    if not exec_data:
        return jsonify({'error': 'Execution not found'}), 404

    project_path = exec_data.get('project_path', '')
    output_file = _find_execution_output_file(project_path, exec_id) or _execution_output_path(project_path, exec_id)
    if not output_file or not os.path.exists(output_file):
        fallback = chats_mod.normalize_cli_result_text(exec_data.get('output_summary') or '')
        if fallback:
            return jsonify({'content': fallback, 'source': 'summary'})
        return jsonify({'error': 'Output file not found'}), 404

    with open(output_file, encoding='utf-8') as f:
        content = f.read()
    return jsonify({'content': content})


@app.route('/api/executions/<int:exec_id>/diff')
@require_project_access('viewer')
def get_execution_diff(exec_id):
    """Return the full git diff for a git-backed execution (code changes the AI made)."""
    exec_data = db.get_execution(exec_id)
    if not exec_data:
        return jsonify({'error': 'Execution not found'}), 404

    git_branch = (exec_data.get('git_branch') or '').strip()
    git_commit  = (exec_data.get('git_commit') or '').strip()
    git_merge_commit = (exec_data.get('git_merge_commit') or '').strip()
    project_path = exec_data.get('project_path', '')

    if not git_branch or not git_commit:
        return jsonify({'error': 'No git branch/commit for this execution'}), 404
    if not agit.is_repo(project_path):
        return jsonify({'error': 'Project is not a git repo'}), 404

    # Preferred path: diff the task branch against the default branch. This
    # fails once the branch has been merged and deleted, so fall back to
    # diffing the recorded commit (merge commit → its first-parent change,
    # else the task commit itself).
    if agit.commit_exists(project_path, git_branch):
        base = agit.default_branch(project_path)
        result = agit.get_diff(project_path, base, git_branch)
        result['base_branch'] = base
        result['task_branch'] = git_branch
    else:
        ref = git_merge_commit or git_commit
        result = agit.get_commit_diff(project_path, ref)
        result['base_branch'] = agit.default_branch(project_path)
        result['task_branch'] = f'{ref} (merged)'
    return jsonify(result)


@app.route('/api/execution/output/<int:exec_id>/raw')
@require_project_access('viewer')
def get_execution_output_raw(exec_id):
    """Serve the raw .md output file inline so the browser's markdown viewer
    (or extension) can render it directly. Falls back to the summary text
    when the file is missing."""
    row = _get_exec_context(exec_id)
    if not row:
        abort(404)

    project_path = row.get('project_path', '')
    output_file = _find_execution_output_file(project_path, exec_id) or _execution_output_path(project_path, exec_id)
    filename = f'exec-{exec_id}-output.md'

    if output_file and os.path.exists(output_file):
        resp = send_file(
            output_file,
            mimetype='text/markdown; charset=utf-8',
            as_attachment=False,
            download_name=filename,
        )
        resp.headers['Content-Disposition'] = f'inline; filename="{filename}"'
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp

    fallback = chats_mod.normalize_cli_result_text(row.get('output_summary') or '')
    if not fallback:
        abort(404)
    body = f"# {row.get('task_title') or 'Execution output'}\n\n_(file missing — showing stored summary)_\n\n{fallback}\n"
    resp = Response(body, mimetype='text/markdown; charset=utf-8')
    resp.headers['Content-Disposition'] = f'inline; filename="{filename}"'
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


# ── Chats ─────────────────────────────────────────────────────────────────────
# Phase 4: chats are first-class. A chat lives at one of three scopes:
#   project  (project_id only)
#   phase    (project_id + phase_name)
#   task     (project_id + task_id, phase_name optional)
# The legacy /api/workflows/* routes have been removed; existing sessions rows
# are left dormant in the DB for audit only.

@app.route('/api/chats', methods=['GET'])
@require_auth
def list_chats():
    project_id = request.args.get('project_id', type=int)
    phase_name = request.args.get('phase_name')
    task_id    = request.args.get('task_id', type=int)
    status     = request.args.get('status', 'active')
    # Phase 3B: in auth mode, a present project_id is membership-gated (404
    # when non-member); an absent one scopes the rows to member projects.
    # Global admins (allowed is None) see all.
    allowed = None
    if auth_enabled():
        allowed = get_user_project_ids(g.current_user)
        if allowed is not None and project_id is not None and project_id not in allowed:
            return jsonify({'error': 'not_found'}), 404
    chats = db.get_chats(
        project_id=project_id,
        phase_name=phase_name,
        task_id=task_id,
        status=status,
    )
    if allowed is not None and project_id is None:
        chats = [c for c in chats if c.get('project_id') in allowed]
    return jsonify(chats)


@app.route('/api/chats', methods=['POST'])
@require_project_access('member')
def create_chat():
    data = request.json or {}
    project_id = data.get('project_id')
    name       = (data.get('name') or '').strip()
    if not project_id or not name:
        return jsonify({'error': 'project_id and name required'}), 400

    phase_name  = (data.get('phase_name') or '').strip()
    task_id     = data.get('task_id')
    chat_proj   = db.get_project(project_id)
    model       = (data.get('model') or '').strip() or agent_config.default_model_for(chat_proj)
    ok, err = _eu_guard(chat_proj, model)
    if not ok:
        return _eu_reject(chat_proj, model, 'create_chat', err)
    attachments = data.get('attachments') or []
    if not isinstance(attachments, list):
        attachments = []

    # If task_id is set and phase_name omitted, inherit phase from the task
    if task_id and not phase_name:
        t = db.get_task(task_id)
        if t:
            phase_name = (t.get('phase_name') or '').strip()

    chat = db.create_chat(
        project_id=project_id,
        name=name,
        phase_name=phase_name,
        task_id=task_id,
        model=model,
        attachments=attachments,
    )

    # Prepare the chat file with project + task metadata for the header
    full = db.get_chat(chat['id'])
    if full and full.get('project_path'):
        path = chats_mod.create_chat_file(full['project_path'], full)
        db.update_chat(chat['id'], file_path=path)
        chat['file_path'] = path

    return jsonify(db.get_chat(chat['id'])), 201


@app.route('/api/chats/<int:cid>', methods=['GET'])
@require_project_access('viewer')
def get_chat_detail(cid):
    chat = db.get_chat(cid)
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
    # Annotate each attachment with its on-disk size for the client.
    # db.get_chat already returns `attachments` as a list — _parse_attachments
    # tolerates both that and the legacy JSON-string shape.
    attachments = _parse_attachments(chat)
    cap = ATTACHMENT_CHAR_CAP
    for att in attachments:
        att['size'] = 0
        att['truncated'] = False
        # Per-attachment: a missing/unreadable target must not blank the whole list.
        try:
            if att['kind'] == 'definition':
                fpath = _safe_resolve(chat.get('project_path', ''), att['ref'])
                if fpath and os.path.exists(fpath):
                    att['size'] = os.path.getsize(fpath)
                    if att['size'] > cap:
                        att['truncated'] = True
            elif att['kind'] == 'memory':
                fpath = _safe_resolve(chat.get('project_path', ''), 'Artifacts', att['ref'])
                if fpath and os.path.exists(fpath):
                    att['size'] = os.path.getsize(fpath)
                    if att['size'] > cap:
                        att['truncated'] = True
            elif att['kind'] == 'working_doc':
                fpath = _safe_resolve(chat.get('project_path', ''), 'Working Docs', att['ref'])
                if fpath and os.path.exists(fpath):
                    att['size'] = os.path.getsize(fpath)
                    if att['size'] > cap:
                        att['truncated'] = True
            elif att['kind'] == 'task':
                t = db.get_task(int(att['ref']))
                if t:
                    desc = (t.get('description') or '').encode('utf-8')
                    att['size'] = len(desc)
                    if att['size'] > cap:
                        att['truncated'] = True
        except Exception:
            pass
    chat['attachments'] = attachments
    chat['transcript'] = chats_mod.parse_messages(chat.get('file_path') or '')
    chat['executions'] = db.get_chat_executions(cid)
    return jsonify(chat)


# Fields a client may edit on a chat. Anything else (notably ``file_path`` and
# ``project_path``) is internal: ``file_path`` decides which file the chat
# reads, appends to and deletes on the filesystem, so it must never be
# client-settable (arbitrary read / append / delete, e.g. ``.env`` or
# ``~/.ssh/authorized_keys``).
_CHAT_EDITABLE_FIELDS = frozenset({
    'name', 'status', 'model', 'phase_name', 'attachments',
    'auto_inject_defs', 'scaffold_draft',
})


@app.route('/api/chats/<int:cid>', methods=['PATCH'])
@require_project_access('member')
def update_chat_route(cid):
    raw = request.json or {}
    if not isinstance(raw, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400
    fields = {k: v for k, v in raw.items() if k in _CHAT_EDITABLE_FIELDS}
    db.update_chat(cid, **fields)
    return jsonify({'ok': True})


@app.route('/api/chats/<int:cid>', methods=['DELETE'])
@require_project_access('member')
def delete_chat_route(cid):
    chat = db.get_chat(cid)
    db.delete_chat(cid)
    if chat and chat.get('file_path') and os.path.exists(chat['file_path']):
        try:
            os.remove(chat['file_path'])
        except Exception:
            pass
    return jsonify({'ok': True})


@app.route('/api/chats/status', methods=['GET'])
@require_auth
def chat_status_route():
    with _chat_execution_lock:
        active = dict(_active_chat_execution) if _active_chat_execution else None
    # Phase 3B: the active chat id must not leak across project boundaries.
    # Resolve the running chat to its project and only expose it to viewers;
    # everyone else sees the inactive shape (response keys unchanged).
    if active is not None and auth_enabled():
        _cid = active.get('chat_id')
        _chat = db.get_chat(_cid) if _cid is not None else None
        _pid = _chat.get('project_id') if _chat else None
        if _pid is None or not has_project_access(g.current_user, _pid, 'viewer'):
            active = None
    return jsonify({
        'busy': active is not None,
        'active_chat_id': active.get('chat_id') if active else None,
        'started_at': active.get('started_at') if active else None,
    })


def _record_chat_error(cid, message):
    """Persist a failed execution row for a chat reply that errored before
    creating one, so the frontend's reply-status poll terminates instead of
    hanging on `no_execution` forever."""
    try:
        chat = db.get_chat(cid)
        if not chat:
            return
        project_path = chat.get('project_path') or ''
        exec_id = db.create_execution(
            task_id=chat.get('task_id'),
            model=chat.get('model') or '',
            chat_id=cid,
            project_path=project_path or None,
        )
        db.finish_execution(
            exec_id, 'failed', error_message=message,
            project_path=project_path or None,
        )
        # Document the failure in the transcript so the user message (which may
        # already have been appended by chat_reply) is followed by an explanation
        # rather than a dangling message with no reply.
        chat_path = chat.get('file_path') or ''
        if chat_path and os.path.exists(chat_path):
            chats_mod.append_system_note(chat_path, f'Error: {message}')
    except Exception:
        app.logger.exception('_record_chat_error failed for chat %s', cid)


@app.route('/api/chats/<int:cid>/message', methods=['POST'])
@require_project_access('member')
def post_chat_message(cid):
    """Queue a chat message and return immediately (HTTP 202).

    The model reply can take minutes (vibe CLI runs up to 80 turns / 900s),
    which exceeds the Cloudflare Tunnel's ~100s origin-request timeout and
    surfaces as a 502 to the browser. So the reply runs in a background thread
    and the frontend polls `/api/chats/<cid>/reply-status` until it's done.
    """
    global _active_chat_execution
    data = request.json or {}
    text  = (data.get('text') or '').strip()
    model = (data.get('model') or '').strip() or None
    if not text:
        return jsonify({'error': 'text required'}), 400
    with _chat_execution_lock:
        if _active_chat_execution is not None:
            return jsonify({
                'error': 'Another chat is already working. Wait for it to finish before sending a new message.',
                'active_chat_id': _active_chat_execution.get('chat_id'),
            }), 409
        _active_chat_execution = {'chat_id': cid, 'started_at': datetime.now(timezone.utc).isoformat()}

    def _run():
        global _active_chat_execution
        try:
            result = chat_reply(cid, text, model=model)
            if 'error' in result and 'reply' not in result:
                # chat_reply returned an error. If it already created and failed
                # an execution row (late errors carry an exec_id), that row is
                # sufficient for reply-status to report done. Only when NO
                # execution row exists (early errors: EU guard refusal, chat not
                # found, failed to create execution) do we record a failed
                # execution ourselves, otherwise reply-status would report
                # `no_execution` forever and the frontend poll would hang.
                if 'exec_id' not in result:
                    _record_chat_error(cid, result.get('error') or 'Chat reply failed')
                return
            # Spend is recorded centrally in route().
            # For scaffolding chats, opportunistically persist the latest full draft.
            # If the reply doesn't include a full-roadmap fence, the stored draft is
            # left untouched (server-side fallback).
            _maybe_update_scaffold_draft(cid, result.get('reply') or '')
        except Exception:
            app.logger.exception('chat_reply background thread failed for chat %s', cid)
            _record_chat_error(cid, 'Internal error while generating the reply')
        finally:
            with _chat_execution_lock:
                _active_chat_execution = None

    threading.Thread(target=_run, name=f'chat-reply-{cid}', daemon=True).start()

    # Return immediately; the frontend polls reply-status for completion.
    return jsonify({
        'accepted': True,
        'chat_id': cid,
        'stream_url': f'/api/chats/{cid}/stream',
        'poll_url': f'/api/chats/{cid}/reply-status',
    }), 202


@app.route('/api/chats/<int:cid>/promote-memory', methods=['POST'])
@require_project_access('member')
def promote_chat_memory(cid):
    """Promote selected chat content into scoped memory."""
    chat = db.get_chat(cid)
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404

    data = request.json or {}
    level = (data.get('target_scope') or data.get('level') or 'phase').strip()
    mode = (data.get('mode') or 'compact').strip()
    if level not in ('workflow', 'phase', 'project'):
        return jsonify({'error': 'target_scope must be workflow, phase, or project'}), 400
    if mode not in ('compact', 'raw'):
        return jsonify({'error': 'mode must be compact or raw'}), 400

    transcript = chats_mod.parse_messages(chat.get('file_path') or '')
    selected = _extract_chat_selection(transcript, data.get('selection') or 'last_assistant')
    if not selected:
        return jsonify({'error': 'No chat content matched that selection'}), 400

    model = ((data.get('model') or '').strip() or chat.get('model') or chat.get('task_model')
             or agent_config.default_model_for(db.get_project(chat.get('project_id'))
                                               if chat.get('project_id') else None))
    write_content = selected
    compaction = None
    if mode == 'compact':
        compaction = _compact_for_memory(
            selected,
            level=level,
            model=model,
            project_path=chat.get('project_path') or '',
            source_label=f'chat #{cid}: {chat.get("name") or "Chat"}',
        )
        if compaction.get('error'):
            return jsonify({'error': compaction['error']}), 502
        write_content = compaction.get('content') or ''
        if not write_content:
            return jsonify({'error': 'Compaction found no durable memory delta'}), 422

    phase_name = (data.get('phase_name') or chat.get('phase_name') or '').strip()
    if level == 'phase' and not phase_name:
        return jsonify({'error': 'phase memory promotion requires a phase_name'}), 400

    workflow_id = None
    if level == 'workflow':
        workflow_id = data.get('workflow_id') or data.get('session_id') or cid

    path = mem.append_to_memory(
        chat.get('project_path') or '',
        write_content,
        level=level,
        phase_name=phase_name if level == 'phase' else None,
        workflow_id=workflow_id,
        task_title=f'Chat promotion: {chat.get("name") or "Chat"}',
    )
    chats_mod.append_system_note(
        chat.get('file_path') or '',
        f'Promoted selected chat content to {level} memory ({os.path.basename(path)}).'
    )
    db.touch_chat(cid)

    return jsonify({
        'ok': True,
        'chat_id': cid,
        'memory_file': path,
        'level': level,
        'mode': mode,
        'selection_chars': len(selected),
        'written_chars': len(write_content),
        'compaction': compaction,
    })


@app.route('/api/chats/<int:cid>/transcript', methods=['GET'])
@require_project_access('viewer')
def get_chat_transcript(cid):
    chat = db.get_chat(cid)
    if not chat:
        return jsonify({'error': 'Chat not found'}), 404
    content = chats_mod.parse_messages(chat.get('file_path') or '')
    return jsonify({'content': content, 'lines': content.count('\n')})


# ── Cross-Chat Search ──────────────────────────────────────────────────────────

@app.route('/api/search/chats')
@require_auth
def search_chats():
    """Full-text search across all .chat.md files.
    Query param: q (required). Returns up to 20 results with snippet.
    Phase 3B: in auth mode, only chats in the caller's member projects
    are searched (admins search all).
    """
    import html
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'error': 'query parameter q required'}), 400

    allowed = None
    if auth_enabled():
        allowed = get_user_project_ids(g.current_user)

    results = []
    for chat in db.get_chats():
        if allowed is not None and chat.get('project_id') not in allowed:
            continue
        file_path = chat.get('file_path')
        if not file_path or not os.path.exists(file_path):
            continue
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception:
            continue
        # Simple case-insensitive search
        lower_content = content.lower()
        lower_q = q.lower()
        idx = lower_content.find(lower_q)
        if idx == -1:
            continue
        # Extract a snippet around the match
        start = max(0, idx - 80)
        end = min(len(content), idx + len(q) + 80)
        # Split at the known match offset and escape each part, so the
        # highlight survives case differences and HTML-special characters
        match_end = idx + len(q)
        snippet = (html.escape(content[start:idx])
                   + f'<mark>{html.escape(content[idx:match_end])}</mark>'
                   + html.escape(content[match_end:end]))
        results.append({
            'chat_id': chat['id'],
            'chat_name': html.escape(chat.get('name') or f'Chat #{chat["id"]}'),
            'project_name': html.escape(chat.get('project_name') or ''),
            'snippet': snippet,
        })
        if len(results) >= 20:
            break

    return jsonify({'results': results})


# ── Scaffold Progress SSE ──────────────────────────────────────────────────────

@app.route('/api/projects/<int:pid>/scaffold-progress')
@require_project_access('viewer')
def scaffold_progress(pid):
    """SSE endpoint that streams progress messages while the scaffolding AI
    drafts the roadmap. The frontend connects after creating the project.
    Reads from a per-project queue that the background thread fills.
    """
    import time as _time

    def _generate():
        import json as _json
        q = None
        with _scaffold_progress_lock:
            q = _scaffold_progress_queues.get(pid)
        if q is None:
            yield f'data: {_json.dumps({"type": "done"})}\n\n'
            return

        while True:
            try:
                msg = q.get(timeout=30)
                yield f'data: {_json.dumps(msg)}\n\n'
                if msg.get('type') == 'done':
                    return
            except queue.Empty:
                # No progress for 30s — send a heartbeat to keep connection alive
                yield f'data: {_json.dumps({"type": "heartbeat"})}\n\n'
                # Check if queue still exists
                with _scaffold_progress_lock:
                    if pid not in _scaffold_progress_queues:
                        yield f'data: {_json.dumps({"type": "done"})}\n\n'
                        return

    return Response(
        _generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


# ── Project Events SSE ──────────────────────────────────────────────────────

@app.route('/api/projects/<int:pid>/events')
@require_project_access('viewer')
def project_events(pid):
    """SSE endpoint: stream dirty-signal events for a project.
    Emits immediately on connect, then heartbeats every 25s.
    Events: task_changed, execution_changed, chat_changed, work_session_changed, cost_changed.
    Client refetches affected collections on receipt.
    """
    import time as _time
    import json as _json

    q = agent_events.subscribe(pid)

    def _generate():
        yield ': connected\n\n'
        last_heartbeat = _time.time()
        try:
            while True:
                try:
                    evt = q.get(timeout=15)
                    yield f'data: {_json.dumps(evt)}\n\n'
                    last_heartbeat = _time.time()
                except queue.Empty:
                    if _time.time() - last_heartbeat > 25:
                        yield ': heartbeat\n\n'
                        last_heartbeat = _time.time()
        finally:
            agent_events.unsubscribe(pid, q)

    return Response(
        _generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control':    'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/api/projects/<int:pid>/context-options')
@require_project_access('viewer')
def get_context_options(pid):
    """Return everything the user can attach to a chat in this project,
    grouped into definitions / memories / tasks / working_docs.
    """
    project = next((p for p in db.get_projects() if p['id'] == pid), None)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    project_path = project.get('path') or ''

    # Definitions: only files that actually exist on disk (detect READMEFIRST.md vs legacy CLAUDE.md)
    definitions = []
    for fname in get_def_files_for_project(project_path):
        fpath = os.path.join(project_path, fname)
        if os.path.exists(fpath):
            definitions.append({
                'kind':  'definition',
                'ref':   fname,
                'label': fname,
                'size':  os.path.getsize(fpath),
            })

    # Memories: every *.memory.md under Artifacts/
    memories = []
    for f in _phase_memory_metadata(pid, project_path):
        if f.get('file_type') == 'memory':
            memories.append({
                'kind':  'memory',
                'ref':   f['name'],
                'label': f['name'],
                'level': f.get('level'),
                'phase_name': f.get('phase_name') or '',
                'phase_names': f.get('phase_names') or [],
                'phase_slug': f.get('phase_slug') or f.get('phase'),
                'size':  f.get('size'),
            })

    # Tasks: every task in this project (compact form for the picker)
    tasks_opts = []
    for t in db.get_tasks(project_id=pid):
        tasks_opts.append({
            'kind':       'task',
            'ref':        t['id'],
            'label':      t['title'],
            'status':     t['status'],
            'phase_name': t.get('phase_name') or '',
        })

    # Working docs
    working_docs = []
    for f in mem.list_working_docs(project_path):
        working_docs.append({
            'kind':  'working_doc',
            'ref':   f['name'],
            'label': f['name'],
            'size':  f.get('size'),
            'ext':   f.get('ext'),
        })

    return jsonify({
        'definitions':  definitions,
        'memories':     memories,
        'tasks':        tasks_opts,
        'working_docs': working_docs,
    })


@app.route('/api/tasks/attachable')
@require_auth
def get_attachable_tasks():
    project_id = request.args.get('project_id', type=int)
    if not project_id:
        return jsonify({'error': 'project_id required'}), 400
    # Phase 3B: query-arg pid is gated in-handler (a decorator would turn the
    # missing-arg 400 above into a 404). Non-member → 404, admin passes.
    if auth_enabled() and not has_project_access(g.current_user, project_id, 'viewer'):
        return jsonify({'error': 'not_found'}), 404
    return jsonify(db.get_attachable_tasks(project_id))


# ── Work sessions ────────────────────────────────────────────────────────────

@app.route('/api/work-sessions', methods=['GET'])
@require_auth
def list_work_sessions():
    sessions = db.get_work_sessions()
    return jsonify(sessions)


@app.route('/api/work-sessions/start', methods=['POST'])
@require_admin
def start_work_session_route():
    """Manual/admin endpoint: start a column's 5-hour countdown.
    Body: { slot: 1-3, force?: bool }. Force re-starts an already-started slot.
    (The old /api/work-sessions/rotate endpoint was removed: slot rotation
    belonged to the retired rotating-window model; columns are provider-bound.)"""
    data  = request.json or {}
    slot  = int(data.get('slot') or 1)
    force = bool(data.get('force'))
    if slot < 1 or slot > 3:
        return jsonify({'error': 'slot must be 1-3'}), 400
    updated = db.start_work_session(slot, force=force)
    if updated is None:
        return jsonify({'error': 'slot not found'}), 404
    return jsonify(updated)


@app.route('/api/work-sessions/status', methods=['GET'])
@require_auth
def work_sessions_status():
    """Returns all 4 slots with computed timer + planned tokens.
    Each row: slot, status, started_at, ends_at, seconds_remaining,
              expired, token_budget, tokens_used, planned_tokens,
              task_count, done_count, duration_seconds, pause_reason.
    Phase 3C: aggregate counters only — no per-project task ids — so
    read-only @require_auth with no membership filtering."""
    rows = db.get_work_sessions()
    running = set()
    for r in rows:
        if _slot_run_thread(r['slot']) is not None:
            running.add(r['slot'])
    for r in rows:
        r['seq_running'] = r['slot'] in running
        # Attach pause_reason from the slot's next_run_at metadata
        nr = r.get('next_run_at')
        if nr:
            # Determine reason from the stored pause_reason column (if any)
            # Fallback: check if next_run_at is set (means paused)
            r['pause_reason'] = r.get('pause_reason') or 'window_full'
        else:
            r['pause_reason'] = None
    return jsonify(rows)


@app.route('/api/work-sessions/<int:slot>/run', methods=['POST'])
@require_admin
def run_work_session(slot):
    """Legacy alias for the kanban endpoint. Runs every confirmed task currently
    in column `slot` (no client-side filtering). Kept for any callers that still
    poke the old URL; new UI uses /api/columns/<slot>/run with explicit ids."""
    if slot < 1 or slot > 3:
        return jsonify({'error': 'slot must be 1-3'}), 400
    confirmed = db.get_column_tasks(slot, statuses=('confirmed',))
    return _run_task_ids(slot, [t['id'] for t in confirmed])


@app.route('/api/columns/<int:slot>/run', methods=['POST'])
@require_auth
def run_column(slot):
    """Run a client-supplied list of task IDs sequentially. Body: {task_ids:[...]}.
    Used by Claude Pro (first sub-session only), Mistral Pro, PAYG, EU Scaleway,
    and Ollama Cloud (slot 5). Each id is re-checked: must still be
    status=='confirmed' and in column `slot`.
    Returns immediately; execution happens in a background thread.
    Multi-tenancy: explicit id list, so members may run their own projects'
    tasks — every submitted id must resolve to a member+ project (fail closed
    with 404, anti-enumeration). Global admins bypass via has_project_access.
    (The slot-wide /api/work-sessions/<slot>/run stays admin-only: it
    server-selects tasks across projects.)"""
    if slot < 1 or slot > 5:
        return jsonify({'error': 'slot must be 1-5'}), 400
    data = request.json or {}
    task_ids = data.get('task_ids') or []
    if not isinstance(task_ids, list):
        return jsonify({'error': 'task_ids must be a list'}), 400
    if auth_enabled():
        _u = g.get('current_user')
        for _tid in task_ids:
            try:
                _t = db.get_task(int(_tid))
            except Exception:
                _t = None
            _pid = (_t or {}).get('project_id')
            if not _pid or _u is None or not has_project_access(_u, _pid, 'member'):
                return jsonify({'error': 'not_found'}), 404
    return _run_task_ids(slot, [int(t) for t in task_ids])


def _maybe_schedule_next_run(slot, quota_hit=False):
    """Decide whether the slot's auto-batch scheduler should auto-resume.
    Called from _run_seq's finally block.

    Rules:
      - If the slot still has confirmed tasks (more than fit in one window, or
        the window was cut short by quota), set next_run_at = started_at + 5h.
      - Otherwise clear next_run_at (the slot is fully drained).

    We use started_at + 5h regardless of quota_hit: that's when Anthropic's
    rolling subscription window resets. Picking last_done_at + 5h would wait
    longer than needed and waste the unused part of the window."""
    from datetime import timedelta
    from agent_config import SESSION_DURATION_HOURS

    remaining = db.get_column_tasks(slot, statuses=('confirmed',))
    if not remaining:
        db.set_slot_next_run(slot, None, None)
        return

    ws = db.get_work_session(slot)
    started = (ws or {}).get('started_at')
    try:
        if started:
            anchor = datetime.fromisoformat(started)
            if anchor.tzinfo:
                anchor = anchor.replace(tzinfo=None)
        else:
            anchor = datetime.utcnow()
    except Exception:
        anchor = datetime.utcnow()
    next_run = anchor + timedelta(hours=SESSION_DURATION_HOURS)

    # If anchor + 5h is already in the past (e.g. timer expired ages ago), fire
    # in 60s rather than immediately — gives the user a chance to react.
    now = datetime.utcnow()
    if next_run <= now:
        next_run = now + timedelta(seconds=60)

    reason = 'quota' if quota_hit else 'window_full'
    db.set_slot_next_run(slot, next_run.isoformat(), reason)


def _slot_scheduler_tick():
    """One pass of the auto-batch scheduler. Idempotent + safe to call from any
    thread; the per-slot lock prevents double-firing."""
    now = datetime.utcnow()
    for ws in db.get_work_sessions():
        slot = ws.get('slot')
        nr = ws.get('next_run_at')
        if not nr:
            continue
        try:
            if datetime.fromisoformat(nr) > now:
                continue
        except Exception:
            continue
        if _slot_run_thread(slot) is not None:
            continue
        confirmed = db.get_column_tasks(slot, statuses=('confirmed',))
        if not confirmed:
            db.set_slot_next_run(slot, None, None)
            continue
        # Reset the 5h timer for the new window, clear next_run_at, kick off run.
        db.start_work_session(slot, force=True)
        db.set_slot_next_run(slot, None, None)
        _kick_run_seq(slot, [t['id'] for t in confirmed])


def _slot_scheduler_loop():
    import time
    _sweep_counter = 0
    _trash_counter = 0
    _uploads_counter = 0
    _ptrash_counter = 0
    _archive_counter = 0
    while True:
        try:
            _slot_scheduler_tick()
            # GPU upkeep: accrue hourly cost for open windows (so the UI shows
            # live spend) and auto-close idle/over-budget windows. Previously
            # these were defined but never scheduled, so a forgotten window
            # billed until someone closed it by hand.
            try:
                agent_scw_deploy.accrue_all(region='fr-par')
                agent_scw_deploy.auto_cleanup_check(region='fr-par')
            except Exception as _scw_err:
                print(f'[slot-scheduler] gpu upkeep error: {_scw_err}', file=sys.stderr)
            # Periodic orphan sweep: every 5 ticks (~5 min), fail stale running
            # executions whose heartbeat is stale. A 30s watchdog touches
            # last_heartbeat_at for every live execution, so healthy batches
            # survive indefinitely (see reset_orphaned_executions, 10-min staleness).
            _sweep_counter += 1
            if _sweep_counter % 5 == 0:
                try:
                    _reset = db.reset_orphaned_executions(reason='tick')
                    if _reset:
                        print(f'[orphan-sweep] reset {_reset} execution(s) to failed')
                except Exception as _se:
                    print(f'[orphan-sweep] error: {_se}', file=sys.stderr)
            # R1: reap expired .trash/ entries daily (~1440 ticks) so a
            # long-running service still hard-deletes trashed projects past
            # their retention window, not only at boot.
            _trash_counter += 1
            if _trash_counter % 1440 == 0:
                try:
                    _reaped = _reap_trash()
                    if _reaped:
                        print(f'[trash-reaper] reaped {_reaped} expired trash entr(ies)')
                except Exception as _te:
                    print(f'[trash-reaper] error: {_te}', file=sys.stderr)
            # Built archives past retention (daily). Same cadence as .trash.
            _archive_counter += 1
            if _archive_counter % 1440 == 0:
                try:
                    _areaped = _reap_archives()
                    if _areaped:
                        print(f'[archive-reaper] reaped {_areaped} expired archive(s)')
                except Exception as _ae:
                    print(f'[archive-reaper] error: {_ae}', file=sys.stderr)
            # R2: reap stale .uploads/ chunk dirs older than 24h (daily)
            _uploads_counter += 1
            if _uploads_counter % 1440 == 0:
                try:
                    _ureaped = _reap_uploads()
                    if _ureaped:
                        print(f'[uploads-reaper] reaped {_ureaped} stale uploads dir(s)')
                except Exception as _ue:
                    print(f'[uploads-reaper] error: {_ue}', file=sys.stderr)
            # R3: reap per-project .trash/files/ entries past retention (daily)
            _ptrash_counter += 1
            if _ptrash_counter % 1440 == 0:
                try:
                    _ptreaped = _reap_project_trash()
                    if _ptreaped:
                        print(f'[project-trash-reaper] reaped {_ptreaped} expired per-project trash entr(ies)')
                except Exception as _pte:
                    print(f'[project-trash-reaper] error: {_pte}', file=sys.stderr)
        except Exception as e:
            print(f'[slot-scheduler] tick error: {e}', file=sys.stderr)
        time.sleep(60)


def _slot_run_thread(slot):
    """Return the live run thread for `slot`, or None if the slot is not
    currently running (or its thread has died)."""
    with _running_slots_lock:
        entry = _running_slots.get(slot)
        if not entry:
            return None
        t = entry.get('thread')
        if t is _RESERVED:
            # Claimed but the run thread has not started yet — treat as running.
            return _RESERVED
        if t is None or not t.is_alive():
            # Stale entry: the thread is gone but the flag was never cleared.
            # Drop it so the column is no longer blocked.
            _running_slots.pop(slot, None)
            return None
        return t


def _preflight_token_budget(task_ids):
    """UX pre-flight: refuse to kick a run batch when any submitted task's
    quota owner is already over their monthly token budget.

    Pure check — no counter mutation; the executor's own pre-flight remains
    the authoritative enforcement. Returns the QuotaError, or None when the
    batch may start. Without this, the kick returns ok:true and a quota
    failure dies silently in the background thread: no execution row, the
    task stays confirmed, and the UI keeps showing "Running" with no
    explanation of the limit."""
    try:
        import agent_quotas
    except Exception:
        return None
    _seen_owners = set()
    for _tid in task_ids or []:
        try:
            _t = db.get_task(int(_tid))
        except Exception:
            continue
        if not _t:
            continue
        try:
            _proj = db.get_project((_t or {}).get('project_id')) or {}
            _owner = agent_quotas.resolve_owner_id(_t, _proj)
        except Exception:
            continue
        if not _owner or _owner in _seen_owners:
            continue
        _seen_owners.add(_owner)
        try:
            agent_quotas.check_token_budget(_owner)
        except agent_quotas.QuotaError as e:
            return e
    return None


def _preflight_model_allowed(task_ids):
    """UX pre-flight: refuse to kick a run batch when any submitted task's
    model is outside its quota owner's free-tier whitelist.

    Without this the kick returns ok:true, the background run dies on the
    router's whitelist gate, and the reason never reaches the UI — a failed
    exec row appears with a consumed run and no explanation (task #10001176
    user report: free-tier run with Claude failed silently). Callers map
    the returned error to a response the frontend toasts, same as the token
    budget pre-flight. No execution row is created, no run is consumed.
    """
    try:
        import agent_quotas
    except Exception:
        return None
    for _tid in task_ids or []:
        try:
            _t = db.get_task(int(_tid))
        except Exception:
            continue
        if not _t:
            continue
        try:
            _proj = db.get_project((_t or {}).get('project_id')) or {}
            _owner = agent_quotas.resolve_owner_id(_t, _proj)
        except Exception:
            continue
        if not _owner:
            continue
        try:
            if agent_quotas.quotas_applicable(_owner) and \
                    not agent_quotas.is_allowed_model(_t.get('model') or '', _owner):
                return agent_quotas.QuotaError(
                    f"Model '{_t.get('model')}' is not available on the free tier. "
                    "Allowed: mistral-*, scw-* (excl. GPU), oll-*. "
                    'Upgrade to a paid plan to use it, or switch the task to a free model.',
                    field='model', limit=0, current=0)
        except Exception:
            continue
    return None


def _kick_run_seq(slot, task_ids):
    """Start a background run thread for `slot` over the supplied task_ids.
    Returns (ok, message). Safe to call from any thread — does not need a
    Flask request context. Used by both the HTTP route wrapper and the
    auto-batch scheduler."""
    # Fail fast on an exhausted token budget (UX pre-flight only — the
    # executor re-checks authoritatively before spending). Callers map
    # False to an error response carrying the message.
    _qerr = _preflight_token_budget(task_ids)
    if _qerr is not None:
        return False, str(_qerr)
    # Same for the free-tier model whitelist: refuse the kick up front with
    # the reason instead of letting the run fail silently in the background.
    _merr = _preflight_model_allowed(task_ids)
    if _merr is not None:
        return False, str(_merr)
    with _running_slots_lock:
        entry = _running_slots.get(slot)
        if entry:
            t = entry.get('thread')
            if t is _RESERVED or (t is not None and t.is_alive()):
                return False, f'Column {slot} is already running'
            # Stale entry: the previous thread died without clearing the flag.
            _running_slots.pop(slot, None)
        if not task_ids:
            return True, 'No tasks to run'
        _running_slots[slot] = {'thread': _RESERVED, 'started': time.time()}
    # Emit work_session_changed for all subscribers
    agent_events.emit_global_safe({'type': 'work_session_changed', 'slot': slot, 'status': 'started'})

    def _run_seq():
        quota_hit = False
        previous_result = None
        skipped_because_deps = []
        try:
            for tid in task_ids:
                current = db.get_task(tid)
                if not current or current['status'] != 'confirmed':
                    continue
                if current.get('work_session_slot') != slot:
                    continue

                # Enforce dependencies: if any dependency is not 'done', skip
                # this task. Leave it confirmed so it can be re-run later.
                deps = db.get_dependencies(tid)
                unmet = [d for d in deps if d.get('status') != 'done']
                if unmet:
                    skipped_because_deps.append((tid, [d['id'] for d in unmet]))
                    app.logger.info(
                        'task %s skipped (unmet deps): %s',
                        tid, [d['id'] for d in unmet]
                    )
                    continue

                # H2a clarification gate: if the task is on hold awaiting answers
                # to clarifying questions, skip it (leave confirmed) so the user
                # can answer on the card. Once answered, gate_state → 'open' and
                # the next run picks it up.
                if current.get('gate_state') == 'hold' and current.get('gate_source') == 'H2a':
                    app.logger.info(
                        'task %s skipped (awaiting clarification): %s',
                        tid, current.get('gate_reason') or 'questions pending'
                    )
                    continue

                if previous_result:
                    generate_queue_handoff(previous_result, tid)
                result = run_task(tid)
                if not isinstance(result, dict):
                    continue
                previous_result = result
                status = result.get('status')
                if status == 'quota_paused':
                    quota_hit = True
                    break
                if status == 'failed' and result.get('quota'):
                    # Free-tier quota (runs/tokens) refused this task before
                    # any execution was created. The task stays confirmed for
                    # retry — but the kick already returned ok:true, so the
                    # failure must be surfaced: log it, mark the scheduler
                    # reason correctly, and notify the UI. Break: the block
                    # is account-level, further tasks would fail identically
                    # (each burning one run from the budget).
                    quota_hit = True
                    app.logger.warning('Slot %s: task %s quota-blocked: %s',
                                       slot, tid, result.get('error'))
                    try:
                        agent_events.emit_global_safe({
                            'type': 'run_failed', 'slot': slot,
                            'task_id': tid, 'error': result.get('error'),
                            'quota': result.get('quota'),
                            'limit': result.get('limit'),
                        })
                    except Exception:
                        pass
                    break
        finally:
            if skipped_because_deps:
                app.logger.warning(
                    'Slot %s: %s task(s) skipped due to unmet dependencies: %s',
                    slot, len(skipped_because_deps),
                    ', '.join(f'#{t}→{deps}' for t, deps in skipped_because_deps[:5])
                )
            with _running_slots_lock:
                _running_slots.pop(slot, None)
            try:
                _maybe_schedule_next_run(slot, quota_hit=quota_hit)
            except Exception as e:
                app.logger.warning('Slot %s: _maybe_schedule_next_run failed: %s', slot, e)
            # Emit work_session_changed for all subscribers
            agent_events.emit_global_safe({'type': 'work_session_changed', 'slot': slot, 'status': 'finished'})

    t = threading.Thread(target=_run_seq, daemon=True, name=f'slot-{slot}-run')
    with _running_slots_lock:
        _running_slots[slot] = {'thread': t, 'started': time.time()}
    t.start()
    return True, None


def _run_task_ids(slot, task_ids):
    ok, msg = _kick_run_seq(slot, task_ids)
    if not ok:
        return jsonify({'error': msg}), 409
    if msg == 'No tasks to run':
        return jsonify({'ok': True, 'slot': slot, 'queued': 0, 'message': msg})
    return jsonify({'ok': True, 'slot': slot, 'queued': len(task_ids)})


@app.route('/api/tasks/<int:tid>/archive', methods=['POST'])
@require_project_access('member')
def archive_task_route(tid):
    task = db.get_task(tid)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    db.archive_task(tid)
    return jsonify({'ok': True})


# ── Phases ────────────────────────────────────────────────────────────────────

@app.route('/api/phases')
@require_auth
def get_phases():
    from agent_phases import get_all_phases
    phases = get_all_phases()
    # Phase 3C: entries carry per-project task titles — non-admins see only
    # their member projects (matched by path; entries carry no pid).
    if auth_enabled():
        allowed = get_user_project_ids(g.current_user)
        if allowed is not None:
            id_by_path = {p.get('path'): p.get('id') for p in db.get_projects()}
            phases = [e for e in phases if id_by_path.get(e.get('path')) in allowed]
    return jsonify(phases)


# ── Improve prompt ────────────────────────────────────────────────────────────

@app.route('/api/improve-prompt', methods=['POST'])
@require_auth
def improve_prompt():
    data = request.json or {}
    title       = data.get('title', '').strip()
    description = data.get('description', '').strip()
    attachments = data.get('attachments', [])
    project_id  = data.get('project_id')
    # Phase 3C: spends tokens + reads project files — member of the body's
    # project_id. project_id is optional (new-project pitch path), so this is
    # in-handler: no project_id means no membership to check.
    if project_id and auth_enabled():
        try:
            _imp_pid = int(project_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'not_found'}), 404
        if not has_project_access(g.current_user, _imp_pid, 'member'):
            return jsonify({'error': 'not_found'}), 404
    stack       = data.get('stack', '').strip()
    files       = data.get('files', [])  # inline file content: [{name, content}]
    requires_rag = 1 if data.get('requires_rag') else 0
    corpus_id    = (data.get('corpus_id') or '').strip()

    # Build the meta prompt with attachments as context
    attachment_context = ""
    # Resolve project path for quota metering + attachment loading
    _imp_proj_path = None
    if project_id:
        try:
            _imp_pid = int(project_id)
        except (TypeError, ValueError):
            _imp_pid = None
        if _imp_pid:
            _imp_proj = next((p for p in db.get_projects() if p['id'] == _imp_pid), None)
            _imp_proj_path = (_imp_proj.get('path') or '') if _imp_proj else None

    if attachments and project_id:
        project_id_int = int(project_id) if project_id else None
        project = next((p for p in db.get_projects() if p['id'] == project_id_int), None)
        if project:
            project_path = project.get('path') or ''
            for att in attachments:
                kind = att.get('kind')
                ref = att.get('ref')
                label = att.get('label', ref)
                content = _load_attachment_content(kind, ref, project_path)
                if content:
                    attachment_context += f"\n[Attached: {label}]\n{content}\n"

    # Inline file content (e.g. from New Project modal — no project exists yet)
    inline_context = ""
    for f in (files or []):
        fname = f.get('name', 'file')
        fcontent = f.get('content', '')
        inline_context += f"\n[Attached file: {fname}]\n{fcontent}\n"

    full_context = attachment_context + inline_context

    # RAG instruction — when the task is flagged to use the shared legal library,
    # tell the instruction-writer to make the improved instructions require the
    # model to ground its answer on retrieved citations (Dz.U./CELEX/ELI).
    rag_context = ""
    if requires_rag:
        rag_context = (
            "\n\nIMPORTANT: This task is flagged to use the shared RAG legal library "
            f"(corpus: {corpus_id or 'railway'}). The improved instructions MUST instruct "
            "the model to answer ONLY from the retrieved legal context and cite "
            "Dz.U. + ELI + CELEX verbatim extracts; if the answer is not in the context, "
            "say 'Brak w dostarczonych aktach' — never answer statutes from memory or "
            "invent citations.\n"
            "CRITICAL: Do NOT include any example answer, sample citation, or worked "
            "example in the improved instructions. You do not have access to the corpus, "
            "so any Dz.U./CELEX/ELI reference you invent would be fabricated and would "
            "poison the task. Write only the instructions — no example output, no "
            "made-up article numbers or legal references.\n"
        )

    if project_id and not files:
        # Existing task improvement path
        meta = (
            "You are an expert AI task instruction writer. "
            "A user has a task they want an AI to complete. "
            "Improve their instructions to be specific, actionable, and produce the best possible output.\n\n"
            f"Task title: {title}\n\n"
            f"Current instructions:\n{description or '(none provided)'}\n\n"
            f"{full_context}"
            f"{rag_context}"
            "Write improved instructions. Be concrete: what to produce, what sources to use, "
            "what format, what success looks like.\n\n"
            "Rules:\n"
            "- Do not add deliverables, recipients or side effects the user did not ask for: no "
            "sending email, publishing, posting, deleting or moving files, or starting services "
            "unless the current instructions ask for it. Attached files are background context "
            "only; never turn something they describe into a new step.\n"
            "- Output files: you may name them, but never give them a folder. Cordée saves every "
            "task output in the task's own folder under Artifacts/outputs/. Never tell the task to "
            "save in the project root or in Working Docs / Working Documents (reference material).\n"
            "- Keep input file paths exactly as the user wrote them, with plain spaces (never "
            "URL-encode them as %20).\n\n"
            "Return ONLY the improved instructions text, no preamble, no explanation."
        )
    else:
        # Project scaffolding path (no project exists yet)
        meta = (
            "You are an expert AI project architect. A user is creating a new project and wants a clear, "
            "well-structured project pitch/description that will be used to scaffold the project roadmap.\n\n"
            f"Project name: {title}\n\n"
            f"Current pitch:\n{description or '(none provided)'}\n\n"
            f"Tech stack hints: {stack or '(none provided)'}\n\n"
            f"{full_context}"
            "Write an improved project pitch. Be specific: what the project does, who it's for, "
            "key features, tech choices, and success criteria. Keep it to one well-crafted paragraph "
            "(3-6 sentences). Return ONLY the improved pitch text, no preamble, no explanation."
        )

    try:
        from agent_router import route
        text, tok_in, tok_out, cost = route("mistral-medium-latest", meta, max_tokens=2048,
                                            force_mistral_mode="api",
                                            policy_path=_imp_proj_path)
        return jsonify({'improved': text.strip(), 'cost_usd': cost})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def _load_attachment_content(kind, ref, project_path):
    """Load content of an attachment based on its kind and reference."""
    try:
        if kind == 'definition':
            # Definitions are in project root
            fpath = _safe_resolve(project_path, ref)
            if fpath and os.path.exists(fpath):
                with open(fpath, 'r', encoding='utf-8') as f:
                    return f.read()
        elif kind == 'memory':
            # Memories are in Artifacts/
            fpath = _safe_resolve(project_path, 'Artifacts', ref)
            if fpath and os.path.exists(fpath):
                with open(fpath, 'r', encoding='utf-8') as f:
                    return f.read()
        elif kind == 'working_doc':
            # Working docs are in Working Docs/
            fpath = _safe_resolve(project_path, 'Working Docs', ref)
            if fpath and os.path.exists(fpath):
                with open(fpath, 'r', encoding='utf-8') as f:
                    return f.read()
        elif kind == 'task':
            # For tasks, we get the task description from the database.
            # Scope to this project: task ids are only unique per project, so an
            # unscoped lookup would leak another tenant's task description.
            task = db.get_task(int(ref))
            if task:
                try:
                    proj = db.get_project_by_path(project_path) if project_path else None
                except Exception:
                    proj = None
                if proj and task.get('project_id') is not None and \
                        task.get('project_id') != proj.get('id'):
                    return None
                return task.get('description', '')
    except Exception:
        pass
    return None


# ── Roles ─────────────────────────────────────────────────────────────────────

@app.route('/api/roles')
@require_auth
def list_roles():
    return jsonify(db.get_roles())


@app.route('/api/roles', methods=['POST'])
@require_admin
def create_role():
    data = request.json or {}
    if not data.get('name'):
        return jsonify({'error': 'name required'}), 400
    role = db.create_role(
        name=data['name'],
        system_prompt=data.get('system_prompt', ''),
        default_model=data.get('default_model', ''),
        context_scope=data.get('context_scope', ''),
        is_template=int(data.get('is_template', 0)),
        project_id=None,
    )
    return jsonify(role), 201


@app.route('/api/roles/<int:rid>', methods=['PUT'])
@require_auth
def update_role(rid):
    role = db.get_role(rid)
    if not role:
        return jsonify({'error': 'Role not found'}), 404
    # Phase 3C: project-scoped role → owner of that project; global role
    # (project_id NULL) → global admin. In-handler: the URL carries no pid.
    if auth_enabled():
        _rpid = role.get('project_id')
        if _rpid is not None:
            if not has_project_access(g.current_user, _rpid, 'owner'):
                return jsonify({'error': 'not_found'}), 404
        elif g.current_user.get('role') != 'admin':
            return jsonify({'error': 'not_found'}), 404
    data = request.json or {}
    db.update_role(rid, **{k: data[k] for k in
                           ('name', 'system_prompt', 'default_model', 'context_scope', 'is_template')
                           if k in data})
    return jsonify({'ok': True})


@app.route('/api/roles/<int:rid>', methods=['DELETE'])
@require_auth
def delete_role(rid):
    role = db.get_role(rid)
    if not role:
        return jsonify({'error': 'Role not found'}), 404
    # Phase 3C: same branch as PUT — project role → project owner,
    # global role → global admin.
    if auth_enabled():
        _rpid = role.get('project_id')
        if _rpid is not None:
            if not has_project_access(g.current_user, _rpid, 'owner'):
                return jsonify({'error': 'not_found'}), 404
        elif g.current_user.get('role') != 'admin':
            return jsonify({'error': 'not_found'}), 404
    db.delete_role(rid)
    return jsonify({'ok': True})


@app.route('/api/projects/<int:pid>/roles')
@require_project_access('viewer')
def list_project_roles(pid):
    if not db.get_project(pid):
        return jsonify({'error': 'Project not found'}), 404
    return jsonify(db.get_roles(pid))


@app.route('/api/projects/<int:pid>/roles', methods=['POST'])
@require_project_access('owner')
def create_project_role(pid):
    if not db.get_project(pid):
        return jsonify({'error': 'Project not found'}), 404
    data = request.json or {}
    if not data.get('name'):
        return jsonify({'error': 'name required'}), 400
    role = db.create_role(
        name=data['name'],
        system_prompt=data.get('system_prompt', ''),
        default_model=data.get('default_model', ''),
        context_scope=data.get('context_scope', ''),
        is_template=int(data.get('is_template', 0)),
        project_id=pid,
    )
    return jsonify(role), 201


@app.route('/api/projects/<int:pid>/roles/<int:rid>', methods=['DELETE'])
@require_project_access('owner')
def delete_project_role(pid, rid):
    role = db.get_role(rid)
    if not role or role.get('project_id') != pid:
        return jsonify({'error': 'Role not found for this project'}), 404
    db.delete_role(rid)
    return jsonify({'ok': True})


@app.route('/api/roles/templates')
@require_auth
def list_role_templates():
    return jsonify(db.get_role_templates())


@app.route('/api/projects/<int:pid>/roles/from-template', methods=['POST'])
@require_project_access('owner')
def apply_role_template(pid):
    if not db.get_project(pid):
        return jsonify({'error': 'Project not found'}), 404
    data = request.json or {}
    template_id = data.get('template_id')
    if not template_id:
        return jsonify({'error': 'template_id required'}), 400
    created = db.apply_role_template(pid, template_id)
    return jsonify({'created': created, 'count': len(created)}), 201


# ── Master Spec ────────────────────────────────────────────────────────────────

@app.route('/api/projects/<int:pid>/spec', methods=['GET'])
@require_project_access('viewer')
def get_project_spec(pid):
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    data = _get_spec_data(project['path'])
    data['path'] = os.path.join(project['path'], 'Artifacts', 'master-spec.json')
    return jsonify(data)


@app.route('/api/projects/<int:pid>/spec', methods=['PUT'])
@require_project_access('member')
def put_project_spec(pid):
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    body = request.json or {}
    content = body.get('content')
    if content is None or not isinstance(content, dict):
        return jsonify({'error': 'content must be a JSON object'}), 400
    artifacts_dir = os.path.join(project['path'], 'Artifacts')
    spec_path = os.path.join(artifacts_dir, 'master-spec.json')
    os.makedirs(artifacts_dir, exist_ok=True)
    content['schema_version'] = content.get('schema_version', 0) + 1
    content['updated_at'] = datetime.utcnow().isoformat()
    with open(spec_path, 'w', encoding='utf-8') as f:
        json.dump(content, f, indent=2, ensure_ascii=False)
    return jsonify({'ok': True, 'spec': content, 'path': spec_path})


@app.route('/api/projects/<int:pid>/spec/merge', methods=['POST'])
@require_project_access('member')
def merge_project_spec(pid):
    project = db.get_project(pid)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    body = request.json or {}
    delta = body.get('delta')
    if delta is None or not isinstance(delta, dict):
        return jsonify({'error': 'delta must be a JSON object'}), 400
    changed_keys = _spec_merge_inplace(project['path'], delta)
    if changed_keys is None:
        return jsonify({'error': 'Spec merge failed'}), 500
    spec_data = _get_spec_data(project['path'])
    return jsonify({'ok': True, 'spec': spec_data['content'], 'changed_keys': changed_keys})


# ── Boot ──────────────────────────────────────────────────────────────────────

_slot_scheduler_started = False


def _start_slot_scheduler_once():
    global _slot_scheduler_started
    if _slot_scheduler_started:
        return
    _slot_scheduler_started = True
    threading.Thread(target=_slot_scheduler_loop, daemon=True, name='slot-scheduler').start()
    print('[slot-scheduler] background tick started (60s interval)')


def _apply_saved_app_settings():
    """Re-apply Settings › General values saved in app_settings over the env
    defaults; before this they were lost on every restart."""
    try:
        saved = db.get_app_settings()
    except Exception as _e:
        print(f'[startup] WARNING: could not read app_settings: {_e}')
        return
    if saved.get('anthropic_mode') in ('api', 'claude-code'):
        agent_config.ANTHROPIC_MODE = saved['anthropic_mode']
    if saved.get('mistral_mode') in ('api', 'vibe'):
        agent_config.MISTRAL_MODE = saved['mistral_mode']
    try:
        if saved.get('claude_pro_token_budget'):
            agent_config.SESSION_TOKEN_BUDGET = int(saved['claude_pro_token_budget'])
    except ValueError:
        pass


if __name__ == '__main__':
    db.init_db()
    _apply_saved_app_settings()
    # Bootstrap: generate aingel.json for any project that doesn't have one yet.
    try:
        _bootstrapped = 0
        for _p in db.get_projects():
            if _p.get('path') and not os.path.exists(os.path.join(_p['path'], 'aingel.json')):
                _write_aingel_json(_p['path'], _p)
                _bootstrapped += 1
        if _bootstrapped:
            print(f'[startup] Generated aingel.json for {_bootstrapped} project(s)')
    except Exception as _e:
        print(f'[startup] WARNING: aingel.json bootstrap failed: {_e}')
    # Defensive: a bug in the orphan sweep must never crash-loop the service on
    # boot (it would block the very restart that clears the orphan). Log and move on.
    try:
        _reset = db.reset_orphaned_executions(reason='startup')
        if _reset:
            print(f'[startup] Reset {_reset} orphaned execution(s) to failed/pending')
    except Exception as _e:
        print(f'[startup] WARNING: orphan sweep failed, continuing anyway: {_e}')
    # Reap expired .trash/ entries (non-EU projects past their retention window).
    try:
        _reaped = _reap_trash()
        if _reaped:
            print(f'[startup] Reaped {_reaped} expired trash entr(ies)')
    except Exception as _e:
        print(f'[startup] WARNING: trash reaper failed, continuing anyway: {_e}')
    # Reap built archives past their retention window.
    try:
        _areaped = _reap_archives()
        if _areaped:
            print(f'[startup] Reaped {_areaped} expired archive(s)')
    except Exception as _e:
        print(f'[startup] WARNING: archive reaper failed, continuing anyway: {_e}')
    # Reap stale .uploads/ chunk dirs older than 24h.
    try:
        _ureaped = _reap_uploads()
        if _ureaped:
            print(f'[startup] Reaped {_ureaped} stale uploads dir(s)')
    except Exception as _e:
        print(f'[startup] WARNING: uploads reaper failed, continuing anyway: {_e}')
    # Reap per-project .trash/files/ entries past their retention window.
    try:
        _ptreaped = _reap_project_trash()
        if _ptreaped:
            print(f'[startup] Reaped {_ptreaped} expired per-project trash entr(ies)')
    except Exception as _e:
        print(f'[startup] WARNING: project trash reaper failed, continuing anyway: {_e}')
    # Refresh the SuperAgent-managed A+B+C block in ~/.claude/settings.json.
    # Idempotent: only the sentinel-marked block is replaced, user entries are
    # preserved. Must never block boot.
    try:
        import agent_permissions as _ap
        _prov = _ap.provision_global_defaults()
        print(f'[startup] Global permission defaults provisioned: {_prov}')
    except Exception as _e:
        print(f'[startup] WARNING: global permission provisioning failed: {_e}')
    _start_slot_scheduler_once()
    _bind_host = os.environ.get('AINGEL_BIND_HOST', '127.0.0.1')
    print(f'SuperAgent running → http://{_bind_host}:{API_PORT}/')
    # Production WSGI server when available. Flask's app.run() is a development
    # server (single-process, no request limits); waitress is a real one and
    # still honours AINGEL_BIND_HOST (127.0.0.1 behind cloudflared).
    try:
        from waitress import serve
        print('[startup] Serving with waitress')
        # Every open tab holds a thread forever on /api/projects/<pid>/events
        # (SSE), and /api/execute holds one for the whole run. A small pool is
        # exhausted by a handful of tabs and the whole app stops responding, so
        # size it well above the expected number of concurrent tabs/runs.
        _threads = int(os.environ.get('AINGEL_WAITRESS_THREADS', '64'))
        _conn_limit = int(os.environ.get('AINGEL_WAITRESS_CONNECTIONS', '256'))
        serve(app, host=_bind_host, port=API_PORT, threads=_threads,
              connection_limit=_conn_limit)
    except ImportError:
        print('[startup] waitress not installed — falling back to Flask dev server')
        app.run(host=_bind_host, port=API_PORT, debug=False, threaded=True)
