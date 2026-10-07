import json
import os
import sqlite3
import threading
import logging
from datetime import datetime, timezone
from agent_config import DB_PATH, PROJECTS_ROOT
import agent_events

_log = logging.getLogger(__name__)


def _resolve_project_id(task_id=None, exec_id=None, chat_id=None, project_path=None):
    """Resolve project_id from task/exec/chat id or project_path.
    Returns project_id or None if not found. Safe to call from any thread.
    """
    if project_path:
        # Look up in projects table
        conn = get_db()
        row = conn.execute('SELECT id FROM projects WHERE path=?', (project_path,)).fetchone()
        conn.close()
        return row['id'] if row else None
    if task_id is not None:
        conn = get_db()
        row = conn.execute('SELECT project_id FROM task_registry WHERE id=?', (task_id,)).fetchone()
        conn.close()
        return row['project_id'] if row else None
    if exec_id is not None:
        conn = get_db()
        row = conn.execute('SELECT project_id FROM exec_registry WHERE id=?', (exec_id,)).fetchone()
        conn.close()
        return row['project_id'] if row else None
    if chat_id is not None:
        conn = get_db()
        row = conn.execute('SELECT project_id FROM chat_registry WHERE id=?', (chat_id,)).fetchone()
        conn.close()
        return row['project_id'] if row else None
    return None


def _emit_safe(project_id, event):
    """Wrapper around agent_events.emit that swallows all exceptions.
    Safe to call from DB write paths where failures must not break the write.
    """
    try:
        agent_events.emit(project_id, event)
    except Exception:
        pass


# ── Central DB (aingel.db) ────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout = 5000')
    return conn


# ── Per-project DB (project.db) ───────────────────────────────────────────────

# Per-project DB schema is migrated once per process, keyed by realpath. The
# heartbeat opens project DBs every 30s, so re-running the full schema script
# (executescript + PRAGMA table_info per table) on every open was pure waste.
# Cleared implicitly on restart, so a deploy re-runs migrations once per path.
_PROJECT_DB_READY: set = set()
_PROJECT_DB_READY_LOCK = threading.Lock()


def get_project_db(project_path):
    """Open <project_path>/project.db, auto-creating schema if needed."""
    db_path = os.path.join(project_path, 'project.db')
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout = 5000')
    key = os.path.realpath(db_path)
    with _PROJECT_DB_READY_LOCK:
        ready = key in _PROJECT_DB_READY
    if not ready:
        _init_project_db_schema(conn)
        with _PROJECT_DB_READY_LOCK:
            _PROJECT_DB_READY.add(key)
    return conn


def _add_column_if_missing(conn, table, col, defn):
    """Guarded ALTER TABLE that logs (instead of swallowing) failures.

    Each column is applied independently so one failure cannot skip the rest.
    """
    try:
        cols = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
        if col not in cols:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {defn}')
    except Exception as e:
        _log.warning('project.db: ALTER %s.%s failed: %s', table, col, e)


def _init_project_db_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS tasks (
            id               INTEGER PRIMARY KEY,
            project_id       INTEGER NOT NULL,
            external_id      TEXT,
            title            TEXT NOT NULL,
            description      TEXT DEFAULT '',
            phase_name       TEXT DEFAULT '',
            session_id       INTEGER,
            status           TEXT DEFAULT 'pending',
            model            TEXT DEFAULT 'claude-sonnet-5-5',
            priority         INTEGER DEFAULT 5,
            estimated_tokens INTEGER DEFAULT 50000,
            estimated_cost   REAL DEFAULT 0.0,
            actual_cost      REAL,
            handoff_context  TEXT DEFAULT '',
            handoff_source_exec_id INTEGER DEFAULT NULL,
            handoff_created_at TEXT DEFAULT NULL,
            source           TEXT DEFAULT 'md',
            source_file      TEXT DEFAULT NULL,
            work_session_slot INTEGER DEFAULT NULL,
            archived         INTEGER DEFAULT 0,
            slot_position    INTEGER DEFAULT 0,
            role_id          INTEGER,
            stale            INTEGER DEFAULT 0,
            created_by       INTEGER DEFAULT NULL,
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS executions (
            id               INTEGER PRIMARY KEY,
            task_id          INTEGER REFERENCES tasks(id),
            session_id       INTEGER,
            chat_id          INTEGER,
            model            TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'running',
            memory_status    TEXT DEFAULT 'pending',
            tokens_input     INTEGER DEFAULT 0,
            tokens_output    INTEGER DEFAULT 0,
            cost_usd         REAL DEFAULT 0.0,
            gpu_cost_usd     REAL DEFAULT 0.0,
            output_summary   TEXT DEFAULT '',
            error_message    TEXT DEFAULT '',
            git_branch       TEXT DEFAULT '',
            git_commit       TEXT DEFAULT '',
            git_diffstat     TEXT DEFAULT '',
            git_merge_commit TEXT DEFAULT '',
            aingel_brief     TEXT DEFAULT '',
            batch_parent_id  INTEGER DEFAULT NULL,
            created_by       INTEGER DEFAULT NULL,
            started_at       TEXT DEFAULT CURRENT_TIMESTAMP,
            finished_at      TEXT
        );
        CREATE TABLE IF NOT EXISTS chats (
            id               INTEGER PRIMARY KEY,
            project_id       INTEGER NOT NULL,
            phase_name       TEXT DEFAULT '',
            task_id          INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
            name             TEXT NOT NULL,
            status           TEXT DEFAULT 'active',
            file_path        TEXT DEFAULT '',
            model            TEXT DEFAULT '',
            attachments      TEXT DEFAULT '[]',
            scaffold_draft   TEXT DEFAULT '',
            auto_inject_defs INTEGER NOT NULL DEFAULT 1,
            created_by       INTEGER DEFAULT NULL,
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY,
            project_id    INTEGER,
            name          TEXT DEFAULT '',
            level         TEXT DEFAULT 'phase',
            phase_name    TEXT DEFAULT '',
            topic         TEXT DEFAULT '',
            primary_model TEXT DEFAULT '',
            chat_path     TEXT DEFAULT '',
            status        TEXT DEFAULT 'active',
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS project_skills (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id    INTEGER NOT NULL,
            category      TEXT NOT NULL,
            name          TEXT NOT NULL,
            auto_detected INTEGER NOT NULL DEFAULT 1,
            detected_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_id, category, name)
        );
        CREATE TABLE IF NOT EXISTS project_permissions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            group_key  TEXT NOT NULL,
            rule       TEXT NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 1,
            auto_added INTEGER NOT NULL DEFAULT 0,
            added_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_id, rule)
        );
        CREATE TABLE IF NOT EXISTS file_tags (
            rel_path   TEXT PRIMARY KEY,
            tags       TEXT NOT NULL DEFAULT '[]',
            note       TEXT DEFAULT '',
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS delete_tombstones (
            rel_path    TEXT NOT NULL,
            sha256      TEXT NOT NULL,
            deleted_at  TEXT NOT NULL,
            PRIMARY KEY (rel_path, sha256)
        );
    ''')
    # ── Column migrations ──────────────────────────────────────────────────────
    # CREATE TABLE IF NOT EXISTS does not add columns to existing tables, so
    # guarded ALTER TABLE statements are needed for project.db files created
    # before the relevant phase. Runs on every get_project_db() call; cheap and
    # idempotent. MUST run before the CREATE INDEX statements below because
    # some indexes reference columns added by these migrations (e.g.
    # executions.last_heartbeat_at). Each ALTER is applied independently so a
    # single failure is logged and does not skip the rest.
    for col, defn in [
        ('original_description', 'TEXT'),
        ('gate_state',  "TEXT DEFAULT 'open'"),
        ('gate_reason', 'TEXT'),
        ('gate_source', 'TEXT'),
        ('gate_decided_at', 'TEXT'),
        ('gate_report_json', 'TEXT'),
        ('gate_report_h2_json', 'TEXT'),
        # Hugging Face Scout: the adopted HF repo assigned to this task, and
        # whether it is still awaiting self-hosting before it can run.
        ('hf_repo_id', 'TEXT'),
        ('awaiting_model', "INTEGER DEFAULT 0"),
        # RAG: per-task opt-in + corpus override. The library (factory at
        # /opt/RAG) is shared across projects; these fields let a task ask
        # the library explicitly and pick which corpus to search.
        ('corpus_id', 'TEXT'),
        ('requires_rag', "INTEGER DEFAULT 0"),
        # Lane B: explicit context refs (JSON array of rel_path strings)
        ('context_refs', 'TEXT'),
        # Multi-tenancy (Phase 1): durable DB identity of the creating user.
        # NULL = pre-multi-tenancy row (treated as admin-owned).
        ('created_by', 'INTEGER DEFAULT NULL'),
        # Processing type per task: NULL (normal), 'research', 'deployment'.
        ('execution_type', 'TEXT'),
    ]:
        _add_column_if_missing(conn, 'tasks', col, defn)
    for col, defn in [
        ('aingel_review_json', 'TEXT'),
        ('child_pid', 'INTEGER'),
        ('last_heartbeat_at', 'TEXT'),
        # RAG provenance: how this execution grounded its answer (pre-fetch or
        # tool), which corpus/chunks, and a UI badge label.
        ('rag_provenance_json', 'TEXT'),
        ('rag_label', 'TEXT'),
        # Multi-tenancy (Phase 1): creating user; NULL = pre-multi-tenancy.
        ('created_by', 'INTEGER DEFAULT NULL'),
    ]:
        _add_column_if_missing(conn, 'executions', col, defn)
    # ── Indexes (after migrations so newly-added columns are indexed safely) ───
    try:
        conn.executescript('''
            CREATE INDEX IF NOT EXISTS idx_tasks_project   ON tasks(project_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status    ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_archived  ON tasks(archived);
            CREATE INDEX IF NOT EXISTS idx_tasks_slot      ON tasks(work_session_slot);
            CREATE INDEX IF NOT EXISTS idx_tasks_phase     ON tasks(project_id, phase_name);
            CREATE INDEX IF NOT EXISTS idx_execs_task      ON executions(task_id);
            CREATE INDEX IF NOT EXISTS idx_execs_started   ON executions(started_at);
            CREATE INDEX IF NOT EXISTS idx_execs_chat      ON executions(chat_id);
            CREATE INDEX IF NOT EXISTS idx_execs_heartbeat ON executions(last_heartbeat_at);
            CREATE INDEX IF NOT EXISTS idx_execs_status    ON executions(status);
            CREATE INDEX IF NOT EXISTS idx_chats_project   ON chats(project_id);
            CREATE INDEX IF NOT EXISTS idx_chats_task      ON chats(task_id);
            CREATE INDEX IF NOT EXISTS idx_chats_phase     ON chats(project_id, phase_name);
            CREATE INDEX IF NOT EXISTS idx_skills_project  ON project_skills(project_id);
            CREATE INDEX IF NOT EXISTS idx_perms_project   ON project_permissions(project_id);
         ''')
    except Exception as e:
        _log.warning('project.db: CREATE INDEX failed: %s', e)
    # ── chats.created_by guarded migration (project.db files created before Phase 1) ──
    _add_column_if_missing(conn, 'chats', 'created_by', 'INTEGER DEFAULT NULL')
    # ── file_tags guarded migration (per-project DBs created before this phase) ──
    _add_column_if_missing(conn, 'file_tags', 'tags', "TEXT NOT NULL DEFAULT '[]'")
    _add_column_if_missing(conn, 'file_tags', 'note', "TEXT DEFAULT ''")
    _add_column_if_missing(conn, 'file_tags', 'updated_at', 'TEXT')
    conn.commit()


# ── Delete-tombstone helpers (per-project DB) ────────────────────────────────

def record_delete_tombstone(project_path, rel_path, sha256, deleted_at=None):
    """Record a soft-delete tombstone in the per-project DB.

    Used by the tombstone gate: a task commit that re-adds a file whose
    sha256 matches a tombstone (at any path) is treated as a resurrection and
    re-trashed. deleted_at default: now (UTC, '%Y-%m-%dT%H:%M:%SZ')."""
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return
    if not rel_path or not sha256:
        return
    if not deleted_at:
        deleted_at = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        conn = get_project_db(project_path)
        conn.execute(
            'INSERT OR REPLACE INTO delete_tombstones (rel_path, sha256, deleted_at) VALUES (?, ?, ?)',
            (rel_path, sha256, deleted_at))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_delete_tombstones(project_path):
    """Return list of {rel_path, sha256, deleted_at} for all tombstones.
    Sorted newest-first."""
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return []
    try:
        conn = get_project_db(project_path)
        rows = conn.execute(
            'SELECT rel_path, sha256, deleted_at FROM delete_tombstones '
            'ORDER BY deleted_at DESC').fetchall()
        conn.close()
    except Exception:
        return []
    return [{'rel_path': r['rel_path'], 'sha256': r['sha256'], 'deleted_at': r['deleted_at']}
            for r in rows]


def clear_delete_tombstone(project_path, rel_path=None, sha256=None):
    """Remove tombstone(s). rel_path + sha256: that row only. rel_path only:
    all rows for that rel_path. sha256 only: every row carrying that content
    hash (any path) — used by restore, since the commit gate matches by hash.
    Neither: all rows. Returns count removed."""
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return 0
    try:
        conn = get_project_db(project_path)
        if sha256 and rel_path:
            cur = conn.execute(
                'DELETE FROM delete_tombstones WHERE rel_path=? AND sha256=?',
                (rel_path, sha256))
        elif sha256:
            cur = conn.execute(
                'DELETE FROM delete_tombstones WHERE sha256=?', (sha256,))
        elif rel_path:
            cur = conn.execute(
                'DELETE FROM delete_tombstones WHERE rel_path=? AND sha256 IN '
                '(SELECT sha256 FROM delete_tombstones WHERE rel_path=?)',
                (rel_path, rel_path))
        else:
            cur = conn.execute('DELETE FROM delete_tombstones')
        removed = cur.rowcount or 0
        conn.commit()
        conn.close()
        return removed
    except Exception:
        return 0


def reap_delete_tombstones(project_path, cutoff_ts):
    """Delete tombstones older than cutoff_ts (an ISO '%Y-%m-%dT%H:%M:%SZ'
    string). Used so tombstones expire together with the 7-day trash reaper.
    Returns count removed."""
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return 0
    try:
        conn = get_project_db(project_path)
        cur = conn.execute(
            'DELETE FROM delete_tombstones WHERE deleted_at < ?', (cutoff_ts,))
        removed = cur.rowcount or 0
        conn.commit()
        conn.close()
        return removed
    except Exception:
        return 0


def tombstone_sha_active(project_path, sha256):
    """True if any tombstone row carries this sha256 (used by the commit gate
    to catch resurrected files by content hash even under a new path)."""
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return False
    try:
        conn = get_project_db(project_path)
        row = conn.execute(
            'SELECT 1 FROM delete_tombstones WHERE sha256=? LIMIT 1',
            (sha256,)).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


# ── File tags helpers (per-project DB) ──────────────────────────────────────

def get_file_tags(project_path):
    """Return {rel_path: {tags, note, updated_at}} for all file_tags in project.db."""
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return {}
    try:
        conn = get_project_db(project_path)
        rows = conn.execute('SELECT rel_path, tags, note, updated_at FROM file_tags').fetchall()
        conn.close()
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            tags = json.loads(r['tags']) if r['tags'] else []
        except Exception:
            tags = []
        out[r['rel_path']] = {
            'tags': tags if isinstance(tags, list) else [],
            'note': r['note'] or '',
            'updated_at': r['updated_at'] or '',
        }
    return out


def set_file_tag(project_path, rel_path, tags, note=None):
    """Upsert tags/note for a file. tags is list[str]; note is optional str.
    If tags is empty and note is empty/None, the row is deleted."""
    if not project_path:
        return False
    rel_path = (rel_path or '').strip().replace(os.sep, '/')
    if not rel_path:
        return False
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    note_val = (note or '').strip() if note is not None else None
    # If both empty -> delete
    if not tags and not (note_val or ''):
        return delete_file_tag(project_path, rel_path)
    try:
        tags_json = json.dumps(tags, ensure_ascii=False)
    except Exception:
        tags_json = '[]'
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = get_project_db(project_path)
        # Need to decide note handling: if note is None, keep existing
        if note_val is None:
            # fetch existing note
            row = conn.execute('SELECT note FROM file_tags WHERE rel_path=?', (rel_path,)).fetchone()
            existing_note = row['note'] if row else ''
            note_val = existing_note or ''
        conn.execute(
            'INSERT INTO file_tags (rel_path, tags, note, updated_at) VALUES (?,?,?,?) '
            'ON CONFLICT(rel_path) DO UPDATE SET tags=excluded.tags, note=excluded.note, updated_at=excluded.updated_at',
            (rel_path, tags_json, note_val, now)
        )
        conn.commit()
        conn.close()
        # Block D1: host-local rag-index re-embed on tag change (only that file, local /opt/RAG)
        try:
            if any(str(t).strip().lower() == 'rag-index' for t in (tags or [])):
                import logging as _lg2
                try:
                    import agent_rag as _rag
                    _rag.reindex_file(project_path, rel_path)
                except Exception as _e:
                    _lg2.getLogger(__name__).warning('rag-index reindex after set_file_tag failed for %s: %s', rel_path, _e)
        except Exception:
            pass
        return True
    except Exception:
        return False


def delete_file_tag(project_path, rel_path):
    """Delete file_tags row for rel_path. Returns True if deleted."""
    if not project_path or not rel_path:
        return False
    rel_path = rel_path.strip().replace(os.sep, '/')
    # Capture previous tags so we know if rag-index was present (for cleanup)
    _had_rag = False
    try:
        conn0 = get_project_db(project_path)
        row0 = conn0.execute('SELECT tags FROM file_tags WHERE rel_path=?', (rel_path,)).fetchone()
        conn0.close()
        if row0 and row0['tags']:
            try:
                _prev = json.loads(row0['tags']) if row0['tags'] else []
                _had_rag = any(str(t).strip().lower() == 'rag-index' for t in (_prev or []))
            except Exception:
                _had_rag = False
    except Exception:
        _had_rag = False
    try:
        conn = get_project_db(project_path)
        cur = conn.execute('DELETE FROM file_tags WHERE rel_path=?', (rel_path,))
        conn.commit()
        conn.close()
        # Block D1: cleanup host-local index when rag-index tag removed
        if _had_rag and cur.rowcount:
            try:
                import logging as _lg3
                import agent_rag as _rag2
                # Remove local index file best-effort (not re-embed)
                try:
                    idx_path = _rag2._rag_local_index_path(project_path, rel_path)
                    if os.path.exists(idx_path):
                        os.remove(idx_path)
                        _lg3.getLogger(__name__).info('rag-index local index removed for %s', rel_path)
                except Exception:
                    pass
            except Exception:
                pass
        return cur.rowcount > 0
    except Exception:
        return False


# ── Context refs helpers (per-project DB) ──────────────────────────────────

def _normalize_context_refs(rels):
    """Normalize a list of rel paths: strip, forward-slash, reject abs/traversal.
    Returns deduplicated list preserving order. Does not enforce writable."""
    out = []
    seen = set()
    for r in (rels or []):
        if not isinstance(r, str):
            continue
        s = r.strip().replace(os.sep, '/')
        if not s:
            continue
        if os.path.isabs(s):
            continue
        parts = s.split('/')
        if any(p == '..' for p in parts):
            continue
        # also reject empty parts like // ?
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def get_task_context_refs(task_id, project_path=None):
    """Return list[str] of context_refs for a task, parsed from JSON string."""
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return []
    try:
        pconn = get_project_db(project_path)
        row = pconn.execute('SELECT context_refs FROM tasks WHERE id=?', (task_id,)).fetchone()
        pconn.close()
        if not row:
            return []
        raw = row['context_refs']
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return _normalize_context_refs(parsed)
            return []
        except Exception:
            return []
    except Exception:
        return []


def set_task_context_refs(task_id, project_path, rels):
    """Store context_refs JSON array for a task. Validates via _normalize_context_refs."""
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path:
        return False
    norm = _normalize_context_refs(rels or [])
    try:
        val = json.dumps(norm, ensure_ascii=False) if norm else None
        # Use allowed column via direct SQL to support NULL (clearing)
        pconn = get_project_db(project_path)
        if val is None:
            pconn.execute('UPDATE tasks SET context_refs=NULL, updated_at=? WHERE id=?',
                          (datetime.now(timezone.utc).isoformat(), task_id))
        else:
            pconn.execute('UPDATE tasks SET context_refs=?, updated_at=? WHERE id=?',
                          (val, datetime.now(timezone.utc).isoformat(), task_id))
        pconn.commit()
        pconn.close()
        # Emit task changed for SSE
        proj_id = _resolve_project_id(task_id=task_id, project_path=project_path)
        if proj_id is not None:
            _emit_safe(proj_id, {'type': 'task_changed', 'task_id': task_id})
        return True
    except Exception:
        return False


# ── Registry helpers (aingel.db) ─────────────────────────────────────────────

def _get_task_path(task_id):
    conn = get_db()
    row = conn.execute('SELECT project_path FROM task_registry WHERE id=?', (task_id,)).fetchone()
    conn.close()
    return row['project_path'] if row else None


def _get_exec_reg(exec_id):
    conn = get_db()
    row = conn.execute('SELECT project_id, project_path FROM exec_registry WHERE id=?', (exec_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _get_chat_reg(chat_id):
    conn = get_db()
    row = conn.execute('SELECT project_id, project_path FROM chat_registry WHERE id=?', (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _all_project_paths():
    """Return [(project_id, project_path)] for all projects with a project.db."""
    conn = get_db()
    rows = conn.execute('SELECT id, path FROM projects WHERE path IS NOT NULL').fetchall()
    conn.close()
    return [
        (r['id'], r['path'])
        for r in rows
        if r['path'] and os.path.exists(os.path.join(r['path'], 'project.db'))
    ]


def _projects_map():
    """Return {project_id: {id, name, slug, path}} from central DB."""
    conn = get_db()
    rows = conn.execute('SELECT id, name, slug, path FROM projects').fetchall()
    conn.close()
    return {r['id']: dict(r) for r in rows}


# ── init_db (central DB) ──────────────────────────────────────────────────────

def _rebuild_projects_without_chat_fk(conn):
    """1 → 2 migration: drop the stale FK on projects.aingel_chat_id.

    Rebuilt from the table's *current* definition (sqlite_master keeps the SQL
    up to date across ALTER TABLE ADD COLUMN), so every column — including the
    ones added by the guarded ALTERs that run earlier in init_db (owner_id,
    autopilot, scw_*, rag, …) — survives. Only the REFERENCES clause is removed.
    A no-op (beyond logging) when the table has no foreign key.
    """
    import re
    if not conn.execute('PRAGMA foreign_key_list(projects)').fetchall():
        return
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    create_sql = row[0]
    cols = [r[1] for r in conn.execute('PRAGMA table_info(projects)').fetchall()]
    # Column-level "REFERENCES chats(id) [ON DELETE …]" and table-level
    # "FOREIGN KEY (aingel_chat_id) REFERENCES …" forms.
    new_sql = re.sub(
        r',\s*FOREIGN\s+KEY\s*\([^)]*\)\s*REFERENCES\s+\w+\s*\([^)]*\)'
        r'(\s+ON\s+(DELETE|UPDATE)\s+(SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION))*',
        '', create_sql, flags=re.I)
    new_sql = re.sub(
        r'\s+REFERENCES\s+\w+\s*\([^)]*\)'
        r'(\s+ON\s+(DELETE|UPDATE)\s+(SET\s+NULL|SET\s+DEFAULT|CASCADE|RESTRICT|NO\s+ACTION))*',
        '', new_sql, flags=re.I)
    new_sql = re.sub(r'^\s*CREATE\s+TABLE\s+("?projects"?|\[projects\])',
                     'CREATE TABLE projects_v2', new_sql, count=1, flags=re.I)
    col_list = ', '.join(f'"{c}"' for c in cols)
    # DROP TABLE takes explicit indexes/triggers with it; re-create them after.
    extras = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='projects' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()]
    # PRAGMA foreign_keys is a no-op inside an open transaction, and with FKs on
    # DROP TABLE projects would cascade-delete roles/members. Commit first.
    conn.commit()
    conn.execute('PRAGMA foreign_keys = OFF')
    try:
        conn.execute('BEGIN')
        conn.execute('DROP TABLE IF EXISTS projects_v2')
        conn.execute(new_sql)
        conn.execute(f'INSERT INTO projects_v2 ({col_list}) SELECT {col_list} FROM projects')
        conn.execute('DROP TABLE projects')
        conn.execute('ALTER TABLE projects_v2 RENAME TO projects')
        for sql in extras:
            conn.execute(sql)
        conn.commit()
        _log.warning('init_db: rebuilt projects without FK (%d columns kept)', len(cols))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys = ON')


def get_app_settings():
    """{key: value} of the saved instance-wide settings."""
    conn = get_db()
    try:
        rows = conn.execute('SELECT key, value FROM app_settings').fetchall()
    finally:
        conn.close()
    return {r[0]: r[1] for r in rows}


def set_app_setting(key, value):
    conn = get_db()
    try:
        conn.execute(
            'INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
            (key, None if value is None else str(value)))
        conn.commit()
    finally:
        conn.close()


def init_db():
    conn = get_db()
    # A brand-new DB has no projects table yet. Everything created below is
    # already at the current schema version, so the historical migrations have
    # nothing to do — and the 1 → 2 rebuild would actively harm a fresh DB by
    # renaming a 17-column copy over the full table. Recorded now, applied
    # after the schema exists.
    _fresh_db = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()[0] == 0
    conn.executescript('''
        -- Full current shape, not the historical 7-column one. On an existing DB
        -- this is a no-op (IF NOT EXISTS); on a fresh DB it is the only thing
        -- that creates these columns, since the ALTER TABLE migrations that
        -- used to add them have been retired. The user_version 1 → 2 migration
        -- below SELECTs every column listed here, so the two must stay in sync.
        CREATE TABLE IF NOT EXISTS projects (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            name                 TEXT NOT NULL,
            slug                 TEXT UNIQUE NOT NULL,
            path                 TEXT NOT NULL,
            budget_monthly       REAL DEFAULT 0.0,
            created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
            current_month_spend  REAL DEFAULT 0.0,
            budget_reset_day     INTEGER DEFAULT 1,
            budget_month         INTEGER DEFAULT NULL,
            git_enabled          INTEGER DEFAULT NULL,
            aingel_model         TEXT DEFAULT NULL,
            aingel_chat_id       INTEGER,
            default_model        TEXT DEFAULT "",
            project_type         TEXT DEFAULT "",
            eu_only              INTEGER DEFAULT 0,
            llm_mode             TEXT DEFAULT "standard",
            execution_type       TEXT DEFAULT "standard",
            aingel_name          TEXT DEFAULT NULL,
            aingel_autopilot     INTEGER DEFAULT 0,
            aingel_mode          TEXT DEFAULT "advisory",
            scw_session_enabled  INTEGER DEFAULT 0,
            scw_project_id       TEXT DEFAULT NULL,
            scw_session_bucket   TEXT DEFAULT NULL,
            scw_kms_key_id       TEXT DEFAULT NULL,
            scw_session_region   TEXT DEFAULT "fr-par",
            scw_session_created_at TEXT DEFAULT NULL,
            owner_id             INTEGER DEFAULT NULL
        );
        -- Phase 2 (2026-07-17): tasks/executions/chats/sessions/project_skills/
        -- project_permissions were dropped from aingel.db entirely. They live only in
        -- per-project project.db (see _init_project_db_schema). Do not re-add stubs here —
        -- an empty stub turns a wrong-database query into a silent empty result instead
        -- of a loud "no such table".
        CREATE TABLE IF NOT EXISTS work_sessions (
            slot         INTEGER PRIMARY KEY,
            token_budget INTEGER DEFAULT 150000,
            tokens_used  INTEGER DEFAULT 0,
            status       TEXT DEFAULT 'inactive',
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS roles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id     INTEGER REFERENCES projects(id) ON DELETE CASCADE,
            name           TEXT NOT NULL,
            system_prompt  TEXT DEFAULT '',
            default_model  TEXT DEFAULT '',
            context_scope  TEXT DEFAULT '',
            is_template    INTEGER DEFAULT 0
        );
        -- Instance-wide settings changed at run time (Settings › General);
        -- applied over the env defaults at startup so a restart keeps them.
        CREATE TABLE IF NOT EXISTS app_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS project_type_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            role_names  TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS task_dependencies (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id       INTEGER NOT NULL,
            depends_on_id INTEGER NOT NULL,
            spec_keys     TEXT DEFAULT '[]',
            UNIQUE(task_id, depends_on_id)
        );
        -- Phase 2: global ID registries (task_id/exec_id/chat_id stay globally unique)
        CREATE TABLE IF NOT EXISTS task_registry (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER,
            project_path TEXT
        );
        CREATE TABLE IF NOT EXISTS exec_registry (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER,
            project_path TEXT
        );
        CREATE TABLE IF NOT EXISTS chat_registry (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER,
            project_path TEXT
        );
        CREATE TABLE IF NOT EXISTS scw_deployments (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id           INTEGER REFERENCES projects(id),
            scw_deployment_id    TEXT NOT NULL,
            model_name           TEXT NOT NULL,
            node_type            TEXT NOT NULL,
            endpoint_kind        TEXT DEFAULT 'public',
            endpoint_url         TEXT,
            status               TEXT DEFAULT 'creating',
            provider_status      TEXT,
            hourly_eur           REAL DEFAULT 0.0,
            idle_delete_minutes  INTEGER DEFAULT 30,
            max_context_size     INTEGER,
            accrued_cost_usd     REAL DEFAULT 0.0,
            last_billed_at       TEXT,
            created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
            deleted_at           TEXT,
            is_shared            INTEGER DEFAULT 0
        );
        -- Per-call cost attribution for shared GPU windows: one row per execution
        -- that ran on a deployment, so a shared window's accrued cost can be split
        -- across the projects that actually used it.
        CREATE TABLE IF NOT EXISTS scw_deployment_calls (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            deployment_id  INTEGER NOT NULL REFERENCES scw_deployments(id),
            project_id     INTEGER REFERENCES projects(id),
            task_id        INTEGER,
            exec_id        INTEGER,
            tokens_in      INTEGER DEFAULT 0,
            tokens_out     INTEGER DEFAULT 0,
            duration_s     REAL DEFAULT 0.0,
            cost_usd       REAL DEFAULT 0.0,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS scw_session_costs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id),
            day         TEXT NOT NULL,
            category    TEXT DEFAULT 'general',
            eur         REAL DEFAULT 0.0,
            usd         REAL DEFAULT 0.0,
            UNIQUE(project_id, day, category)
        );
        -- Hugging Face → Scaleway custom model imports (Generative APIs beta).
        -- One row per import attempt; scw_model_id is the library UUID used to
        -- poll status and delete the model when it is no longer needed. status
        -- mirrors Scaleway (preparing|downloading|ready|error) plus 'failed'
        -- when the client gave up polling.
        CREATE TABLE IF NOT EXISTS scw_model_imports (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id     INTEGER REFERENCES projects(id),
            repo_id        TEXT NOT NULL,
            model_name     TEXT NOT NULL,
            scw_model_id   TEXT,
            status         TEXT DEFAULT 'preparing',
            error_message  TEXT,
            size_bytes     INTEGER,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at     TEXT DEFAULT CURRENT_TIMESTAMP
        );
        -- Per-project roster of Hugging Face models adopted for this project
        -- (Phase: Hugging Face Scout). model_id is the resolvable provider id
        -- (e.g. scw-qwen3.6-35b) or '' when the model needs self-hosting.
        CREATE TABLE IF NOT EXISTS project_hf_models (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            repo_id          TEXT NOT NULL,
            model_id         TEXT DEFAULT '',
            label            TEXT DEFAULT '',
            provider         TEXT DEFAULT '',
            validation_score REAL DEFAULT 0.0,
            created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_id, repo_id)
        );
        -- Indexes on the Phase 2 legacy tables (tasks/executions/sessions/chats/
        -- project_skills/project_permissions) were removed with those tables.
        -- Their project.db equivalents live in _init_project_db_schema.
        CREATE INDEX IF NOT EXISTS idx_roles_project   ON roles(project_id);
        CREATE INDEX IF NOT EXISTS idx_deps_task       ON task_dependencies(task_id);
        CREATE INDEX IF NOT EXISTS idx_deps_depends_on ON task_dependencies(depends_on_id);
        CREATE INDEX IF NOT EXISTS idx_task_reg        ON task_registry(project_id);
        CREATE INDEX IF NOT EXISTS idx_exec_reg        ON exec_registry(project_id);
        CREATE INDEX IF NOT EXISTS idx_chat_reg        ON chat_registry(project_id);
        CREATE INDEX IF NOT EXISTS idx_scw_deployments_project ON scw_deployments(project_id);
        CREATE INDEX IF NOT EXISTS idx_scw_model_imports_project ON scw_model_imports(project_id);
        CREATE INDEX IF NOT EXISTS idx_project_hf_models_project ON project_hf_models(project_id);
        CREATE INDEX IF NOT EXISTS idx_projects_path ON projects(path);
        -- Multi-tenancy (Phase 1): users, memberships, quotas, bootstrap sentinel.
        -- All new columns are NULLABLE or DEFAULTed so AINGEL_AUTH=off paths
        -- work untouched. owner_id NULL = pre-multi-tenancy, admin-owned.
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            oidc_sub    TEXT UNIQUE NOT NULL,
            email       TEXT NOT NULL,
            name        TEXT DEFAULT '',
            role        TEXT DEFAULT 'user',
            plan        TEXT DEFAULT 'free',
            status      TEXT DEFAULT 'active',
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS project_members (
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role        TEXT DEFAULT 'member',
            PRIMARY KEY (project_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS usage_counters (
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            month         TEXT NOT NULL,
            runs          INTEGER DEFAULT 0,
            storage_bytes INTEGER DEFAULT 0,
            tokens_in     INTEGER DEFAULT 0,
            tokens_out    INTEGER DEFAULT 0,
            gpu_minutes   INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, month)
        );
        CREATE TABLE IF NOT EXISTS app_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);
        CREATE INDEX IF NOT EXISTS idx_usage_counters_month ON usage_counters(month);
    ''')

    # ── Project column migrations ─────────────────────────────────────────────
    proj_cols = {r['name'] for r in conn.execute('PRAGMA table_info(projects)').fetchall()}
    if 'current_month_spend' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN current_month_spend REAL DEFAULT 0.0')
    if 'budget_reset_day' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN budget_reset_day INTEGER DEFAULT 1')
    if 'budget_month' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN budget_month INTEGER DEFAULT NULL')
    if 'git_enabled' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN git_enabled INTEGER DEFAULT NULL')
    if 'project_type' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN project_type TEXT DEFAULT ""')
    if 'eu_only' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN eu_only INTEGER DEFAULT 0')
    if 'llm_mode' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN llm_mode TEXT DEFAULT "standard"')
    if 'execution_type' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN execution_type TEXT DEFAULT "standard"')
    if 'aingel_name' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN aingel_name TEXT DEFAULT NULL')
    # ── Hugging Face Scout: per-project default model (single default drawn
    # ── from the project_hf_models roster; stores the resolvable model_id). ──
    if 'default_model' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN default_model TEXT DEFAULT ""')
    # ── Phase 7: AIngel Autopilot ──────────────────────────────────────────────
    # Per-project toggle + advisory/strict binding mode for H2/H3 gate decisions.
    if 'aingel_autopilot' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN aingel_autopilot INTEGER DEFAULT 0')
    if 'aingel_mode' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN aingel_mode TEXT DEFAULT "advisory"')
    # RAG library: per-project availability + default corpus (the shared factory
    # at /opt/RAG is opt-in per project, not enabled for every project).
    if 'use_rag' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN use_rag INTEGER DEFAULT 0')
    if 'rag_corpus_id' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN rag_corpus_id TEXT DEFAULT "railway"')
    if 'scw_session_enabled' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN scw_session_enabled INTEGER DEFAULT 0')
    if 'scw_project_id' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN scw_project_id TEXT DEFAULT NULL')
    if 'scw_session_bucket' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN scw_session_bucket TEXT DEFAULT NULL')
    if 'scw_kms_key_id' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN scw_kms_key_id TEXT DEFAULT NULL')
    if 'scw_session_region' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN scw_session_region TEXT DEFAULT "fr-par"')
    if 'scw_session_created_at' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN scw_session_created_at TEXT DEFAULT NULL')
    # Multi-tenancy (Phase 1): durable owner identity. NULL = pre-multi-tenancy,
    # treated as admin-owned; AINGEL_AUTH=off behavior unchanged.
    if 'owner_id' not in proj_cols:
        conn.execute('ALTER TABLE projects ADD COLUMN owner_id INTEGER DEFAULT NULL')

    # ── Shared GPU window (cross-project run window) ────────────────────────────
    # scw_deployments.project_id becomes nullable (a shared window is not owned by
    # a single project) and gains an is_shared flag. SQLite cannot drop the NOT NULL
    # constraint in place, so we rebuild the table when the old shape is detected.
    dep_cols = {r['name'] for r in conn.execute('PRAGMA table_info(scw_deployments)').fetchall()}
    if 'is_shared' not in dep_cols:
        conn.execute('ALTER TABLE scw_deployments ADD COLUMN is_shared INTEGER DEFAULT 0')
    if 'project_id' in dep_cols:
        dep_pk = conn.execute('PRAGMA table_info(scw_deployments)').fetchall()
        pid_col = next((c for c in dep_pk if c['name'] == 'project_id'), None)
        if pid_col and pid_col['notnull']:
            conn.executescript('''
                ALTER TABLE scw_deployments RENAME TO scw_deployments_old;
                CREATE TABLE scw_deployments (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id           INTEGER REFERENCES projects(id),
                    scw_deployment_id    TEXT NOT NULL,
                    model_name           TEXT NOT NULL,
                    node_type            TEXT NOT NULL,
                    endpoint_kind        TEXT DEFAULT 'public',
                    endpoint_url         TEXT,
                    status               TEXT DEFAULT 'creating',
                    provider_status      TEXT,
                    hourly_eur           REAL DEFAULT 0.0,
                    idle_delete_minutes  INTEGER DEFAULT 30,
                    max_context_size     INTEGER,
                    accrued_cost_usd     REAL DEFAULT 0.0,
                    last_billed_at       TEXT,
                    created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
                    deleted_at           TEXT,
                    is_shared            INTEGER DEFAULT 0
                );
                INSERT INTO scw_deployments
                    (id, project_id, scw_deployment_id, model_name, node_type,
                     endpoint_kind, endpoint_url, status, hourly_eur,
                     idle_delete_minutes, accrued_cost_usd, last_billed_at,
                     created_at, deleted_at, is_shared)
                SELECT id, project_id, scw_deployment_id, model_name, node_type,
                       endpoint_kind, endpoint_url, status, hourly_eur,
                       idle_delete_minutes, accrued_cost_usd, last_billed_at,
                       created_at, deleted_at, 0
                FROM scw_deployments_old;
                DROP TABLE scw_deployments_old;
            ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_scw_deployment_calls_dep ON scw_deployment_calls(deployment_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_scw_deployment_calls_project ON scw_deployment_calls(project_id)')

    # ── Repair dangling FK from the scw_deployments rebuild ───────────────────
    # The 1 → 2 rebuild above renames scw_deployments → scw_deployments_old and
    # SQLite auto-rewrites scw_deployment_calls.deployment_id's FK to point at
    # the new name, then drops scw_deployments_old — leaving a dangling FK to a
    # non-existent table. Any FK-triggering operation on scw_deployments then
    # fails with "no such table: main.scw_deployments_old". Detect and rebuild
    # the calls table with the correct FK when that happens.
    _calls_fks = {r['table'] for r in conn.execute('PRAGMA foreign_key_list(scw_deployment_calls)').fetchall()}
    if 'scw_deployments_old' in _calls_fks:
        conn.executescript('''
            PRAGMA foreign_keys = OFF;
            ALTER TABLE scw_deployment_calls RENAME TO scw_deployment_calls_old;
            CREATE TABLE scw_deployment_calls (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                deployment_id  INTEGER NOT NULL REFERENCES scw_deployments(id),
                project_id     INTEGER REFERENCES projects(id),
                task_id        INTEGER,
                exec_id        INTEGER,
                tokens_in      INTEGER DEFAULT 0,
                tokens_out     INTEGER DEFAULT 0,
                duration_s     REAL DEFAULT 0.0,
                cost_usd       REAL DEFAULT 0.0,
                created_at     TEXT DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO scw_deployment_calls
                (id, deployment_id, project_id, task_id, exec_id, tokens_in,
                 tokens_out, duration_s, cost_usd, created_at)
            SELECT id, deployment_id, project_id, task_id, exec_id, tokens_in,
                   tokens_out, duration_s, cost_usd, created_at
            FROM scw_deployment_calls_old;
            DROP TABLE scw_deployment_calls_old;
            PRAGMA foreign_keys = ON;
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_scw_deployment_calls_dep ON scw_deployment_calls(deployment_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_scw_deployment_calls_project ON scw_deployment_calls(project_id)')

    # Custom models have a real context window (e.g. 4096) that must override the
    # generic 128k default so prompt/chunk sizing doesn't overflow the endpoint.
    dep_cols2 = {r['name'] for r in conn.execute('PRAGMA table_info(scw_deployments)').fetchall()}
    if 'max_context_size' not in dep_cols2:
        conn.execute('ALTER TABLE scw_deployments ADD COLUMN max_context_size INTEGER DEFAULT NULL')
    if 'provider_status' not in dep_cols2:
        conn.execute('ALTER TABLE scw_deployments ADD COLUMN provider_status TEXT')

    # ── Legacy task/execution/chat column migrations — RETIRED ────────────────
    # These ALTER TABLE / CREATE INDEX migrations targeted the legacy tasks,
    # executions, chats and sessions tables in aingel.db, dropped in Phase 2.
    # The equivalent per-project migrations live in _init_project_db_schema().

    # ── Work session columns ──────────────────────────────────────────────────
    ws_cols = {r['name'] for r in conn.execute('PRAGMA table_info(work_sessions)').fetchall()}
    for col, defn in [
        ('started_at', 'TEXT DEFAULT NULL'),
        ('ends_at', 'TEXT DEFAULT NULL'),
        ('next_run_at', 'TEXT DEFAULT NULL'),
        ('pause_reason', 'TEXT DEFAULT NULL'),
    ]:
        if col not in ws_cols:
            conn.execute(f'ALTER TABLE work_sessions ADD COLUMN {col} {defn}')

    # ── Work sessions initialization ──────────────────────────────────────────
    if conn.execute('SELECT COUNT(*) FROM work_sessions').fetchone()[0] == 0:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        for slot, hrs, status in [(1, 3, 'active'), (2, 8, 'active'), (3, 13, 'active'),
                                  (4, 18, 'inactive'), (5, 23, 'inactive')]:
            conn.execute(
                'INSERT INTO work_sessions (slot, token_budget, tokens_used, status, created_at) VALUES (?,150000,0,?,?)',
                (slot, status, (now - timedelta(hours=hrs)).isoformat())
            )

    # Ensure the Ollama Cloud slot-5 work session exists on already-seeded DBs
    # (the seed above only runs on a fresh DB). Idempotent.
    if conn.execute('SELECT COUNT(*) FROM work_sessions WHERE slot=5').fetchone()[0] == 0:
        from datetime import timedelta as _timedelta
        _now = datetime.now(timezone.utc)
        conn.execute(
            'INSERT INTO work_sessions (slot, token_budget, tokens_used, status, created_at) VALUES (5,150000,0,?,?)',
            ('inactive', (_now - _timedelta(hours=23)).isoformat())
        )

    # ── Kanban migration (user_version 0 → 1) — RETIRED ───────────────────────
    # Operated on the legacy aingel.db.tasks table, dropped in Phase 2. Every live
    # DB is already at user_version >= 2, so this never ran again; removed with the
    # table it depended on. The 1 → 2 migration below is self-contained.
    if _fresh_db:
        # Schema above is current by construction — stamp it so the migrations
        # below are skipped rather than run against a table that never had the
        # old shape.
        conn.execute('PRAGMA user_version = 2')
        conn.commit()

    user_version = conn.execute('PRAGMA user_version').fetchone()[0]

    # ── Drop stale FK on projects.aingel_chat_id (user_version 1 → 2) ─────────
    # aingel_chat_id previously referenced the legacy aingel.db.chats table which
    # no longer holds new chats (Phase 2 moved chats to project.db). The FK must
    # be removed or every _init_aingel call fails with a constraint error.
    if user_version < 2:
        _rebuild_projects_without_chat_fk(conn)
        conn.execute('PRAGMA user_version = 2')
        conn.commit()

    # ── Seed global roles ─────────────────────────────────────────────────────
    if conn.execute('SELECT COUNT(*) FROM roles WHERE project_id IS NULL').fetchone()[0] == 0:
        _SEED_ROLES = [
            ('Polish Legal Specialist',
             'You are a Polish legal expert specializing in the Civil Code, Commercial Code, '
             'and Labor Code. You handle KRS filings, corporate documentation, contracts, '
             'and regulatory compliance. Cite specific articles and relevant case law.\n\n'
             'LEGAL RAG RULE: every statute/regulation answer MUST draw on the '
             '"Retrieved legal context (RAG)" block when it is present in the prompt. '
             'Cite Dz.U. + ELI + CELEX verbatim extracts. If the answer is not in the '
             'retrieved context, say "Brak w dostarczonych aktach" — never answer statutes '
             'from memory or invent Dz.U./CELEX references.',
             'claude-opus-4-7', 'project'),
            ('Business Analyst',
             'You are a business analyst expert in market sizing, financial modeling, '
             'feasibility analysis, and competitive landscape research. Produce structured, '
             'data-backed reports with clear assumptions and sensitivity analysis.',
             'claude-sonnet-5-5', 'project'),
            ('Backend Engineer',
             'You are a senior backend engineer specializing in Python, Flask, async design, '
             'REST API architecture, SQLite/PostgreSQL, and performance optimization. '
             'Write clean, secure, well-tested code following existing project conventions.',
             'claude-sonnet-5-5', 'phase'),
            ('Audio/DSP Engineer',
             'You are a digital signal processing engineer specializing in audio processing, '
             'phase inversion, noise reduction, mobile audio pipelines, and codec optimization. '
             'Reason precisely about frequency domains, sample rates, and latency budgets.',
             'claude-opus-4-7', 'project'),
            ('3D/Mechanical Engineer',
             'You are a mechanical and 3D design engineer with expertise in CAD, geometric '
             'modeling, weight calculations, material selection, and manufacturing constraints. '
             'Produce detailed specifications and feasibility assessments.',
             'claude-opus-4-7', 'project'),
            ('Market Researcher',
             'You are a market research specialist focused on competitive intelligence, '
             'sector analysis, consumer behavior, pricing dynamics, and strategic positioning. '
             'Synthesize multiple sources into actionable, well-structured insights.',
             'mistral-large-latest', 'project'),
            ('Documentation Writer',
             'You are a technical writer specializing in API references, compliance documents, '
             'user guides, and standard operating procedures. Write clear, precise, '
             'well-organized content following established style conventions.',
             'mistral-large-latest', 'phase'),
            ('DevOps/Sysadmin',
             'You are a Linux systems engineer specializing in systemd services, LXC containers, '
             'network configuration, backup strategies, and infrastructure automation. '
             'Write reliable, idempotent shell scripts and configurations.',
             'claude-sonnet-5-5', 'phase'),
        ]
        conn.executemany(
            'INSERT INTO roles (name, system_prompt, default_model, context_scope, is_template, project_id) '
            'VALUES (?,?,?,?,1,NULL)',
            _SEED_ROLES
        )

    # ── RAG role migration (guarded) ──────────────────────────────────────────
    # Upgrade the legacy 'Polish Legal Specialist' role prompt with the RAG rule.
    # Idempotent and non-destructive: only rows that STILL hold the exact old
    # default text are updated, so a user-customised role is never overwritten.
    _LEGAL_OLD = (
        'You are a Polish legal expert specializing in the Civil Code, Commercial Code, '
        'and Labor Code. You handle KRS filings, corporate documentation, contracts, '
        'and regulatory compliance. Cite specific articles and relevant case law.'
    )
    _LEGAL_NEW = (
        _LEGAL_OLD + '\n\n'
        'LEGAL RAG RULE: every statute/regulation answer MUST draw on the '
        '"Retrieved legal context (RAG)" block when it is present in the prompt. '
        'Cite Dz.U. + ELI + CELEX verbatim extracts. If the answer is not in the '
        'retrieved context, say "Brak w dostarczonych aktach" — never answer statutes '
        'from memory or invent Dz.U./CELEX references.'
    )
    conn.execute(
        "UPDATE roles SET system_prompt=? WHERE name='Polish Legal Specialist' AND system_prompt=?",
        (_LEGAL_NEW, _LEGAL_OLD)
    )

    # ── Seed project-type templates ───────────────────────────────────────────
    import json as _json
    if conn.execute('SELECT COUNT(*) FROM project_type_templates').fetchone()[0] == 0:
        _SEED_TEMPLATES = [
            ('Software', 'Software / web application project',
             _json.dumps(['Backend Engineer', 'DevOps/Sysadmin', 'Documentation Writer'])),
            ('Business Plan', 'Business planning and market research project',
             _json.dumps(['Business Analyst', 'Market Researcher', 'Documentation Writer'])),
            ('Hardware', '3D printing / mechanical engineering project',
             _json.dumps(['3D/Mechanical Engineer', 'Documentation Writer'])),
            ('Research', 'Research or analysis project',
             _json.dumps(['Market Researcher', 'Documentation Writer'])),
            ('Financial', 'Financial analysis and planning project',
             _json.dumps(['Business Analyst', 'Documentation Writer'])),
            ('Legal', 'Legal analysis and compliance project',
             _json.dumps(['Polish Legal Specialist', 'Documentation Writer'])),
        ]
        conn.executemany(
            'INSERT INTO project_type_templates (name, description, role_names) VALUES (?,?,?)',
            _SEED_TEMPLATES
        )

    # ── Empty phase_name → 'Notebook' migration ───────────────────────────────
    # Replaces the legacy '' sentinel with 'Notebook' as a real phase name so
    # the React Sidebar's phase filter doesn't compare display label '(no phase)'
    # against DB value '' (which made unphased tasks "unreachable" in the board).
    # Guarded by user_version < 3 so it runs exactly once.
    user_version = conn.execute('PRAGMA user_version').fetchone()[0]
    if user_version < 3:
        _migrate_empty_phase_to_notebook()
        conn.execute('PRAGMA user_version = 3')

    conn.commit()
    conn.close()


def _migrate_empty_phase_to_notebook():
    """Backfill empty phase_name → 'Notebook' across every project's tasks/chats.

    Idempotent: WHERE phase_name = '' makes subsequent runs a no-op.
    """
    conn = get_db()
    projects = conn.execute('SELECT path FROM projects').fetchall()
    conn.close()
    for row in projects:
        ppath = row['path']
        if not ppath or not os.path.exists(os.path.join(ppath, 'project.db')):
            continue
        try:
            pconn = get_project_db(ppath)
            tasks_n = pconn.execute(
                "UPDATE tasks SET phase_name='Notebook' WHERE phase_name='' OR phase_name IS NULL"
            ).rowcount
            chats_n = pconn.execute(
                "UPDATE chats SET phase_name='Notebook' WHERE phase_name='' OR phase_name IS NULL"
            ).rowcount
            if tasks_n or chats_n:
                pconn.commit()
                import logging
                logging.getLogger(__name__).info(
                    'phase migration: %s — %d tasks, %d chats → Notebook',
                    ppath, tasks_n, chats_n,
                )
            pconn.close()
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                'phase migration failed for %s: %s', ppath, exc,
            )


# ── Orphan sweep (runs at startup) ───────────────────────────────────────────

def touch_execution_heartbeat(exec_id, project_path=None):
    """Update last_heartbeat_at for a running execution. Safe to call from any thread."""
    reg = None
    if project_path:
        path = project_path
    else:
        reg = _get_exec_reg(exec_id)
        path = reg['project_path'] if reg else None
    if not path:
        return False
    try:
        pconn = get_project_db(path)
        cur = pconn.execute(
            "UPDATE executions SET last_heartbeat_at=datetime('now') WHERE id=? AND status='running'",
            (exec_id,))
        pconn.commit()
        pconn.close()
        return cur.rowcount > 0
    except Exception:
        return False


def _older_than_minutes(ts, minutes):
    """True when timestamp ``ts`` is more than ``minutes`` old (or unparseable/missing).

    Accepts both ISO-8601 (``2026-09-17T10:00:00+00:00``) and SQLite
    ``CURRENT_TIMESTAMP`` (``2026-09-17 10:00:00``, UTC) formats.
    """
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(str(ts).replace(' ', 'T', 1))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() > minutes * 60


def reset_orphaned_executions(reason='tick'):
    """Mark stale running executions as failed across all project DBs.

    Called at startup (agent_api.py, reason='startup') and periodically from
    the slot-scheduler tick (~every 5 min, reason='tick'). Staleness is
    determined by last_heartbeat_at (or started_at for legacy rows) — no
    heartbeat for 10 min means the worker thread is dead. A 30s watchdog
    touches the heartbeat for every live execution, so healthy batches survive
    indefinitely regardless of model. The old scw-dep-% exemption is removed."""
    now = datetime.now(timezone.utc).isoformat()
    if reason == 'startup':
        err_msg = 'Orphaned: service restarted while execution was running'
    else:
        err_msg = 'Orphaned: no heartbeat for 10 min'
    total = 0
    for _proj_id, proj_path in _all_project_paths():
        try:
            pconn = get_project_db(proj_path)
            orphans = pconn.execute(
                "SELECT id, task_id FROM executions WHERE status='running' "
                "AND COALESCE(last_heartbeat_at, started_at) < datetime('now', '-10 minutes')"
            ).fetchall()
            for row in orphans:
                pconn.execute(
                    "UPDATE executions SET status='failed', "
                    "error_message=?, finished_at=? WHERE id=?",
                    (err_msg, now, row['id'])
                )
                if row['task_id']:
                    pconn.execute(
                        "UPDATE tasks SET status='pending', updated_at=? WHERE id=? AND status='running'",
                        (now, row['task_id'])
                    )
            # Stranded tasks: 'running' with no running execution at all (e.g. the
            # process died between claim_task and create_execution). run_task
            # refuses 'running' tasks, so without this they could never run again.
            stranded = pconn.execute(
                "SELECT id, updated_at FROM tasks t WHERE status='running' "
                "AND NOT EXISTS (SELECT 1 FROM executions e "
                "WHERE e.task_id = t.id AND e.status='running')"
            ).fetchall()
            for row in stranded:
                if _older_than_minutes(row['updated_at'], 10):
                    pconn.execute(
                        "UPDATE tasks SET status='pending', updated_at=? WHERE id=? AND status='running'",
                        (now, row['id'])
                    )
                    total += 1
            pconn.commit()
            pconn.close()
            total += len(orphans)
        except Exception:
            pass
    return total


# ── Projects (central DB) ─────────────────────────────────────────────────────

def upsert_project(name, slug, path, project_type='', eu_only=0, llm_mode='standard',
                   execution_type='standard', aingel_name=None,
                   scw_session_enabled=0, scw_project_id=None, scw_session_bucket=None,
                   scw_kms_key_id=None, scw_session_region='fr-par', scw_session_created_at=None):
    conn = get_db()
    conn.execute(
        'INSERT INTO projects (name, slug, path, project_type, eu_only, llm_mode, execution_type, aingel_name, '
        'scw_session_enabled, scw_project_id, scw_session_bucket, scw_kms_key_id, scw_session_region, scw_session_created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(slug) DO UPDATE SET name=excluded.name, path=excluded.path, '
        'project_type=excluded.project_type, eu_only=excluded.eu_only, llm_mode=excluded.llm_mode, '
        'execution_type=excluded.execution_type, aingel_name=excluded.aingel_name, '
        'scw_session_enabled=excluded.scw_session_enabled, scw_project_id=excluded.scw_project_id, '
        'scw_session_bucket=excluded.scw_session_bucket, scw_kms_key_id=excluded.scw_kms_key_id, '
        'scw_session_region=excluded.scw_session_region, scw_session_created_at=excluded.scw_session_created_at',
        (name, slug, path, project_type, eu_only, llm_mode, execution_type, aingel_name,
         scw_session_enabled, scw_project_id, scw_session_bucket, scw_kms_key_id,
         scw_session_region, scw_session_created_at)
    )
    conn.commit()
    row = conn.execute('SELECT * FROM projects WHERE slug=?', (slug,)).fetchone()
    conn.close()
    return dict(row)


def get_projects():
    conn = get_db()
    rows = conn.execute('SELECT * FROM projects ORDER BY name').fetchall()
    conn.close()
    projects = []
    for r in rows:
        p = dict(r)
        proj_path = p.get('path', '')
        if proj_path and os.path.exists(os.path.join(proj_path, 'project.db')):
            try:
                pconn = get_project_db(proj_path)
                stats = pconn.execute('''
                    SELECT COUNT(*) total,
                           SUM(status="pending")   pending,
                           SUM(status="confirmed") confirmed,
                           SUM(status="running")   running,
                           SUM(status="done")      done,
                           COALESCE(SUM(actual_cost),0) spent
                    FROM tasks WHERE project_id=?
                ''', (p['id'],)).fetchone()
                pconn.close()
                if stats:
                    p.update(dict(stats))
            except Exception:
                pass
        else:
            p.update({'total': 0, 'pending': 0, 'confirmed': 0, 'running': 0, 'done': 0, 'spent': 0})
        projects.append(p)
    return projects


def update_project_budget(project_id, monthly_budget=None, reset_day=None):
    conn = get_db()
    fields, params = [], []
    if monthly_budget is not None:
        fields.append('budget_monthly=?')
        params.append(float(monthly_budget) if monthly_budget else 0.0)
    if reset_day is not None:
        fields.append('budget_reset_day=?')
        params.append(max(1, min(28, int(reset_day))))
    if fields:
        params.append(project_id)
        conn.execute(f'UPDATE projects SET {", ".join(fields)} WHERE id=?', params)
        conn.commit()
    conn.close()


def get_project_by_path(path):
    conn = get_db()
    row = conn.execute('SELECT * FROM projects WHERE path=?', (path,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_project(project_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


_PROJECT_FIELD_WHITELIST = frozenset({
    "name", "slug", "path", "project_type", "eu_only", "llm_mode",
    "execution_type", "aingel_name", "aingel_model", "aingel_chat_id",
    "git_enabled", "budget_monthly", "budget_reset_day",
    "aingel_autopilot", "aingel_mode", "default_model",
    "scw_session_enabled", "scw_project_id", "scw_session_bucket",
    "scw_kms_key_id", "scw_session_region", "scw_session_created_at",
    "use_rag", "rag_corpus_id",
})


def set_project_field(project_id, field, value):
    if field not in _PROJECT_FIELD_WHITELIST:
        raise ValueError(f"invalid project field: {field!r}")
    conn = get_db()
    conn.execute(f'UPDATE projects SET "{field}"=? WHERE id=?', (value, project_id))
    conn.commit()
    conn.close()


def set_project_git_enabled(project_id, value):
    conn = get_db()
    conn.execute('UPDATE projects SET git_enabled=? WHERE id=?', (value, project_id))
    conn.commit()
    conn.close()


# ── Multi-tenancy (Phase 1): users, memberships, quotas ─────────────────────
# All functions open their own central-DB connection via get_db() (the file's
# existing convention) unless an open connection is passed as `conn`, in which
# case the caller owns commit/close. New columns are NULLABLE/DEFAULTed so
# AINGEL_AUTH=off paths work untouched; NULL owner_id / created_by means
# pre-multi-tenancy, treated as admin-owned.

_USER_ROLES = ('user', 'admin')
_USER_PLANS = ('free', 'paid')
_USER_STATUSES = ('active', 'suspended')
_MEMBER_ROLES = ('owner', 'admin', 'member', 'viewer')
_USAGE_FIELDS = ('runs', 'storage_bytes', 'tokens_in', 'tokens_out', 'gpu_minutes')


def ensure_admin_bootstrapped(conn, user_id):
    """Atomically grant admin to user_id if they are the first-ever user.

    Uses BEGIN IMMEDIATE + the app_meta.admin_bootstrapped sentinel so two
    concurrent first logins elect exactly one admin. Tolerates being called
    inside an existing transaction (the caller's write already holds the lock).
    Returns True if this call granted admin, False otherwise. Caller commits.
    """
    try:
        conn.execute('BEGIN IMMEDIATE')
    except Exception:
        pass  # already inside a transaction — our write lock is held anyway
    conn.execute(
        "INSERT OR IGNORE INTO app_meta (key, value) VALUES ('admin_bootstrapped','0')")
    cur = conn.execute(
        "UPDATE app_meta SET value='1' WHERE key='admin_bootstrapped' AND value='0'")
    if cur.rowcount == 1:
        conn.execute('UPDATE users SET role=? WHERE id=?', ('admin', user_id))
        return True
    return False


def get_or_create_user(oidc_sub, email, name='', conn=None):
    """Provision a users row on OIDC login. First-ever user becomes admin
    (atomic via ensure_admin_bootstrapped). Returns the user dict."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        try:
            conn.execute('BEGIN IMMEDIATE')
        except Exception:
            pass
        conn.execute(
            'INSERT INTO users (oidc_sub, email, name) VALUES (?,?,?) '
            'ON CONFLICT(oidc_sub) DO NOTHING',
            (oidc_sub, email, name or ''))
        row = conn.execute('SELECT * FROM users WHERE oidc_sub=?', (oidc_sub,)).fetchone()
        user = dict(row)
        # First-ever user (sentinel still unset and this row is the only one)
        # is promoted to admin atomically.
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1:
            if ensure_admin_bootstrapped(conn, user['id']):
                user['role'] = 'admin'
        if own:
            conn.commit()
        return user
    except Exception:
        if own:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if own:
            conn.close()


def get_user(user_id, conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def get_user_by_oidc_sub(sub, conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute('SELECT * FROM users WHERE oidc_sub=?', (sub,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def list_users(conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        rows = conn.execute('SELECT * FROM users ORDER BY id').fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def update_user_plan(user_id, plan, conn=None):
    if plan not in _USER_PLANS:
        raise ValueError(f"invalid plan: {plan!r} (expected one of {_USER_PLANS})")
    own = conn is None
    if own:
        conn = get_db()
    try:
        conn.execute('UPDATE users SET plan=? WHERE id=?', (plan, user_id))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def update_user_status(user_id, status, conn=None):
    if status not in _USER_STATUSES:
        raise ValueError(f"invalid status: {status!r} (expected one of {_USER_STATUSES})")
    own = conn is None
    if own:
        conn = get_db()
    try:
        conn.execute('UPDATE users SET status=? WHERE id=?', (status, user_id))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def delete_user(user_id, conn=None):
    """Delete a user, their memberships and usage counters.

    Projects they owned are left ownerless (``owner_id=NULL``) rather than
    deleted — the project and its data stay. Task/execution/chat ``created_by``
    references are historical and left untouched. Returns the number of
    projects that lost an owner; None when the user did not exist.

    Raises ValueError('last_admin') if this is the only remaining admin, so a
    delete can never lock every operator out.
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute('SELECT role FROM users WHERE id=?', (user_id,)).fetchone()
        if row is None:
            return None
        if row['role'] == 'admin':
            others = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND id<>?",
                (user_id,)).fetchone()[0]
            if others == 0:
                raise ValueError('last_admin')
        cur = conn.execute('UPDATE projects SET owner_id=NULL WHERE owner_id=?', (user_id,))
        owned = cur.rowcount
        conn.execute('DELETE FROM project_members WHERE user_id=?', (user_id,))
        conn.execute('DELETE FROM usage_counters WHERE user_id=?', (user_id,))
        conn.execute('DELETE FROM users WHERE id=?', (user_id,))
        if own:
            conn.commit()
        return int(owned or 0)
    finally:
        if own:
            conn.close()


def set_project_owner(project_id, user_id, conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        conn.execute('UPDATE projects SET owner_id=? WHERE id=?', (user_id, project_id))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def add_project_member(project_id, user_id, role='member', conn=None):
    if role not in _MEMBER_ROLES:
        raise ValueError(f"invalid member role: {role!r} (expected one of {_MEMBER_ROLES})")
    own = conn is None
    if own:
        conn = get_db()
    try:
        conn.execute(
            'INSERT INTO project_members (project_id, user_id, role) VALUES (?,?,?) '
            'ON CONFLICT(project_id, user_id) DO UPDATE SET role=excluded.role',
            (project_id, user_id, role))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def set_project_owner(project_id, user_id, conn=None):
    """Update projects.owner_id — the single canonical owner used by quota
    resolution, storage stock and ownership display.

    Membership roles are the access-control truth (project_members.role),
    but projects.owner_id must follow ownership transfers or the old owner
    keeps being charged for runs/tokens/storage of a project they no
    longer own (user report 2026-10-02: a project transferred in the
    Members modal, owner_id stayed on the previous owner).
    """
    own = conn is None
    if own:
        conn = get_db()
    try:
        conn.execute('UPDATE projects SET owner_id=? WHERE id=?',
                     (user_id, project_id))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def remove_project_member(project_id, user_id, conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        cur = conn.execute(
            'DELETE FROM project_members WHERE project_id=? AND user_id=?',
            (project_id, user_id))
        if own:
            conn.commit()
        return cur.rowcount > 0
    finally:
        if own:
            conn.close()


def get_project_members(project_id, conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        rows = conn.execute(
            'SELECT m.project_id, m.user_id, m.role, u.email, u.name '
            'FROM project_members m JOIN users u ON u.id = m.user_id '
            'WHERE m.project_id=? ORDER BY u.id',
            (project_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def get_user_projects(user_id, conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        rows = conn.execute(
            'SELECT p.* FROM projects p '
            'JOIN project_members m ON m.project_id = p.id '
            'WHERE m.user_id=? ORDER BY p.name',
            (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def get_user_role(project_id, user_id, conn=None):
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute(
            'SELECT role FROM project_members WHERE project_id=? AND user_id=?',
            (project_id, user_id)).fetchone()
        return row['role'] if row else None
    finally:
        if own:
            conn.close()


def get_usage(user_id, month, conn=None):
    """Return all counters for (user_id, month); zeros when no row exists."""
    own = conn is None
    if own:
        conn = get_db()
    try:
        row = conn.execute(
            'SELECT * FROM usage_counters WHERE user_id=? AND month=?',
            (user_id, month)).fetchone()
        if row:
            return dict(row)
        return {'user_id': user_id, 'month': month, 'runs': 0,
                'storage_bytes': 0, 'tokens_in': 0, 'tokens_out': 0,
                'gpu_minutes': 0}
    finally:
        if own:
            conn.close()


def check_and_increment_usage(user_id, month, field, amount, limit, conn=None):
    """Atomically increment `field` by `amount` iff the new total stays within
    `limit`. Auto-inserts the month row if missing. Returns True when the
    increment was applied, False when over limit (no increment)."""
    if field not in _USAGE_FIELDS:
        raise ValueError(f"invalid usage field: {field!r} (expected one of {_USAGE_FIELDS})")
    amount = int(amount)
    own = conn is None
    if own:
        conn = get_db()
    try:
        try:
            conn.execute('BEGIN IMMEDIATE')
        except Exception:
            pass
        conn.execute(
            'INSERT OR IGNORE INTO usage_counters (user_id, month) VALUES (?,?)',
            (user_id, month))
        cur = conn.execute(
            f'UPDATE usage_counters SET "{field}"="{field}"+? '
            f'WHERE user_id=? AND month=? AND "{field}"+?<=?',
            (amount, user_id, month, amount, limit))
        ok = cur.rowcount == 1
        if own:
            if ok:
                conn.commit()
            else:
                try:
                    conn.rollback()
                except Exception:
                    pass
        return ok
    except Exception:
        if own:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if own:
            conn.close()


def get_project_hf_models(project_id):
    """Return the project's adopted Hugging Face model roster (oldest first)."""
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM project_hf_models WHERE project_id=? ORDER BY id ASC',
        (project_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_hf_models():
    """All adopted Hugging Face models across projects, deduped by repo_id.

    Selecting an already-imported HF model should not require re-adopting it in
    every project, so the task model picker and counselor draw from this global
    roster rather than the per-project one."""
    conn = get_db()
    rows = conn.execute('SELECT * FROM project_hf_models ORDER BY id ASC').fetchall()
    conn.close()
    seen = {}
    for r in rows:
        r = dict(r)
        seen.setdefault(r['repo_id'], r)
    return list(seen.values())


def add_project_hf_model(project_id, repo_id, model_id='', label='',
                         provider='', validation_score=0.0):
    """Insert or refresh a roster entry. `model_id` is the resolvable provider
    id, or '' when the model needs self-hosting (no serving path yet). An empty
    incoming `model_id` never clobbers an existing non-empty one (e.g. a repo
    already adopted with a serverless mapping must keep it when an import
    re-registers the roster row)."""
    conn = get_db()
    conn.execute(
        'INSERT INTO project_hf_models '
        '(project_id, repo_id, model_id, label, provider, validation_score) '
        'VALUES (?,?,?,?,?,?) '
        'ON CONFLICT(project_id, repo_id) DO UPDATE SET '
        "model_id=CASE WHEN excluded.model_id='' THEN project_hf_models.model_id "
        'ELSE excluded.model_id END, '
        'label=excluded.label, provider=excluded.provider, '
        'validation_score=excluded.validation_score',
        (project_id, repo_id, model_id or '', label or '', provider or '',
         float(validation_score or 0.0)))
    conn.commit()
    conn.close()


def remove_project_hf_model(project_id, repo_id):
    conn = get_db()
    conn.execute('DELETE FROM project_hf_models WHERE project_id=? AND repo_id=?',
                 (project_id, repo_id))
    conn.commit()
    conn.close()


def hf_queue_tasks():
    """Cross-project list of tasks assigned to a Hugging Face model (hf_repo_id
    set), enriched with project name/path. Used by the GPU run-window queue."""
    tasks = get_tasks(include_archived=False)
    return [t for t in tasks if t.get('hf_repo_id')]


def add_deployment_call(deployment_id, project_id=None, task_id=None, exec_id=None,
                        tokens_in=0, tokens_out=0, duration_s=0.0, cost_usd=0.0):
    conn = get_db()
    conn.execute(
        'INSERT INTO scw_deployment_calls '
        '(deployment_id, project_id, task_id, exec_id, tokens_in, tokens_out, duration_s, cost_usd) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (deployment_id, project_id, task_id, exec_id,
         int(tokens_in or 0), int(tokens_out or 0), float(duration_s or 0.0), float(cost_usd or 0.0)))
    conn.commit()
    conn.close()


def get_deployment_calls(deployment_id):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM scw_deployment_calls WHERE deployment_id=? ORDER BY id ASC',
        (deployment_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_model_import(project_id, repo_id, model_name, scw_model_id=None,
                     status='preparing', size_bytes=None):
    """Insert a custom-model import row and return its DB id."""
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO scw_model_imports '
        '(project_id, repo_id, model_name, scw_model_id, status, size_bytes) '
        'VALUES (?,?,?,?,?,?)',
        (project_id, repo_id, model_name, scw_model_id, status, size_bytes))
    conn.commit()
    db_id = cur.lastrowid
    conn.close()
    return db_id


_UNSET = object()


def update_model_import(import_id, status=_UNSET, error_message=_UNSET,
                        scw_model_id=_UNSET):
    """Update an import row by DB id. Pass a field to change it; the sentinel
    distinguishes "leave alone" from an explicit ``None`` (which clears the
    column) — needed because ``_poll`` resets ``error_message`` to NULL on a
    successful import after a previous failure."""
    sets, vals = [], []
    for col, val in (('status', status), ('error_message', error_message),
                     ('scw_model_id', scw_model_id)):
        if val is not _UNSET:
            sets.append(f'{col}=?')
            vals.append(val)
    if not sets:
        return
    sets.append('updated_at=CURRENT_TIMESTAMP')
    vals.append(import_id)
    conn = get_db()
    conn.execute(f'UPDATE scw_model_imports SET {", ".join(sets)} WHERE id=?', vals)
    conn.commit()
    conn.close()


def get_model_imports(project_id=None, scw_model_id=None):
    conn = get_db()
    if scw_model_id:
        rows = conn.execute('SELECT * FROM scw_model_imports WHERE scw_model_id=? '
                            'ORDER BY id DESC', (scw_model_id,)).fetchall()
    elif project_id is not None:
        rows = conn.execute('SELECT * FROM scw_model_imports WHERE project_id=? '
                            'ORDER BY id DESC', (project_id,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM scw_model_imports ORDER BY id DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_model_import(import_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM scw_model_imports WHERE id=?',
                       (import_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def find_active_model_import(repo_id):
    """Newest import row for `repo_id` that is still usable (has a Scaleway model
    id and is in-flight or ready). Used to reuse an existing library model instead
    of importing the same HF repo again and inflating quota."""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM scw_model_imports WHERE repo_id=? AND scw_model_id IS NOT NULL "
        "AND status IN ('preparing','downloading','ready') ORDER BY id DESC LIMIT 1",
        (repo_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def find_latest_model_import(repo_id):
    """Newest import row for `repo_id` regardless of status (for status display)."""
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM scw_model_imports WHERE repo_id=? ORDER BY id DESC LIMIT 1',
        (repo_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def ensure_model_import(project_id, repo_id, model_name, scw_model_id, status,
                        size_bytes=None, error_message=None):
    """Backfill an import row only when the repo has none yet — used to reconcile
    custom models imported out-of-band (Scaleway console, or before AIngel tracked
    imports). Returns the new row id, or None when a row already existed."""
    conn = get_db()
    existing = conn.execute('SELECT id FROM scw_model_imports WHERE repo_id=? LIMIT 1',
                            (repo_id,)).fetchone()
    if existing:
        conn.close()
        return None
    cur = conn.execute(
        'INSERT INTO scw_model_imports '
        '(project_id, repo_id, model_name, scw_model_id, status, error_message, size_bytes) '
        'VALUES (?,?,?,?,?,?,?)',
        (project_id, repo_id, model_name, scw_model_id, status, error_message, size_bytes))
    conn.commit()
    db_id = cur.lastrowid
    conn.close()
    return db_id


def remove_model_import(import_id):
    conn = get_db()
    conn.execute('DELETE FROM scw_model_imports WHERE id=?', (import_id,))
    conn.commit()
    conn.close()


def get_shared_windows():
    """List shared GPU windows (is_shared=1, not deleted) with their per-project
    cost breakdown from scw_deployment_calls."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM scw_deployments WHERE is_shared=1 AND status NOT IN ('deleted','deleting') "
        'ORDER BY id DESC').fetchall()
    conn.close()
    windows = []
    for r in rows:
        w = dict(r)
        calls = get_deployment_calls(w['id'])
        by_project = {}
        for c in calls:
            pid = c.get('project_id')
            if pid is None:
                continue
            by_project[pid] = by_project.get(pid, 0.0) + float(c.get('cost_usd') or 0.0)
        w['calls'] = calls
        w['cost_by_project'] = by_project
        windows.append(w)
    return windows


def allocate_window_costs(deployment_id):
    """Split a shared window's accrued cost across the projects that used it,
    weighted by their tokens, and add each share to that project's monthly spend.
    Returns {project_id: share_usd}. Called once at close (close_window guards
    against re-entry via the deleted/deleting status check), so it is not
    idempotent on its own — do not call it a second time for the same window."""
    conn = get_db()
    dep = conn.execute('SELECT * FROM scw_deployments WHERE id=?', (deployment_id,)).fetchone()
    conn.close()
    if not dep:
        return {}
    dep = dict(dep)
    calls = get_deployment_calls(deployment_id)
    if not calls:
        return {}
    # Weight by tokens; fall back to equal split if no tokens recorded.
    weights = {}
    for c in calls:
        pid = c.get('project_id')
        if pid is None:
            continue
        w = int(c.get('tokens_in') or 0) + int(c.get('tokens_out') or 0)
        weights[pid] = weights.get(pid, 0) + (w if w > 0 else 1)
    total_w = sum(weights.values()) or 1
    accrued = float(dep.get('accrued_cost_usd') or 0.0)
    if accrued <= 0:
        return {}
    shares = {}
    for pid, w in weights.items():
        share = round(accrued * (w / total_w), 6)
        shares[pid] = share
        update_budget_spend(pid, share)
    return shares


def project_task_count(project_id):
    proj = get_project(project_id)
    if not proj or not proj.get('path'):
        return 0
    proj_path = proj['path']
    if not os.path.exists(os.path.join(proj_path, 'project.db')):
        return 0
    pconn = get_project_db(proj_path)
    row = pconn.execute('SELECT COUNT(*) AS n FROM tasks WHERE project_id=?', (project_id,)).fetchone()
    pconn.close()
    return int(row['n']) if row else 0


def delete_project(project_id):
    """Delete project from central DB. Also cleans up project.db via cascade."""
    conn = get_db()
    # Clean up task_dependencies FIRST (while task_registry still has the IDs)
    conn.execute(
        'DELETE FROM task_dependencies WHERE task_id IN '
        '(SELECT id FROM task_registry WHERE project_id=?) OR '
        'depends_on_id IN (SELECT id FROM task_registry WHERE project_id=?)',
        (project_id, project_id)
    )
    # Then remove from registries
    conn.execute('DELETE FROM task_registry WHERE project_id=?', (project_id,))
    conn.execute('DELETE FROM exec_registry WHERE project_id=?', (project_id,))
    conn.execute('DELETE FROM chat_registry WHERE project_id=?', (project_id,))
    # Cost attribution rows tied to this project (no FK cascade on these).
    # Two categories must be removed:
    #   (a) calls on project-OWNED (non-shared) deployments, including rows
    #       where project_id IS NULL — deleted by deployment_id so the
    #       scw_deployments delete below doesn't hit an FK NO-ACTION violation.
    #   (b) calls attributed to this project on SHARED windows (is_shared=1)
    #       that the project merely used — deleted by project_id, since those
    #       shared deployments are NOT being deleted and the rows would
    #       otherwise linger as orphan cost-attribution pointing at a gone project.
    conn.execute(
        'DELETE FROM scw_deployment_calls WHERE deployment_id IN '
        '(SELECT id FROM scw_deployments WHERE project_id=? AND is_shared=0)',
        (project_id,)
    )
    conn.execute('DELETE FROM scw_deployment_calls WHERE project_id=?', (project_id,))
    conn.execute('DELETE FROM scw_session_costs WHERE project_id=?', (project_id,))
    # Custom-model import rows reference the project with an FK NO-ACTION, so
    # they must be cleared before the projects row (the remote Scaleway library
    # model they point at is intentionally left alone — it may still be deployed).
    conn.execute('DELETE FROM scw_model_imports WHERE project_id=?', (project_id,))
    # scw_deployments rows owned by this project (non-shared windows). Shared
    # windows (is_shared=1) are left alone — they belong to no single project.
    conn.execute('DELETE FROM scw_deployments WHERE project_id=? AND is_shared=0', (project_id,))
    # projects row last — roles + project_hf_models cascade via FK
    conn.execute('DELETE FROM projects WHERE id=?', (project_id,))
    conn.commit()
    conn.close()


# ── Archive / restore: central-row export + import ───────────────────────────
# A project is a folder under PROJECTS_ROOT plus a handful of central aingel.db
# rows. The archive feature snapshots both: the folder becomes the zip tree
# (project/) and these rows become manifest.json. Restore re-creates the rows
# with their ORIGINAL ids so GLOBAL registry ids (task/exec/chat) keep matching
# the per-project project.db primary keys, which are the same numbers.
#
# Design invariants, encoded here so a future edit does not silently break them:
#   * scw_* project columns are exported CLEARED (None). The existing DELETE
#     route crypto-shreds the Scaleway bucket/KMS key irreversibly, so a
#     restored project can never re-attach to the old session — it must start
#     session-less. The manifest therefore carries no secret material.
#   * scw_deployments / scw_deployment_calls / scw_session_costs ARE exported,
#     per product decision, to preserve spend history even though delete_project
#     removes them (agent_db.py delete_project, ~1992-2001).
#   * task_dependencies has no project_id column — it is scoped exactly like
#     delete_project scopes it: task_id OR depends_on_id in this project's
#     task_registry. Restore relies on the ids being globally unique so the
#     same filter is correct on the way back in.
#   * The registries store the GLOBAL ids; project.db task/exec/chat PKs equal
#     these. Restore inserts the registry ids VERBATIM and rewrites project_path;
#     it never invents new ids.
# All ids in a v1 archive are same-instance only — restore refuses when any id
# or name already exists (ArchiveConflictError).


class ArchiveConflictError(Exception):
    """Raised when an archive restore would collide with existing instance
    state (name, slug, folder, or any globally-unique id). The message is
    caller-facing and safe to return verbatim in a JSON {"error": ...} body."""


# The central-DB transaction is ordered parents-before-children (FKs are ON):
# projects -> roles/project_hf_models -> project_members -> registries ->
# task_dependencies -> scw_deployments -> scw_deployment_calls ->
# scw_session_costs. The registry sections are positional tuples and the calls
# section needs FK-aware filtering, so the loop is written out explicitly in
# restore_project_central_rows rather than driven by a table list constant.


def _table_columns(conn, table):
    """Column names of `table` in declaration order (empty if it does not exist)."""
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    except Exception:
        return []


def export_project_central_rows(project_id):
    """Read-only snapshot of a project's central aingel.db rows.

    Returns a dict shaped exactly like the manifest sections consumed by
    restore_project_central_rows (see agent_archive.py's manifest schema). Opens
    its own connection and closes it; never writes. The `project` row has every
    scw_* column forced to None (see module comment above).
    """
    conn = get_db()
    try:
        out = {}
        row = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
        if not row:
            return {}
        proj = dict(row)
        for col in list(proj.keys()):
            if col.startswith('scw_'):
                proj[col] = None
        out['project'] = proj

        out['roles'] = [dict(r) for r in conn.execute(
            'SELECT * FROM roles WHERE project_id=? ORDER BY id', (project_id,)).fetchall()]
        out['project_hf_models'] = [dict(r) for r in conn.execute(
            'SELECT * FROM project_hf_models WHERE project_id=? ORDER BY id',
            (project_id,)).fetchall()]
        out['project_members'] = [dict(r) for r in conn.execute(
            'SELECT project_id, user_id, role FROM project_members WHERE project_id=?',
            (project_id,)).fetchall()]

        # task_dependencies has no project_id — scope exactly like delete_project.
        out['task_dependencies'] = [dict(r) for r in conn.execute(
            'SELECT * FROM task_dependencies WHERE task_id IN '
            '(SELECT id FROM task_registry WHERE project_id=?) OR '
            'depends_on_id IN (SELECT id FROM task_registry WHERE project_id=?) '
            'ORDER BY id', (project_id, project_id)).fetchall()]

        # Registries: global ids, emitted as positional tuples (id, project_id,
        # project_path) so a compact manifest cannot lose a column name.
        regs = {}
        for reg, key in (('task_registry', 'tasks'), ('exec_registry', 'execs'),
                         ('chat_registry', 'chats')):
            regs[key] = [list(r) for r in conn.execute(
                f'SELECT id, project_id, project_path FROM {reg} '
                'WHERE project_id=? ORDER BY id', (project_id,)).fetchall()]
        out['registries'] = regs

        # Spend history. scw_deployment_calls includes rows attributed to this
        # project on SHARED windows (project_id match) AND rows on windows this
        # project OWNS (non-shared) even when project_id is NULL — mirroring the
        # two delete_project branches so a restore does not lose any call row.
        out['scw_deployments'] = [dict(r) for r in conn.execute(
            'SELECT * FROM scw_deployments WHERE project_id=? AND is_shared=0 '
            'ORDER BY id', (project_id,)).fetchall()]
        out['scw_deployment_calls'] = [dict(r) for r in conn.execute(
            'SELECT * FROM scw_deployment_calls WHERE project_id=? OR deployment_id IN '
            '(SELECT id FROM scw_deployments WHERE project_id=? AND is_shared=0) '
            'ORDER BY id', (project_id, project_id)).fetchall()]
        out['scw_session_costs'] = [dict(r) for r in conn.execute(
            'SELECT * FROM scw_session_costs WHERE project_id=? ORDER BY id',
            (project_id,)).fetchall()]
        return out
    finally:
        conn.close()


def _manifest_registry_rows(manifest):
    """Flatten manifest['registries'] into [(table, [rows...]), ...]."""
    regs = (manifest or {}).get('registries') or {}
    return (
        ('task_registry', regs.get('tasks') or []),
        ('exec_registry', regs.get('execs') or []),
        ('chat_registry', regs.get('chats') or []),
    )


def check_archive_id_collisions(manifest):
    """Return {'fatal': [...], 'skippable': [...]} id collisions for `manifest`,
    or None if every globally-unique id is free. Read-only.

    `fatal` collisions refuse the restore (409): projects id/slug/name; every
    role / project_hf_model / scw_deployments / scw_session_cost row id; and
    every task/exec/chat registry id. All of those are same-instance-only ids
    whose reuse would corrupt or duplicate rows.

    `skippable` collisions are reported separately and do NOT refuse the
    restore. `task_dependencies` has no project_id and is OR-scoped across
    projects, and `scw_deployment_calls` can be attributed to a shared window;
    the SAME row can therefore legitimately appear in two different projects'
    archives. restore_project_central_rows inserts both with INSERT OR IGNORE
    and counts the skips, so re-restoring a second project that shares a row
    does not permanently 409.

    Note: this intentionally does NOT check project_members (a (project_id,
    user_id) clash is harmless and filtered at restore), despite what an earlier
    revision of this docstring claimed.
    """
    if not manifest:
        return {'fatal': ['manifest is empty'], 'skippable': []}
    project = manifest.get('project') or {}
    fatal = []
    skippable = []
    conn = get_db()
    try:
        pid = project.get('id')
        if pid is not None:
            if conn.execute('SELECT 1 FROM projects WHERE id=?', (pid,)).fetchone():
                fatal.append(f'project id {pid}')
            if conn.execute('SELECT 1 FROM projects WHERE slug=?',
                            (project.get('slug'),)).fetchone():
                fatal.append(f'project slug {project.get("slug")!r}')
            if conn.execute('SELECT 1 FROM projects WHERE name=?',
                            (project.get('name'),)).fetchone():
                fatal.append(f'project name {project.get("name")!r}')

        def _check_rows(table, rows, bucket, require_project_match=True):
            if not rows:
                return
            for r in rows:
                if not isinstance(r, dict):
                    # Registry rows are (id, project_id, project_path) tuples; their
                    # project_id is overwritten at restore, so only the id matters.
                    rid = r[0] if isinstance(r, (list, tuple)) and r else None
                    if rid is None:
                        continue
                    if conn.execute(f'SELECT 1 FROM {table} WHERE id=?',
                                    (rid,)).fetchone():
                        bucket.append(f'{table} id {rid}')
                    continue
                rid = r.get('id')
                if rid is not None and conn.execute(
                        f'SELECT 1 FROM {table} WHERE id=?', (rid,)).fetchone():
                    bucket.append(f'{table} id {rid}')
                # A child row whose project_id is neither this project's id nor
                # NULL would be planted into ANOTHER tenant's project (the
                # manifest is attacker-controlled). The id check above cannot
                # catch it when the id is free, so refuse the mismatch outright —
                # this is what makes "fatal" mean "cannot restore". NULL is
                # allowed: scw_deployment_calls legitimately stores NULL for calls
                # on a project-owned non-shared window (see export query).
                if require_project_match and 'project_id' in r and pid is not None \
                        and r.get('project_id') not in (pid, None):
                    bucket.append(
                        f'{table} project_id {r.get("project_id")} != archive '
                        f'project id {pid}')

        _check_rows('roles', manifest.get('roles'), fatal)
        _check_rows('project_hf_models', manifest.get('project_hf_models'), fatal)
        _check_rows('scw_deployments', manifest.get('scw_deployments'), fatal)
        _check_rows('scw_session_costs', manifest.get('scw_session_costs'), fatal)
        # Cross-project rows: a hit means the row is shared, not that the archive
        # is from another instance. Restore OR-IGNOREs them — so an id collision
        # is skippable. A project_id mismatch is still fatal, though: a call row
        # pointing at another tenant must not be accepted. task_dependencies has
        # no project_id, so every id hit there is genuinely skippable.
        _check_rows('task_dependencies', manifest.get('task_dependencies'), skippable,
                    require_project_match=False)
        _check_rows('scw_deployment_calls', manifest.get('scw_deployment_calls'),
                    skippable)
        for table, rows in _manifest_registry_rows(manifest):
            _check_rows(table, rows, fatal)
    finally:
        conn.close()
    if fatal or skippable:
        return {'fatal': fatal, 'skippable': skippable}
    return None


def restore_project_central_rows(manifest, target_path, owner_id=None,
                                 allowed_user_ids=None):
    """Insert a manifest's central rows in ONE transaction, parents first.

    Uses explicit INSERT statements so the ORIGINAL ids are preserved (SQLite
    bumps sqlite_sequence to the new max after an explicit-id insert, so later
    AUTOINCREMENT rows are safe). FKs are ON: project_members rows are filtered
    to user ids that exist in `users` (dropping the rest rather than aborting),
    and the uploader is (re)installed as owner when auth/multi-tenancy is on.

    Raises ArchiveConflictError if the project row or any checked id already
    exists — the caller should have pre-checked with check_archive_id_collisions,
    but this re-check makes the write itself atomic/self-protecting.

    Returns the restored projects row as a dict.
    """
    project = dict((manifest or {}).get('project') or {})
    if not project:
        raise ArchiveConflictError('manifest has no project row')
    pid = project.get('id')
    if pid is None:
        raise ArchiveConflictError('manifest project has no id')

    conn = get_db()
    try:
        if conn.execute('SELECT 1 FROM projects WHERE id=?', (pid,)).fetchone():
            raise ArchiveConflictError(
                f'project id {pid} already exists — archive was created on another '
                'instance or IDs were reused; restore refused')
        if conn.execute('SELECT 1 FROM projects WHERE slug=?',
                        (project.get('slug'),)).fetchone():
            raise ArchiveConflictError(
                f'project slug {project.get("slug")!r} already exists')

        # Existing user ids for FK-safe membership insert. allowed_user_ids (when
        # supplied by the route) narrows this further, but we always intersect
        # with the live users table so a stale manifest can never violate the FK.
        try:
            existing_users = {r[0] for r in conn.execute('SELECT id FROM users').fetchall()}
        except Exception:
            existing_users = set()

        # ── projects (parents first) ─────────────────────────────────────────
        proj_cols = _table_columns(conn, 'projects')
        proj_vals = dict(project)
        # scw_* is already cleared at export; clear again defensively so a
        # hand-edited manifest cannot smuggle a session back in.
        for col in proj_cols:
            if col.startswith('scw_'):
                proj_vals[col] = None
        proj_vals['path'] = target_path
        if owner_id is not None and 'owner_id' in proj_cols:
            proj_vals['owner_id'] = owner_id
        cols = [c for c in proj_cols if c in proj_vals]
        placeholders = ','.join('?' for _ in cols)
        conn.execute(
            f'INSERT INTO projects ({",".join(chr(34)+c+chr(34) for c in cols)}) '
            f'VALUES ({placeholders})', [proj_vals[c] for c in cols])

        # ── simple child tables, explicit ids ────────────────────────────────
        def _insert_rows(table, rows, or_ignore=False):
            """INSERT manifest rows verbatim. With or_ignore=True an existing id
            is silently skipped; returns the number of skipped rows so the
            caller can log shared cross-project rows rather than 409."""
            if not rows:
                return 0
            tcols = _table_columns(conn, table)
            if not tcols:
                return 0
            skipped = 0
            for r in rows:
                if not isinstance(r, dict):
                    continue
                use = [c for c in tcols if c in r]
                ph = ','.join('?' for _ in use)
                verb = 'INSERT OR IGNORE' if or_ignore else 'INSERT'
                cur = conn.execute(
                    f'{verb} INTO {table} ({",".join(chr(34)+c+chr(34) for c in use)}) '
                    f'VALUES ({ph})', [r[c] for c in use])
                if or_ignore and cur.rowcount == 0:
                    skipped += 1
            return skipped

        _insert_rows('roles', (manifest.get('roles') or []))
        _insert_rows('project_hf_models', (manifest.get('project_hf_models') or []))

        # ── project_members: FK-safe filter + uploader-as-owner ──────────────
        allowed = existing_users if allowed_user_ids is None else (
            existing_users & set(allowed_user_ids))
        for r in (manifest.get('project_members') or []):
            if not isinstance(r, dict):
                continue
            uid = r.get('user_id')
            if uid not in allowed:
                continue
            role = r.get('role') if r.get('role') in _MEMBER_ROLES else 'member'
            conn.execute(
                'INSERT OR IGNORE INTO project_members (project_id, user_id, role) '
                'VALUES (?,?,?)', (pid, uid, role))
        if owner_id is not None and owner_id in existing_users:
            conn.execute(
                'INSERT INTO project_members (project_id, user_id, role) VALUES (?,?,?) '
                'ON CONFLICT(project_id, user_id) DO UPDATE SET role=excluded.role',
                (pid, owner_id, 'owner'))

        # ── registries (verbatim global ids, new absolute project_path) ──────
        for table, rows in _manifest_registry_rows(manifest):
            for r in rows or []:
                if isinstance(r, dict):
                    rid = r.get('id'); rpath = target_path
                elif isinstance(r, (list, tuple)) and len(r) >= 3:
                    rid = r[0]; rpath = target_path
                else:
                    continue
                if rid is None:
                    continue
                conn.execute(
                    f'INSERT INTO {table} (id, project_id, project_path) VALUES (?,?,?)',
                    (rid, pid, rpath))

        # ── dependencies (global task ids) ───────────────────────────────────
        # task_dependencies has no project_id and is OR-scoped across projects, so
        # the SAME dependency row can appear in two projects' archives. OR IGNORE
        # so restoring the second project does not 409 on the shared id; log the
        # count instead. (id is the PK and UNIQUE(task_id,depends_on_id) covers
        # the other collision shape.)
        skipped_deps = _insert_rows(
            'task_dependencies', (manifest.get('task_dependencies') or []),
            or_ignore=True)
        if skipped_deps:
            _log.info('restore: skipped %d task_dependency row(s) already present '
                      '(shared across projects)', skipped_deps)

        # ── spend history ────────────────────────────────────────────────────
        _insert_rows('scw_deployments', (manifest.get('scw_deployments') or []))

        # scw_deployment_calls.deployment_id is NOT NULL + FK to scw_deployments.
        # The manifest includes calls attributed to this project on SHARED
        # windows, but shared deployments belong to no single project and are
        # deliberately NOT archived (delete_project leaves is_shared=1 rows
        # alone). On a SAME-instance restore the shared parent still exists, so
        # every call restores. On a fresh/lost instance it does not — inserting
        # such a call would raise a FK violation and abort the whole restore, so
        # we skip only those orphaned call rows and log them. (Exporting the
        # shared parent is NOT an option: it would collide on same-instance
        # restore and is a global infra row, not project data.)
        existing_deps = {r[0] for r in conn.execute('SELECT id FROM scw_deployments').fetchall()}
        skipped_calls = 0
        ignored_calls = 0
        calls = manifest.get('scw_deployment_calls') or []
        call_cols = _table_columns(conn, 'scw_deployment_calls')
        for r in calls:
            if not isinstance(r, dict):
                continue
            if r.get('deployment_id') not in existing_deps:
                skipped_calls += 1
                continue
            # Force the attribution to THIS project (or NULL, the legitimate
            # owned-window case) rather than trusting the manifest value; the
            # collision pre-check already refuses a foreign non-NULL project_id,
            # but this keeps the write self-protecting against a manifest that
            # was never pre-checked.
            vals = {c: r[c] for c in call_cols if c in r}
            if 'project_id' in vals:
                vals['project_id'] = pid if r.get('project_id') is not None else None
            use = list(vals)
            ph = ','.join('?' for _ in use)
            # OR IGNORE: a call attributed to a shared window can appear in more
            # than one project's archive; skip the duplicate rather than 409.
            cur = conn.execute(
                f'INSERT OR IGNORE INTO scw_deployment_calls '
                f'({",".join(chr(34)+c+chr(34) for c in use)}) '
                f'VALUES ({ph})', [vals[c] for c in use])
            if cur.rowcount == 0:
                ignored_calls += 1
        if skipped_calls:
            _log.warning('restore: skipped %d scw_deployment_call(s) whose shared '
                         'deployment is not present on this instance', skipped_calls)
        if ignored_calls:
            _log.info('restore: skipped %d scw_deployment_call row(s) already '
                      'present (shared across projects)', ignored_calls)

        _insert_rows('scw_session_costs', (manifest.get('scw_session_costs') or []))

        conn.commit()
        # The commit is the last fallible step that matters. Everything after it
        # must NOT raise: agent_archive.restore_archive sets `central_committed`
        # only once this function returns, so an exception here would make the
        # caller rmtree the folder while the rows are already durable — orphaning
        # committed rows and burning globally-unique ids. The read-back is only a
        # convenience (the caller mostly uses id/path), so fall back to the values
        # we just inserted.
        try:
            row = conn.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
            return dict(row) if row else dict(proj_vals)
        except Exception as e:
            _log.warning('restore: project row read-back failed after commit: %s', e)
            return dict(proj_vals)
    except sqlite3.IntegrityError as e:
        # A unique/PK/FK violation here means an id was taken between the
        # up-front collision pre-check and this write (a race). Surface it as a
        # conflict, not a 500, so the caller's 409 contract holds.
        try:
            conn.rollback()
        except Exception:
            pass
        raise ArchiveConflictError(
            'an id from the archive was taken while restoring — restore refused '
            f'({e})')
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _budget_period_key(reset_day, now=None):
    now = now or datetime.now(timezone.utc)
    day = max(1, min(28, int(reset_day or 1)))
    if now.day < day:
        year = now.year if now.month > 1 else now.year - 1
        month = now.month - 1 if now.month > 1 else 12
    else:
        year, month = now.year, now.month
    return year * 100 + month


def check_budget_reset(project_id):
    conn = get_db()
    row = conn.execute(
        'SELECT id, budget_monthly, current_month_spend, budget_reset_day, budget_month '
        'FROM projects WHERE id=?', (project_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    row = dict(row)
    current_period = _budget_period_key(row.get('budget_reset_day') or 1)
    if row.get('budget_month') != current_period:
        conn.execute(
            'UPDATE projects SET current_month_spend=0.0, budget_month=? WHERE id=?',
            (current_period, project_id)
        )
        conn.commit()
        row['current_month_spend'] = 0.0
        row['budget_month'] = current_period
    conn.close()
    return row


def update_budget_spend(project_id, cost):
    try:
        cost = float(cost or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0:
        return get_project_budget(project_id)
    check_budget_reset(project_id)
    conn = get_db()
    conn.execute(
        'UPDATE projects SET current_month_spend=COALESCE(current_month_spend,0)+? WHERE id=?',
        (cost, project_id)
    )
    conn.commit()
    row = conn.execute(
        'SELECT budget_monthly, current_month_spend, budget_reset_day, budget_month '
        'FROM projects WHERE id=?', (project_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_project_budget(project_id):
    check_budget_reset(project_id)
    conn = get_db()
    row = conn.execute(
        'SELECT id, name, budget_monthly, current_month_spend, budget_reset_day, budget_month '
        'FROM projects WHERE id=?', (project_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Tasks (per-project DB) ────────────────────────────────────────────────────

def _enrich_task(task_dict, proj_info):
    """Add project_name, project_slug, path to a task dict from central project info."""
    if proj_info:
        task_dict.setdefault('project_name', proj_info.get('name', ''))
        task_dict.setdefault('project_slug', proj_info.get('slug', ''))
        task_dict.setdefault('path', proj_info.get('path', ''))
    return task_dict


def _task_query(pconn, project_id=None, status=None, include_archived=True,
                work_session_slot=None, task_id=None):
    # dep_up_count / dep_down_count / has_incomplete_deps come from _fill_dep_counts();
    # task_dependencies lives in aingel.db, not in project.db
    q = '''
        SELECT t.*,
               COALESCE((SELECT SUM(tokens_input + tokens_output)
                          FROM executions e WHERE e.task_id = t.id AND e.status='done'), 0) AS actual_tokens,
               0 AS dep_up_count,
               0 AS dep_down_count,
               0 AS has_incomplete_deps,
               (SELECT e.aingel_brief FROM executions e
                WHERE e.task_id = t.id AND e.aingel_brief IS NOT NULL AND e.aingel_brief != ''
                ORDER BY e.id DESC LIMIT 1) AS aingel_brief
        FROM tasks t WHERE 1=1
    '''
    params = []
    if task_id is not None:
        q += ' AND t.id=?'; params.append(task_id)
    if project_id is not None:
        q += ' AND t.project_id=?'; params.append(project_id)
    if status and status != 'all':
        q += ' AND t.status=?'; params.append(status)
    if not include_archived:
        q += ' AND COALESCE(t.archived,0)=0'
    if work_session_slot is not None:
        q += ' AND t.work_session_slot=?'; params.append(work_session_slot)
    q += (
        ' ORDER BY (t.work_session_slot IS NULL) DESC, t.work_session_slot,'
        " CASE WHEN t.work_session_slot IS NULL"
        " THEN -CAST(strftime('%s', t.created_at) AS INTEGER)"
        " ELSE t.slot_position END,"
        ' t.priority, t.created_at'
    )
    rows = pconn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def _fill_dep_counts(tasks):
    """Fill dep_up_count / dep_down_count / has_incomplete_deps from central aingel.db."""
    if not tasks:
        return tasks
    ids = [t['id'] for t in tasks]
    placeholders = ','.join('?' * len(ids))
    conn = get_db()
    up_map = {r['task_id']: r['c'] for r in conn.execute(
        f'SELECT task_id, COUNT(*) c FROM task_dependencies WHERE task_id IN ({placeholders}) GROUP BY task_id',
        ids
    ).fetchall()}
    down_map = {r['depends_on_id']: r['c'] for r in conn.execute(
        f'SELECT depends_on_id, COUNT(*) c FROM task_dependencies WHERE depends_on_id IN ({placeholders}) GROUP BY depends_on_id',
        ids
    ).fetchall()}
    conn.close()
    for t in tasks:
        t['dep_up_count'] = up_map.get(t['id'], 0)
        t['dep_down_count'] = down_map.get(t['id'], 0)
        t['has_incomplete_deps'] = 1 if t['dep_up_count'] > 0 else 0
    return tasks


def _hydrate_task_context_refs(task_dict):
    """Parse task_dict['context_refs'] JSON string to list in-place."""
    raw = task_dict.get('context_refs')
    if raw is None:
        task_dict['context_refs'] = []
    elif isinstance(raw, list):
        task_dict['context_refs'] = _normalize_context_refs(raw)
    elif isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            task_dict['context_refs'] = []
        else:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    task_dict['context_refs'] = _normalize_context_refs(parsed)
                else:
                    task_dict['context_refs'] = []
            except Exception:
                task_dict['context_refs'] = []
    else:
        task_dict['context_refs'] = []
    return task_dict


def _hydrate_tasks_context_refs(tasks):
    for t in tasks or []:
        _hydrate_task_context_refs(t)
    return tasks


def upsert_task(project_id, external_id, title, description='',
                model='claude-sonnet-5-5', priority=5, status='pending',
                phase_name='Notebook', project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        return None
    pconn = get_project_db(project_path)
    existing = pconn.execute(
        'SELECT id, status, phase_name FROM tasks WHERE project_id=? AND external_id=?',
        (project_id, external_id)
    ).fetchone()
    if existing:
        updates = {}
        if status == 'done' and existing['status'] == 'pending':
            updates['status'] = 'done'
        if phase_name and not existing['phase_name']:
            updates['phase_name'] = phase_name
        if updates:
            updates['updated_at'] = datetime.now(timezone.utc).isoformat()
            set_clause = ', '.join(f'{k}=?' for k in updates)
            pconn.execute(f'UPDATE tasks SET {set_clause} WHERE id=?',
                          list(updates.values()) + [existing['id']])
            pconn.commit()
        pconn.close()
        return None

    # Allocate global ID
    central = get_db()
    central.execute('INSERT INTO task_registry (project_id, project_path) VALUES (?,?)',
                    (project_id, project_path))
    central.commit()
    task_id = central.execute('SELECT last_insert_rowid()').fetchone()[0]
    central.close()

    pconn.execute(
        'INSERT INTO tasks (id, project_id, external_id, title, description, phase_name, source, model, priority, status) '
        'VALUES (?,?,?,?,?,?,"md",?,?,?)',
        (task_id, project_id, external_id, title, description, phase_name or '', model, priority, status)
    )
    pconn.commit()
    pconn.close()
    return task_id


def get_tasks(status=None, project_id=None, include_archived=True, work_session_slot=None,
              project_path=None):
    pm = _projects_map()

    if project_id is not None:
        if not project_path:
            proj = pm.get(project_id)
            project_path = proj['path'] if proj else None
        if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
            return []
        pconn = get_project_db(project_path)
        tasks = _task_query(pconn, project_id=project_id, status=status,
                            include_archived=include_archived, work_session_slot=work_session_slot)
        pconn.close()
        proj_info = pm.get(project_id)
        for t in tasks:
            _enrich_task(t, proj_info)
        _hydrate_tasks_context_refs(tasks)
        return _fill_dep_counts(tasks)

    # Cross-project
    all_tasks = []
    for pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            tasks = _task_query(pconn, project_id=pid, status=status,
                                include_archived=include_archived, work_session_slot=work_session_slot)
            pconn.close()
            proj_info = pm.get(pid)
            for t in tasks:
                _enrich_task(t, proj_info)
            all_tasks.extend(tasks)
        except Exception:
            pass

    _hydrate_tasks_context_refs(all_tasks)
    _fill_dep_counts(all_tasks)

    def _sort_key(t):
        slot = t.get('work_session_slot')
        ts = t.get('created_at', '')
        created_neg = -int(datetime.fromisoformat(ts.rstrip('Z')).timestamp()) if ts else 0
        return (
            0 if slot is None else 1,
            slot or 0,
            t.get('slot_position', 0) if slot is not None else created_neg,
            t.get('priority', 5),
            ts,
        )
    all_tasks.sort(key=_sort_key)
    return all_tasks


def get_task(task_id, project_path=None):
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path:
        return None
    pconn = get_project_db(project_path)
    rows = _task_query(pconn, task_id=task_id)
    pconn.close()
    if not rows:
        return None
    t = rows[0]
    conn = get_db()
    proj_row = conn.execute('SELECT id, name, slug, path FROM projects WHERE id=?', (t['project_id'],)).fetchone()
    conn.close()
    if proj_row:
        _enrich_task(t, dict(proj_row))
    _hydrate_task_context_refs(t)
    _fill_dep_counts([t])
    return t


def get_latest_done_execution_for_task(task_id, project_path=None):
    """Return the most recent 'done' execution row for a task, or None.
    Includes id, status, output_summary, aingel_brief, git_branch, git_commit.
    """
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return None
    pconn = get_project_db(project_path)
    row = pconn.execute(
        'SELECT id, status, output_summary, aingel_brief, git_branch, git_commit, '
        '       started_at, finished_at, model '
        'FROM executions WHERE task_id=? AND status="done" '
        'ORDER BY started_at DESC, id DESC LIMIT 1',
        (task_id,)
    ).fetchone()
    pconn.close()
    return dict(row) if row else None


def _execution_output_path(project_path, exec_id):
    return os.path.join(project_path, 'Artifacts', 'outputs', f'exec-{exec_id}-output.md')


def _find_execution_output(project_path, exec_id):
    """Find exec output file, supporting both legacy flat and Lane B slug subdir layout."""
    legacy = _execution_output_path(project_path, exec_id)
    if os.path.exists(legacy):
        return legacy
    base = os.path.join(project_path, 'Artifacts', 'outputs')
    if not os.path.isdir(base):
        return None
    target = f'exec-{exec_id}-output.md'
    try:
        for entry in os.listdir(base):
            sub = os.path.join(base, entry)
            if os.path.isdir(sub):
                cand = os.path.join(sub, target)
                if os.path.exists(cand):
                    return cand
            # also check legacy nested one level deeper just in case
    except Exception:
        pass
    # full walk fallback (cheap, outputs dir is small)
    for root, _, files in os.walk(base):
        if target in files:
            return os.path.join(root, target)
    return None


def _execution_has_output(project_path, exec_id):
    return _find_execution_output(project_path, exec_id) is not None


def get_attachable_tasks(project_id, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return []
    pconn = get_project_db(project_path)
    rows = pconn.execute('''
        SELECT t.id, t.title, t.status, COALESCE(t.archived,0) archived,
               (SELECT e.id FROM executions e WHERE e.task_id = t.id AND e.status='done'
                ORDER BY e.started_at DESC, e.id DESC LIMIT 1) AS latest_done_exec_id,
               (SELECT e.output_summary FROM executions e WHERE e.task_id = t.id AND e.status='done'
                ORDER BY e.started_at DESC, e.id DESC LIMIT 1) AS latest_done_summary
          FROM tasks t
         WHERE t.project_id=? AND (t.status='done' OR COALESCE(t.archived,0)=1)
         ORDER BY t.id
    ''', (project_id,)).fetchall()
    pconn.close()

    conn = get_db()
    dep_up = {r['task_id']: r['c'] for r in conn.execute(
        'SELECT task_id, COUNT(*) c FROM task_dependencies GROUP BY task_id'
    ).fetchall()}
    dep_down = {r['depends_on_id']: r['c'] for r in conn.execute(
        'SELECT depends_on_id, COUNT(*) c FROM task_dependencies GROUP BY depends_on_id'
    ).fetchall()}
    conn.close()

    attachable = []
    for row in rows:
        exec_id = row['latest_done_exec_id']
        has_file = bool(exec_id and _execution_has_output(project_path, exec_id))
        has_summary = bool((row['latest_done_summary'] or '').strip())
        if not (has_file or has_summary):
            continue
        tid = int(row['id'])
        has_deps = bool(dep_up.get(tid) or dep_down.get(tid))
        status = row['status'] or 'done'
        attachable.append({
            'kind': 'task', 'ref': tid, 'id': tid,
            'name': f"#{tid} {row['title'] or ''}".strip(),
            'label': f"#{tid} {row['title'] or ''}".strip(),
            'has_dependencies': has_deps, 'status': status,
            'color': '#28a745' if status == 'done' else '#6c757d',
        })
    attachable.sort(key=lambda t: (0 if t['has_dependencies'] else 1, t['id']))
    return attachable


def get_project_dependencies(project_id):
    conn = get_db()
    rows = conn.execute('''
        SELECT td.task_id, td.depends_on_id, td.spec_keys,
               tr_src.project_id AS task_project_id, tr_src.project_path AS task_project_path,
               tr_dep.project_id AS depends_on_project_id, tr_dep.project_path AS depends_on_project_path
        FROM task_dependencies td
        JOIN task_registry tr_src ON tr_src.id = td.task_id
        JOIN task_registry tr_dep ON tr_dep.id = td.depends_on_id
        WHERE tr_src.project_id=? OR tr_dep.project_id=?
        ORDER BY td.task_id, td.depends_on_id
    ''', (project_id, project_id)).fetchall()
    conn.close()

    pm = _projects_map()
    result = []
    for row in rows:
        d = dict(row)
        src_task = get_task(d['task_id'], d.get('task_project_path'))
        dep_task = get_task(d['depends_on_id'], d.get('depends_on_project_path'))
        src_proj = pm.get(d['task_project_id']) or {}
        dep_proj = pm.get(d['depends_on_project_id']) or {}
        result.append({
            'task_id': d['task_id'],
            'depends_on_id': d['depends_on_id'],
            'spec_keys': d['spec_keys'],
            'task_title': src_task['title'] if src_task else '',
            'task_status': src_task['status'] if src_task else '',
            'task_project_id': d['task_project_id'],
            'task_project_name': src_proj.get('name', ''),
            'task_stale': src_task.get('stale', 0) if src_task else 0,
            'depends_on_title': dep_task['title'] if dep_task else '',
            'depends_on_status': dep_task['status'] if dep_task else '',
            'depends_on_project_id': d['depends_on_project_id'],
            'depends_on_project_name': dep_proj.get('name', ''),
            'depends_on_stale': dep_task.get('stale', 0) if dep_task else 0,
        })
    return result


def update_task(task_id, project_path=None, **kwargs):
    allowed = {'title', 'description', 'status', 'model', 'priority',
               'estimated_tokens', 'estimated_cost', 'actual_cost',
               'phase_name', 'session_id', 'work_session_slot', 'archived',
               'slot_position', 'handoff_context', 'handoff_source_exec_id',
               'handoff_created_at', 'role_id', 'stale',
                # Phase 7: AIngel Autopilot gate state
                'original_description', 'gate_state', 'gate_reason',
                'gate_source', 'gate_decided_at', 'gate_report_json',
                'gate_report_h2_json',
                 # Hugging Face Scout: assigned repo + awaiting-self-host flag
                 'hf_repo_id', 'awaiting_model',
                 # RAG: per-task opt-in + corpus override
                 'corpus_id', 'requires_rag',
                 # Lane B: task context isolation
                 'context_refs',
                 # Multi-tenancy (Phase 1): creating user id
                 'created_by',
                 # Processing type: NULL / 'research' / 'deployment'
                 'execution_type'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path:
        return
    fields['updated_at'] = datetime.now(timezone.utc).isoformat()
    set_clause = ', '.join(f'{k}=?' for k in fields)
    vals = list(fields.values()) + [task_id]
    pconn = get_project_db(project_path)
    pconn.execute(f'UPDATE tasks SET {set_clause} WHERE id=?', vals)
    pconn.commit()
    pconn.close()
    # Emit event for SSE subscribers
    proj_id = _resolve_project_id(task_id=task_id, project_path=project_path)
    if proj_id is not None:
        _emit_safe(proj_id, {'type': 'task_changed', 'task_id': task_id})

    # Regenerate GUIDE.md so the roadmap reflects status / phase / slot changes
    # immediately. No-op if suppressed (bulk import).
    import agent_guide_sync
    agent_guide_sync.after_task_change(project_path, project_id=proj_id, task_id=task_id)


def claim_task(task_id, project_path=None):
    """Atomically claim a task for execution.

    Moves the row from pending/confirmed to running in a single guarded UPDATE
    and reports whether *this* caller won. Two concurrent runners therefore
    cannot both start the same task.
    """
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path:
        return False
    pconn = get_project_db(project_path)
    try:
        cur = pconn.execute(
            "UPDATE tasks SET status='running', updated_at=? "
            "WHERE id=? AND status IN ('confirmed','pending')",
            (datetime.now(timezone.utc).isoformat(), task_id))
        pconn.commit()
        claimed = cur.rowcount == 1
    finally:
        pconn.close()
    if claimed:
        proj_id = _resolve_project_id(task_id=task_id, project_path=project_path)
        if proj_id is not None:
            _emit_safe(proj_id, {'type': 'task_changed', 'task_id': task_id})
    return claimed


def save_task_handoff(task_id, handoff_context, source_exec_id=None, project_path=None):
    update_task(task_id, project_path=project_path,
                handoff_context=(handoff_context or '').strip(),
                handoff_source_exec_id=source_exec_id,
                handoff_created_at=datetime.now(timezone.utc).isoformat())


def clear_task_handoff(task_id, project_path=None):
    update_task(task_id, project_path=project_path,
                handoff_context='', handoff_source_exec_id=None, handoff_created_at=None)


def delete_task(task_id, project_path=None):
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute('UPDATE executions SET task_id=NULL WHERE task_id=?', (task_id,))
    pconn.execute('UPDATE chats SET task_id=NULL WHERE task_id=?', (task_id,))
    pconn.execute('DELETE FROM tasks WHERE id=?', (task_id,))
    pconn.commit()
    pconn.close()
    # Clean up registry and dependencies
    conn = get_db()
    conn.execute('DELETE FROM task_registry WHERE id=?', (task_id,))
    conn.execute('DELETE FROM task_dependencies WHERE task_id=? OR depends_on_id=?', (task_id, task_id))
    conn.commit()
    conn.close()

    # Regenerate GUIDE.md so the roadmap reflects the deletion immediately.
    proj_id = _resolve_project_id(task_id=task_id, project_path=project_path)
    import agent_guide_sync
    agent_guide_sync.after_task_change(project_path, project_id=proj_id, task_id=task_id)


def create_task(project_id, title, description='', model='claude-sonnet-5-5',
                priority=5, phase_name='Notebook', estimated_tokens=50000, role_id=None,
                project_path=None, corpus_id=None, requires_rag=0, context_refs=None,
                created_by=None, execution_type=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        raise ValueError(f'No project_path for project {project_id}')

    central = get_db()
    central.execute('INSERT INTO task_registry (project_id, project_path) VALUES (?,?)',
                    (project_id, project_path))
    central.commit()
    task_id = central.execute('SELECT last_insert_rowid()').fetchone()[0]
    central.close()

    pconn = get_project_db(project_path)
    pconn.execute(
        'INSERT INTO tasks (id, project_id, title, description, phase_name, model, priority, source, status, estimated_tokens, role_id, corpus_id, requires_rag, created_by, execution_type) '
        'VALUES (?,?,?,?,?,?,?,"manual","pending",?,?,?,?,?,?)',
        (task_id, project_id, title, description, phase_name or '', model, priority, estimated_tokens, role_id, corpus_id, int(requires_rag), created_by, execution_type)
    )
    pconn.commit()
    pconn.close()
    # Lane B: store explicit context refs if provided
    if context_refs is not None:
        try:
            set_task_context_refs(task_id, project_path, context_refs)
        except Exception:
            pass

    # Regenerate GUIDE.md so the roadmap reflects the new task immediately.
    import agent_guide_sync
    agent_guide_sync.after_task_change(project_path, project_id=project_id, task_id=task_id)
    return task_id


def archive_task(task_id, project_path=None):
    update_task(task_id, project_path=project_path, archived=1)


def redo_task(task_id, project_path=None):
    """Reset a done/failed/skip task back to pending.

    Clears actual_cost, gate reports, handoff, stale flag and slot assignment.
    Past executions stay linked (history preserved) but no longer roll up cost
    (actual_cost is zeroed). Downstream dependents are flagged stale so the user
    is prompted to re-run them. Emits a task_changed SSE event.
    """
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path:
        return None
    t = get_task(task_id, project_path)
    if not t:
        return None
    if t['status'] not in ('done', 'failed', 'skip'):
        return t
    update_task(task_id, project_path=project_path,
                status='pending', actual_cost=0,
                gate_state='open', gate_reason=None,
                gate_decided_at=None, gate_source=None,
                gate_report_json=None, gate_report_h2_json=None,
                handoff_context='', handoff_source_exec_id=None,
                handoff_created_at=None,
                stale=0, work_session_slot=None, slot_position=0)
    try:
        # include_done: reopening this task also invalidates downstream work that
        # already completed against its previous output.
        flag_stale_dependents(task_id, include_done=True)
    except Exception:
        pass
    return get_task(task_id, project_path)


def copy_task(task_id, new_title=None, copy_deps=False, project_path=None):
    """Clone a task into a new pending task.

    Copies: title (suffixed " (copy)" unless new_title given), description,
    phase_name, model, priority, estimated_tokens, estimated_cost, role_id.
    The clone is always pending, source='manual', no slot, no executions,
    fresh gate state, no handoff. If copy_deps=True, forward dependencies
    (what this task depends on) are copied; reverse deps are never copied.
    Returns the new task dict, or None on failure.
    """
    if not project_path:
        project_path = _get_task_path(task_id)
    if not project_path:
        return None
    src = get_task(task_id, project_path)
    if not src:
        return None
    title = new_title or f"{src['title']} (copy)"
    new_id = create_task(
        project_id=src['project_id'],
        title=title,
        description=src.get('description') or '',
        model=src.get('model') or 'claude-sonnet-5-5',
        priority=src.get('priority') or 5,
        phase_name=src.get('phase_name') or 'Notebook',
        estimated_tokens=src.get('estimated_tokens') or 50000,
        role_id=src.get('role_id'),
        project_path=project_path,
    )
    # Copy estimated_cost (not exposed in create_task signature)
    update_task(new_id, project_path=project_path,
                estimated_cost=src.get('estimated_cost') or 0.0)
    # Lane B: copy explicit context refs
    if src.get('context_refs'):
        try:
            set_task_context_refs(new_id, project_path, src.get('context_refs') or [])
        except Exception:
            pass
    if copy_deps:
        deps = get_dependencies(task_id)
        for d in deps:
            add_dependency(new_id, d['id'],
                           spec_keys=json.loads(d.get('dep_spec_keys') or '[]'))
    return get_task(new_id, project_path)


# ── Task dependencies (central DB — cross-project) ────────────────────────────

def get_dependencies(task_id):
    conn = get_db()
    rows = conn.execute(
        'SELECT td.depends_on_id, td.spec_keys, tr.project_path '
        'FROM task_dependencies td LEFT JOIN task_registry tr ON tr.id = td.depends_on_id '
        'WHERE td.task_id=? ORDER BY td.depends_on_id',
        (task_id,)
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        t = get_task(row['depends_on_id'], row['project_path'])
        if t:
            t['dep_spec_keys'] = row['spec_keys']
            result.append(t)
    return result


def get_dependents(task_id):
    conn = get_db()
    rows = conn.execute(
        'SELECT td.task_id, td.spec_keys, tr.project_path '
        'FROM task_dependencies td LEFT JOIN task_registry tr ON tr.id = td.task_id '
        'WHERE td.depends_on_id=? ORDER BY td.task_id',
        (task_id,)
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        t = get_task(row['task_id'], row['project_path'])
        if t:
            t['dep_spec_keys'] = row['spec_keys']
            result.append(t)
    return result


def add_dependency(task_id, depends_on_id, spec_keys=None):
    conn = get_db()
    conn.execute(
        'INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_id, spec_keys) VALUES (?,?,?)',
        (task_id, depends_on_id, json.dumps(spec_keys or []))
    )
    conn.commit()
    conn.close()


def remove_dependency(task_id, depends_on_id):
    conn = get_db()
    conn.execute(
        'DELETE FROM task_dependencies WHERE task_id=? AND depends_on_id=?',
        (task_id, depends_on_id)
    )
    conn.commit()
    conn.close()


def auto_chain_dependencies(project_id):
    """Add sequential dependencies (N → N-1) for all tasks in a project.

    Sorts non-archived tasks by id, and for each task after the first,
    adds a dependency on the previous task — UNLESS the task already has
    at least one user-configured dependency (preserve manual work).

    Returns the number of new dependency edges added.
    """
    conn = get_db()
    proj = conn.execute('SELECT path FROM projects WHERE id=?', (project_id,)).fetchone()
    if not proj:
        conn.close()
        return 0
    project_path = proj['path']

    # Fetch all non-archived task IDs for this project, ordered by id
    task_ids = conn.execute(
        'SELECT id FROM task_registry WHERE project_id=? ORDER BY id ASC',
        (project_id,)
    ).fetchall()
    conn.close()

    if not task_ids or len(task_ids) < 2:
        return 0

    pconn = get_project_db(project_path)

    # Scan for archived tasks — exclude them from the chain
    archived = set()
    try:
        archived_rows = pconn.execute(
            'SELECT id FROM tasks WHERE status IN (\'archived\',\'skip\')'
        ).fetchall()
        archived = {r['id'] for r in archived_rows}
    except Exception:
        pass

    # Build the chain: only non-archived tasks in id order
    chain = [r['id'] for r in task_ids if r['id'] not in archived]
    pconn.close()

    if len(chain) < 2:
        return 0

    conn = get_db()
    added = 0
    for i in range(1, len(chain)):
        curr = chain[i]
        prev = chain[i - 1]

        existing = conn.execute(
            'SELECT COUNT(*) FROM task_dependencies WHERE task_id=?',
            (curr,)
        ).fetchone()[0]
        if existing > 0:
            continue

        before = conn.total_changes
        conn.execute(
            'INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_id) VALUES (?,?)',
            (curr, prev)
        )
        if conn.total_changes > before:
            added += 1

    conn.commit()
    conn.close()
    return added


def flag_stale_dependents(task_id, include_done=False):
    """Mark every task that depends on `task_id` as stale.

    By default already-completed dependents are left alone — the post-execution
    path only cares about work still to come. `include_done=True` (used by
    redo_task) also flags 'done' dependents: when an upstream task is reopened,
    the downstream work that was completed against the old output is exactly what
    needs re-running. 'skip' is never flagged in either mode — a deliberately
    skipped task shouldn't be reopened by an upstream change.
    """
    conn = get_db()
    rows = conn.execute(
        'SELECT td.task_id, tr.project_path FROM task_dependencies td '
        'LEFT JOIN task_registry tr ON tr.id = td.task_id '
        'WHERE td.depends_on_id=?',
        (task_id,)
    ).fetchall()
    conn.close()
    count = 0
    now = datetime.now(timezone.utc).isoformat()
    skip_clause = "status NOT IN ('skip')" if include_done else "status NOT IN ('done','skip')"
    for row in rows:
        tid = row['task_id']
        pp = row['project_path']
        if not pp:
            continue
        try:
            pconn = get_project_db(pp)
            pconn.execute(
                f"UPDATE tasks SET stale=1, updated_at=? WHERE id=? AND {skip_clause}",
                (now, tid)
            )
            changed = pconn.execute('SELECT changes()').fetchone()[0]
            pconn.commit()
            pconn.close()
            count += changed
        except Exception:
            pass
    return count


def update_task_h2_gate(task_id, *, h2_json, gate_state, gate_reason,
                         gate_decided_at, gate_source='H2', project_path=None):
    """Phase 7.1: write H2 gate decision to its own column (gate_report_h2_json).

    Does NOT touch gate_report_json — that column stays H3-only.
    gate_state / gate_source / gate_decided_at / gate_reason are still written
    as the live gate state (H3 will overwrite them post-run with its own decision).
    """
    update_task(task_id, project_path=project_path,
                gate_state=gate_state,
                gate_reason=gate_reason,
                gate_source=gate_source,
                gate_decided_at=gate_decided_at,
                gate_report_h2_json=h2_json)


def get_dependency_h3_outcomes(task_id, project_path=None):
    """Phase 7.1: return H3 outcomes for all tasks this task depends_on.

    Returns list of {task_id, title, severity, gate_for_next, findings, brief_2s}.
    Sourced from each dep's tasks.gate_report_json (which is H3-only after Phase 7.1).
    Only returns outcomes where gate_report_json is populated and valid.
    """
    conn = get_db()
    rows = conn.execute(
        'SELECT td.depends_on_id, tr.project_path '
        'FROM task_dependencies td LEFT JOIN task_registry tr ON tr.id = td.depends_on_id '
        'WHERE td.task_id=? ORDER BY td.depends_on_id',
        (task_id,)
    ).fetchall()
    conn.close()
    outcomes = []
    for row in rows:
        dep_id = row['depends_on_id']
        dep_pp = row['project_path'] or project_path
        if not dep_pp:
            continue
        try:
            pconn = get_project_db(dep_pp)
            dep_row = pconn.execute(
                'SELECT id, title, gate_report_json FROM tasks WHERE id=?',
                (dep_id,)
            ).fetchone()
            pconn.close()
            if not dep_row or not dep_row['gate_report_json']:
                continue
            try:
                h3 = json.loads(dep_row['gate_report_json'])
            except Exception:
                continue
            outcomes.append({
                'task_id': dep_id,
                'title': dep_row['title'] or '',
                'severity': (h3.get('severity') or 'ok').lower(),
                'gate_for_next': (h3.get('gate_for_next') or 'proceed').lower(),
                'findings': h3.get('findings') or [],
                'brief_2s': h3.get('brief_2s') or '',
            })
        except Exception:
            pass
    return outcomes

def create_execution(task_id, model, chat_id=None, project_path=None, created_by=None):
    if not project_path:
        if task_id:
            project_path = _get_task_path(task_id)
        elif chat_id:
            reg = _get_chat_reg(chat_id)
            project_path = reg['project_path'] if reg else None
    if not project_path:
        raise ValueError('create_execution: cannot determine project_path')

    proj_id = None
    conn = get_db()
    if task_id:
        reg = conn.execute('SELECT project_id FROM task_registry WHERE id=?', (task_id,)).fetchone()
        proj_id = reg['project_id'] if reg else None
    elif chat_id:
        reg = conn.execute('SELECT project_id FROM chat_registry WHERE id=?', (chat_id,)).fetchone()
        proj_id = reg['project_id'] if reg else None
    conn.execute('INSERT INTO exec_registry (project_id, project_path) VALUES (?,?)',
                 (proj_id, project_path))
    conn.commit()
    exec_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()

    pconn = get_project_db(project_path)
    pconn.execute(
        'INSERT INTO executions (id, task_id, model, status, chat_id, last_heartbeat_at, created_by) '
        "VALUES (?,?,?,\"running\",?, datetime('now'), ?)",
        (exec_id, task_id, model, chat_id, created_by)
    )
    pconn.commit()
    pconn.close()
    # Emit events for SSE subscribers
    if proj_id is not None:
        _emit_safe(proj_id, {'type': 'execution_changed', 'exec_id': exec_id, 'task_id': task_id})
        if task_id:
            _emit_safe(proj_id, {'type': 'task_changed', 'task_id': task_id})
    return exec_id


def finish_execution(exec_id, status, tokens_input=0, tokens_output=0,
                     cost_usd=0.0, output_summary='', error_message='',
                     project_path=None):
    if not project_path:
        reg = _get_exec_reg(exec_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute(
        'UPDATE executions SET status=?, tokens_input=?, tokens_output=?, cost_usd=?, '
        'output_summary=?, error_message=?, child_pid=NULL, '
        "finished_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'",
        (status, tokens_input, tokens_output, cost_usd, output_summary, error_message, exec_id)
    )
    pconn.commit()
    pconn.close()
    # Emit events for SSE subscribers
    proj_id = _resolve_project_id(exec_id=exec_id, project_path=project_path)
    if proj_id is not None:
        _emit_safe(proj_id, {'type': 'execution_changed', 'exec_id': exec_id})
        # Also emit cost_changed since finish_execution writes cost_usd
        _emit_safe(proj_id, {'type': 'cost_changed'})


def update_execution_tokens(exec_id, tokens_input, tokens_output, project_path=None):
    """Write live token totals for a running execution (mid-run progress).

    Safe to call from any thread. Only updates rows still marked 'running' so a
    completed execution's final numbers are never overwritten by a late progress
    write. Emits an SSE execution_changed event so the frontend refetches.
    """
    if not project_path:
        reg = _get_exec_reg(exec_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    try:
        pconn = get_project_db(project_path)
        pconn.execute(
            'UPDATE executions SET tokens_input=?, tokens_output=? '
            "WHERE id=? AND status='running'",
            (tokens_input, tokens_output, exec_id))
        pconn.commit()
        pconn.close()
    except Exception:
        return
    proj_id = _resolve_project_id(exec_id=exec_id, project_path=project_path)
    if proj_id is not None:
        _emit_safe(proj_id, {'type': 'execution_changed', 'exec_id': exec_id})


def set_execution_pid(exec_id, pid, project_path=None):
    """Record the OS PID of the provider subprocess for a running execution.
    Used by the /cancel endpoint and the orphan sweep to identify/kill the
    actual child process."""
    pconn = None
    if project_path:
        pconn = get_project_db(project_path)
    else:
        conn = get_db()
        row = conn.execute('SELECT project_path FROM exec_registry WHERE id=?', (exec_id,)).fetchone()
        conn.close()
        if not row:
            return
        pconn = get_project_db(row['project_path'])
    try:
        pconn.execute('UPDATE executions SET child_pid=? WHERE id=? AND status="running"',
                      (pid, exec_id))
        pconn.commit()
    finally:
        pconn.close()


def get_execution_pid(exec_id, project_path=None):
    """Return (project_path, task_id, child_pid, status) for an execution, or None."""
    conn = get_db()
    row = conn.execute('SELECT project_path FROM exec_registry WHERE id=?', (exec_id,)).fetchone()
    conn.close()
    if not row:
        return None
    proj_path = project_path or row['project_path']
    pconn = get_project_db(proj_path)
    try:
        r = pconn.execute('SELECT id, task_id, child_pid, status FROM executions WHERE id=?',
                          (exec_id,)).fetchone()
    finally:
        pconn.close()
    if not r:
        return None
    return {'project_path': proj_path, 'task_id': r['task_id'],
            'child_pid': r['child_pid'], 'status': r['status']}


def update_execution_memory_status(exec_id, memory_status, project_path=None):
    if not project_path:
        reg = _get_exec_reg(exec_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute('UPDATE executions SET memory_status=? WHERE id=?', (memory_status, exec_id))
    pconn.commit()
    pconn.close()
    proj_id = _resolve_project_id(exec_id=exec_id, project_path=project_path)
    if proj_id is not None:
        _emit_safe(proj_id, {'type': 'execution_changed', 'exec_id': exec_id})


def update_execution_review(exec_id, brief=None, review_json=None, project_path=None):
    """Phase 7: store AIngel post-run analysis (H3) on the execution row.

    `brief`        — short 2-3 sentence summary (stored in executions.aingel_brief
                     for backwards-compat with the inline UI element).
    `review_json`  — full structured JSON {severity, findings, recommendation,
                     gate_for_next, brief_2s} stored in executions.aingel_review_json.
    """
    if not project_path:
        reg = _get_exec_reg(exec_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    sets, vals = [], []
    if brief is not None:
        sets.append('aingel_brief=?');       vals.append(brief)
    if review_json is not None:
        sets.append('aingel_review_json=?'); vals.append(review_json)
    if not sets:
        return
    vals.append(exec_id)
    pconn = get_project_db(project_path)
    pconn.execute(f'UPDATE executions SET {", ".join(sets)} WHERE id=?', vals)
    pconn.commit()
    pconn.close()


def overview_file_path(project_path):
    """Phase 7: path to the rolling AIngel overview markdown file."""
    return os.path.join(project_path, 'Artifacts', 'aingel-overview.md')


def update_execution_git(exec_id, branch=None, commit=None, diffstat=None,
                         merge_commit=None, project_path=None):
    if not project_path:
        reg = _get_exec_reg(exec_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    sets, vals = [], []
    if branch is not None:
        sets.append('git_branch=?');       vals.append(branch)
    if commit is not None:
        sets.append('git_commit=?');       vals.append(commit)
    if diffstat is not None:
        sets.append('git_diffstat=?');     vals.append(diffstat)
    if merge_commit is not None:
        sets.append('git_merge_commit=?'); vals.append(merge_commit)
    if not sets:
        return
    vals.append(exec_id)
    pconn = get_project_db(project_path)
    pconn.execute(f'UPDATE executions SET {", ".join(sets)} WHERE id=?', vals)
    pconn.commit()
    pconn.close()


def update_execution_rag(exec_id, label=None, provenance=None, project_path=None):
    """Record RAG provenance (badge label + retrieval detail) for an execution."""
    if not project_path:
        reg = _get_exec_reg(exec_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    sets, vals = [], []
    if label is not None:
        sets.append('rag_label=?'); vals.append(label)
    if provenance is not None:
        sets.append('rag_provenance_json=?'); vals.append(json.dumps(provenance, ensure_ascii=False, default=str))
    if not sets:
        return
    vals.append(exec_id)
    pconn = get_project_db(project_path)
    pconn.execute(f'UPDATE executions SET {", ".join(sets)} WHERE id=?', vals)
    pconn.commit()
    pconn.close()


def get_executions(limit=50):
    """Aggregate recent executions across all project DBs (task runs only, not chat).
    Excludes e.output_summary (2.7M for OCR) from the list view — detail endpoint
    fetches it separately. This was the 49s stall during 41-PDF OCR batches."""
    pm = _projects_map()
    all_execs = []
    for pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            rows = pconn.execute('''
                SELECT e.id, e.task_id, e.model, e.status, e.cost_usd,
                       e.tokens_input, e.tokens_output, e.started_at, e.finished_at,
                       e.last_heartbeat_at, e.child_pid, e.chat_id, e.aingel_brief,
                       e.error_message, e.git_branch, e.git_commit, e.git_diffstat,
                       e.memory_status, e.gpu_cost_usd, e.batch_parent_id, e.session_id,
                       e.rag_label, e.rag_provenance_json,
                       t.title task_title, t.external_id task_ext_id
                FROM executions e
                LEFT JOIN tasks t ON e.task_id = t.id
                WHERE e.chat_id IS NULL
                ORDER BY e.started_at DESC LIMIT ?
            ''', (limit,)).fetchall()
            pconn.close()
            proj_info = pm.get(pid, {})
            for row in rows:
                d = dict(row)
                d['project_id'] = pid
                d['project_name'] = proj_info.get('name', '')
                all_execs.append(d)
        except Exception:
            pass
    all_execs.sort(key=lambda e: e.get('started_at', ''), reverse=True)
    return all_execs[:limit]


def get_execution(exec_id, project_path=None):
    """Fetch a single execution with task and project info."""
    if not project_path:
        reg = _get_exec_reg(exec_id)
        if not reg:
            return None
        project_path = reg['project_path']
        proj_id = reg['project_id']
    else:
        reg = _get_exec_reg(exec_id)
        proj_id = reg['project_id'] if reg else None
    pconn = get_project_db(project_path)
    row = pconn.execute(
        'SELECT e.*, t.title task_title, t.external_id task_ext_id '
        'FROM executions e LEFT JOIN tasks t ON e.task_id = t.id WHERE e.id=?',
        (exec_id,)
    ).fetchone()
    pconn.close()
    if not row:
        return None
    result = dict(row)
    proj = get_project(proj_id) if proj_id else None
    result['project_id'] = proj_id
    result['project_name'] = proj['name'] if proj else ''
    result['project_path'] = project_path
    return result


def get_cost_summary():
    total = today = week = done_count = 0.0
    project_week = {}
    for pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            row = pconn.execute('''
                SELECT
                    COALESCE(SUM(cost_usd),0) total_spent,
                    COALESCE(SUM(CASE WHEN date(started_at)=date('now') THEN cost_usd END),0) today,
                    COALESCE(SUM(CASE WHEN started_at>=date('now','-7 days') THEN cost_usd END),0) week,
                    COUNT(CASE WHEN status='done' THEN 1 END) tasks_done
                FROM executions
            ''').fetchone()
            pconn.close()
            if row:
                total += row['total_spent'] or 0
                today += row['today'] or 0
                week  += row['week'] or 0
                done_count += row['tasks_done'] or 0
                project_week[pid] = row['week'] or 0.0
        except Exception:
            pass
    return {'total_spent': total, 'today': today, 'week': week,
            'tasks_done': int(done_count), 'project_week': project_week}


def get_cost_by_model():
    today_map = {}
    week_map = {}
    for _pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            for row in pconn.execute(
                "SELECT model, COALESCE(SUM(cost_usd),0) AS spent FROM executions "
                "WHERE date(started_at)=date('now') GROUP BY model"
            ).fetchall():
                today_map[row['model']] = today_map.get(row['model'], 0) + (row['spent'] or 0)
            for row in pconn.execute(
                "SELECT model, COALESCE(SUM(cost_usd),0) AS spent FROM executions "
                "WHERE started_at>=date('now','-7 days') GROUP BY model"
            ).fetchall():
                week_map[row['model']] = week_map.get(row['model'], 0) + (row['spent'] or 0)
            pconn.close()
        except Exception:
            pass
    today = sorted([{'model': m, 'spent': s} for m, s in today_map.items()], key=lambda x: -x['spent'])
    week  = sorted([{'model': m, 'spent': s} for m, s in week_map.items()],  key=lambda x: -x['spent'])
    return {'today': today, 'week': week}


def get_dashboard_stats():
    pm = _projects_map()
    projects = []
    total_tasks = done_tasks = running_tasks = pending_tasks = failed_tasks = 0
    for pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            row = pconn.execute('''
                SELECT COUNT(id) total_tasks,
                       SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done_tasks,
                       SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) running_tasks,
                       SUM(CASE WHEN status IN ('pending','confirmed') THEN 1 ELSE 0 END) pending_tasks,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed_tasks
                FROM tasks WHERE COALESCE(archived,0)=0
            ''').fetchone()
            pconn.close()
            proj_info = pm.get(pid, {})
            d = {
                'id': pid, 'name': proj_info.get('name', ''), 'slug': proj_info.get('slug', ''),
                'budget_monthly': proj_info.get('budget_monthly') or 0.0,
                'current_month_spend': proj_info.get('current_month_spend') or 0.0,
            }
            if row:
                for k in ('total_tasks', 'done_tasks', 'running_tasks', 'pending_tasks', 'failed_tasks'):
                    d[k] = row[k] or 0
            else:
                for k in ('total_tasks', 'done_tasks', 'running_tasks', 'pending_tasks', 'failed_tasks'):
                    d[k] = 0
            t = d['total_tasks']
            dn = d['done_tasks']
            d['progress_pct'] = round(dn / t * 100, 1) if t > 0 else 0
            projects.append(d)
            total_tasks   += d['total_tasks']
            done_tasks    += d['done_tasks']
            running_tasks += d['running_tasks']
            pending_tasks += d['pending_tasks']
            failed_tasks  += d['failed_tasks']
        except Exception:
            pass
    overall_pct = round(done_tasks / total_tasks * 100, 1) if total_tasks > 0 else 0
    return {
        'projects': projects,
        'global': {
            'total_tasks': total_tasks, 'done_tasks': done_tasks,
            'running_tasks': running_tasks, 'pending_tasks': pending_tasks,
            'failed_tasks': failed_tasks, 'overall_progress_pct': overall_pct,
        },
    }


def get_chat_executions(chat_id, project_path=None):
    if not project_path:
        reg = _get_chat_reg(chat_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return []
    pconn = get_project_db(project_path)
    rows = pconn.execute(
        'SELECT * FROM executions WHERE chat_id=? ORDER BY started_at',
        (chat_id,)
    ).fetchall()
    pconn.close()
    return [dict(r) for r in rows]


# ── Chats (per-project DB) ────────────────────────────────────────────────────

def create_chat(project_id, name, phase_name='', task_id=None, model='', attachments=None,
                project_path=None, created_by=None):
    att_json = json.dumps(attachments or [])
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        raise ValueError(f'No project_path for project {project_id}')

    central = get_db()
    central.execute('INSERT INTO chat_registry (project_id, project_path) VALUES (?,?)',
                    (project_id, project_path))
    central.commit()
    chat_id = central.execute('SELECT last_insert_rowid()').fetchone()[0]
    central.close()

    pconn = get_project_db(project_path)
    pconn.execute(
        'INSERT INTO chats (id, project_id, phase_name, task_id, name, model, attachments, created_by) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (chat_id, project_id, phase_name or '', task_id, name, model or '', att_json, created_by)
    )
    pconn.commit()
    row = pconn.execute('SELECT * FROM chats WHERE id=?', (chat_id,)).fetchone()
    pconn.close()
    # Emit event for SSE subscribers
    _emit_safe(project_id, {'type': 'chat_changed', 'chat_id': chat_id})
    return dict(row)


def _resolve_chat_file_path(chat, project_path):
    """Re-anchor a stale chat file_path to the project's own chats folder.

    file_path is stored as an absolute path at chat creation and never
    rewritten, so a project moved or imported from elsewhere (laptop, old
    storage root) keeps paths that no longer exist. The file itself travels
    with the project under Artifacts/chats/ with the same basename.
    """
    fp = chat.get('file_path')
    if not fp or not project_path or os.path.exists(fp):
        return
    candidate = os.path.join(project_path, 'Artifacts', 'chats', os.path.basename(fp))
    if os.path.exists(candidate):
        chat['file_path'] = candidate


def get_chats(project_id=None, phase_name=None, task_id=None, status='active',
              project_path=None):
    pm = _projects_map()

    def _query_one(pconn, pid, pp):
        q = '''
            SELECT c.*, t.title task_title, t.phase_name task_phase_name,
                   (SELECT COUNT(*) FROM executions e WHERE e.chat_id=c.id) message_count
            FROM chats c LEFT JOIN tasks t ON c.task_id=t.id WHERE 1=1
        '''
        params = []
        if pid is not None:
            q += ' AND c.project_id=?'; params.append(pid)
        if phase_name is not None:
            q += ' AND c.phase_name=?'; params.append(phase_name)
        if task_id is not None:
            q += ' AND c.task_id=?'; params.append(task_id)
        if status and status != 'all':
            q += ' AND c.status=?'; params.append(status)
        q += ' ORDER BY c.updated_at DESC, c.created_at DESC'
        rows = pconn.execute(q, params).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            # Always return attachments as a list (stored as JSON string in DB).
            raw_att = d.get('attachments')
            if isinstance(raw_att, str):
                try:
                    d['attachments'] = json.loads(raw_att) if raw_att else []
                except Exception:
                    d['attachments'] = []
            elif raw_att is None:
                d['attachments'] = []
            proj_info = pm.get(d['project_id']) or {}
            d['project_name'] = proj_info.get('name', '')
            _resolve_chat_file_path(d, pp)
            result.append(d)
        return result

    if project_id is not None:
        if not project_path:
            proj_info = pm.get(project_id)
            project_path = proj_info['path'] if proj_info else None
        if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
            return []
        pconn = get_project_db(project_path)
        result = _query_one(pconn, project_id, project_path)
        pconn.close()
        return result

    # Cross-project
    all_chats = []
    for pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            all_chats.extend(_query_one(pconn, pid, pp))
            pconn.close()
        except Exception:
            pass
    all_chats.sort(key=lambda c: (c.get('updated_at') or c.get('created_at') or ''), reverse=True)
    return all_chats


def get_chat(chat_id, project_path=None):
    if not project_path:
        reg = _get_chat_reg(chat_id)
        if not reg:
            return None
        project_path = reg['project_path']
        proj_id = reg['project_id']
    else:
        reg = _get_chat_reg(chat_id)
        proj_id = reg['project_id'] if reg else None
    pconn = get_project_db(project_path)
    row = pconn.execute(
        'SELECT c.*, t.title task_title, t.description task_description, t.model task_model '
        'FROM chats c LEFT JOIN tasks t ON c.task_id=t.id WHERE c.id=?',
        (chat_id,)
    ).fetchone()
    pconn.close()
    if not row:
        return None
    result = dict(row)
    # Always return attachments as a list (stored as JSON string in DB).
    raw_att = result.get('attachments')
    if isinstance(raw_att, str):
        try:
            result['attachments'] = json.loads(raw_att) if raw_att else []
        except Exception:
            result['attachments'] = []
    elif raw_att is None:
        result['attachments'] = []
    proj = get_project(proj_id) if proj_id else None
    result['project_name'] = proj['name'] if proj else ''
    result['project_path'] = project_path
    _resolve_chat_file_path(result, project_path)
    return result


def update_chat(chat_id, project_path=None, **kwargs):
    allowed = {'name', 'status', 'file_path', 'model', 'phase_name', 'attachments', 'scaffold_draft', 'auto_inject_defs'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    if 'attachments' in fields and not isinstance(fields['attachments'], str):
        fields['attachments'] = json.dumps(fields['attachments'] or [])
    if 'auto_inject_defs' in fields:
        fields['auto_inject_defs'] = 1 if fields['auto_inject_defs'] else 0
    fields['updated_at'] = datetime.now(timezone.utc).isoformat()
    if not project_path:
        reg = _get_chat_reg(chat_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    set_clause = ', '.join(f'{k}=?' for k in fields)
    pconn = get_project_db(project_path)
    pconn.execute(f'UPDATE chats SET {set_clause} WHERE id=?', list(fields.values()) + [chat_id])
    pconn.commit()
    pconn.close()
    proj_id = _resolve_project_id(chat_id=chat_id, project_path=project_path)
    if proj_id is not None:
        _emit_safe(proj_id, {'type': 'chat_changed', 'chat_id': chat_id})


def touch_chat(chat_id, project_path=None):
    if not project_path:
        reg = _get_chat_reg(chat_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute('UPDATE chats SET updated_at=? WHERE id=?',
                  (datetime.now(timezone.utc).isoformat(), chat_id))
    pconn.commit()
    pconn.close()
    proj_id = _resolve_project_id(chat_id=chat_id, project_path=project_path)
    if proj_id is not None:
        _emit_safe(proj_id, {'type': 'chat_changed', 'chat_id': chat_id})


def delete_chat(chat_id, project_path=None):
    if not project_path:
        reg = _get_chat_reg(chat_id)
        project_path = reg['project_path'] if reg else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute('UPDATE executions SET chat_id=NULL WHERE chat_id=?', (chat_id,))
    pconn.execute('DELETE FROM chats WHERE id=?', (chat_id,))
    pconn.commit()
    pconn.close()
    conn = get_db()
    conn.execute('DELETE FROM chat_registry WHERE id=?', (chat_id,))
    conn.commit()
    conn.close()


# ── Project skills (per-project DB) ──────────────────────────────────────────

def save_project_skills(project_id, detected, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute('DELETE FROM project_skills WHERE project_id=? AND auto_detected=1', (project_id,))
    rows = []
    for category, items in (detected or {}).items():
        for name in items or []:
            rows.append((project_id, category, name))
    if rows:
        pconn.executemany(
            'INSERT OR IGNORE INTO project_skills (project_id, category, name, auto_detected) VALUES (?,?,?,1)',
            rows
        )
    pconn.commit()
    pconn.close()


def get_project_skills(project_id, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return {}
    pconn = get_project_db(project_path)
    rows = pconn.execute(
        'SELECT category, name, auto_detected, detected_at '
        'FROM project_skills WHERE project_id=? ORDER BY category, auto_detected DESC, name',
        (project_id,)
    ).fetchall()
    pconn.close()
    grouped = {}
    for r in rows:
        grouped.setdefault(r['category'], []).append({
            'name': r['name'], 'auto_detected': bool(r['auto_detected']), 'detected_at': r['detected_at'],
        })
    return grouped


def add_manual_skill(project_id, category, name, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute(
        'INSERT OR IGNORE INTO project_skills (project_id, category, name, auto_detected) VALUES (?,?,?,0)',
        (project_id, category, name)
    )
    pconn.commit()
    pconn.close()


def delete_manual_skill(project_id, category, name, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute(
        'DELETE FROM project_skills WHERE project_id=? AND category=? AND name=? AND auto_detected=0',
        (project_id, category, name)
    )
    pconn.commit()
    pconn.close()


# ── Project permissions (per-project DB) ──────────────────────────────────────

def get_project_permissions(project_id, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path or not os.path.exists(os.path.join(project_path, 'project.db')):
        return {}
    pconn = get_project_db(project_path)
    rows = pconn.execute(
        'SELECT group_key, rule, enabled, auto_added, added_at '
        'FROM project_permissions WHERE project_id=? ORDER BY group_key, auto_added DESC, rule',
        (project_id,)
    ).fetchall()
    pconn.close()
    grouped = {}
    for r in rows:
        grouped.setdefault(r['group_key'], []).append({
            'rule': r['rule'], 'enabled': bool(r['enabled']),
            'auto_added': bool(r['auto_added']), 'added_at': r['added_at'],
        })
    return grouped


def seed_project_permissions(project_id, group_rules, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    rows = [
        (project_id, gk, rule)
        for gk, rules in (group_rules or {}).items()
        for rule in rules
    ]
    if rows:
        pconn.executemany(
            'INSERT OR IGNORE INTO project_permissions '
            '(project_id, group_key, rule, enabled, auto_added) VALUES (?,?,?,1,1)',
            rows
        )
    pconn.commit()
    pconn.close()


def toggle_permission(project_id, rule, enabled, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute(
        'UPDATE project_permissions SET enabled=? WHERE project_id=? AND rule=?',
        (1 if enabled else 0, project_id, rule)
    )
    pconn.commit()
    pconn.close()


def add_custom_permission(project_id, group_key, rule, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute(
        'INSERT OR IGNORE INTO project_permissions '
        '(project_id, group_key, rule, enabled, auto_added) VALUES (?,?,?,1,0)',
        (project_id, group_key, rule)
    )
    pconn.commit()
    pconn.close()


def delete_custom_permission(project_id, rule, project_path=None):
    if not project_path:
        proj = get_project(project_id)
        project_path = proj['path'] if proj else None
    if not project_path:
        return
    pconn = get_project_db(project_path)
    pconn.execute(
        'DELETE FROM project_permissions WHERE project_id=? AND rule=? AND auto_added=0',
        (project_id, rule)
    )
    pconn.commit()
    pconn.close()


# ── Work sessions (central DB) ────────────────────────────────────────────────

def get_work_sessions():
    from agent_config import SESSION_DURATION_HOURS
    conn = get_db()
    rows = [dict(r) for r in conn.execute('SELECT * FROM work_sessions ORDER BY slot').fetchall()]
    stats = {row['slot']: {'planned_tokens': 0, 'task_count': 0, 'done_count': 0} for row in rows}
    conn.close()

    for pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            for r in pconn.execute('''
                SELECT work_session_slot,
                       COUNT(*) AS task_count,
                       COALESCE(SUM(estimated_tokens), 0) AS planned_tokens,
                       SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_count
                  FROM tasks
                 WHERE COALESCE(archived,0)=0 AND work_session_slot IS NOT NULL
                 GROUP BY work_session_slot
            ''').fetchall():
                slot = r['work_session_slot']
                if slot in stats:
                    stats[slot]['planned_tokens'] += r['planned_tokens'] or 0
                    stats[slot]['task_count']     += r['task_count']     or 0
                    stats[slot]['done_count']     += r['done_count']     or 0
            pconn.close()
        except Exception:
            pass

    now = datetime.utcnow()
    for s in rows:
        s.update(stats.get(s['slot'], {'planned_tokens': 0, 'task_count': 0, 'done_count': 0}))
        started = s.get('started_at')
        ends    = s.get('ends_at')
        if started and ends:
            try:
                ends_dt = datetime.fromisoformat(ends)
                delta   = (ends_dt - now).total_seconds()
                s['seconds_remaining'] = max(0, int(delta))
                s['expired']           = delta <= 0
            except Exception:
                s['seconds_remaining'] = None
                s['expired']           = False
        else:
            s['seconds_remaining'] = None
            s['expired']           = False
        s['duration_seconds'] = int(SESSION_DURATION_HOURS * 3600)
        nr = s.get('next_run_at')
        if nr:
            try:
                s['seconds_to_next_run'] = max(0, int((datetime.fromisoformat(nr) - now).total_seconds()))
            except Exception:
                s['seconds_to_next_run'] = None
        else:
            s['seconds_to_next_run'] = None
    return rows


def get_work_session(slot):
    conn = get_db()
    row = conn.execute('SELECT * FROM work_sessions WHERE slot=?', (slot,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_slot_next_run(slot, next_run_at_iso, pause_reason=None):
    conn = get_db()
    conn.execute('UPDATE work_sessions SET next_run_at=?, pause_reason=? WHERE slot=?',
                 (next_run_at_iso, pause_reason, slot))
    conn.commit()
    conn.close()


def start_work_session(slot, force=False):
    from agent_config import SESSION_DURATION_HOURS
    from datetime import timedelta
    conn = get_db()
    row = conn.execute('SELECT * FROM work_sessions WHERE slot=?', (slot,)).fetchone()
    if not row:
        conn.close()
        return None
    row = dict(row)
    if row.get('started_at') and not force:
        conn.close()
        return row
    now  = datetime.now(timezone.utc)
    ends = now + timedelta(hours=SESSION_DURATION_HOURS)
    conn.execute(
        "UPDATE work_sessions SET started_at=?, ends_at=?, status='active' WHERE slot=?",
        (now.isoformat(), ends.isoformat(), slot)
    )
    conn.commit()
    updated = dict(conn.execute('SELECT * FROM work_sessions WHERE slot=?', (slot,)).fetchone())
    conn.close()
    return updated


def get_column_tasks(slot, statuses=('confirmed',)):
    """Return non-archived tasks in a kanban column across all project DBs."""
    placeholders = ','.join('?' * len(statuses))
    result = []
    for pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            rows = pconn.execute(
                f'SELECT * FROM tasks WHERE work_session_slot=? '
                f'AND status IN ({placeholders}) AND COALESCE(archived,0)=0 ORDER BY slot_position',
                (slot, *statuses)
            ).fetchall()
            pconn.close()
            result.extend([dict(r) for r in rows])
        except Exception:
            pass
    result.sort(key=lambda t: t.get('slot_position', 0))
    return result


def slot_capacity_remaining(slot, exclude_task_id=None):
    conn = get_db()
    ws = conn.execute('SELECT token_budget, tokens_used FROM work_sessions WHERE slot=?', (slot,)).fetchone()
    conn.close()
    if not ws:
        return 0
    budget = ws['token_budget'] or 150000
    used   = ws['tokens_used']  or 0
    planned = 0
    for pid, pp in _all_project_paths():
        try:
            pconn = get_project_db(pp)
            q = 'SELECT COALESCE(SUM(estimated_tokens),0) AS s FROM tasks WHERE work_session_slot=? AND COALESCE(archived,0)=0'
            params = [slot]
            if exclude_task_id is not None:
                q += ' AND id != ?'; params.append(exclude_task_id)
            planned += pconn.execute(q, params).fetchone()['s'] or 0
            pconn.close()
        except Exception:
            pass
    return max(0, budget - used - planned)


def auto_promote_task(task_id, candidate_slots=(3,)):
    project_path = _get_task_path(task_id)
    if not project_path:
        return {'moved': False, 'reason': 'task not found'}
    pconn = get_project_db(project_path)
    row = pconn.execute('SELECT id, work_session_slot, estimated_tokens FROM tasks WHERE id=?', (task_id,)).fetchone()
    if not row:
        pconn.close()
        return {'moved': False, 'reason': 'task not found'}
    row = dict(row)
    pconn.close()
    cur_slot = row['work_session_slot']
    need = row['estimated_tokens'] or 0
    if cur_slot is None or need <= 0:
        return {'moved': False, 'from_slot': cur_slot, 'to_slot': cur_slot, 'reason': 'unassigned or zero-token'}
    remaining = slot_capacity_remaining(cur_slot, exclude_task_id=task_id)
    if need <= remaining:
        return {'moved': False, 'from_slot': cur_slot, 'to_slot': cur_slot, 'reason': 'fits in current slot'}
    for nxt in candidate_slots:
        if nxt == cur_slot:
            continue
        if slot_capacity_remaining(nxt) >= need:
            pconn = get_project_db(project_path)
            max_pos_row = pconn.execute(
                'SELECT COALESCE(MAX(slot_position), -1)+1 AS p FROM tasks WHERE work_session_slot=?', (nxt,)
            ).fetchone()
            max_pos = max_pos_row['p'] if max_pos_row else 0
            pconn.execute(
                'UPDATE tasks SET work_session_slot=?, slot_position=?, updated_at=? WHERE id=?',
                (nxt, max_pos, datetime.now(timezone.utc).isoformat(), task_id)
            )
            pconn.commit()
            pconn.close()
            return {'moved': True, 'from_slot': cur_slot, 'to_slot': nxt,
                    'reason': f'needed {need} tokens, column {cur_slot} had {remaining} left'}
    return {'moved': False, 'from_slot': cur_slot, 'to_slot': cur_slot, 'reason': 'no candidate slot has capacity'}


def update_work_session_tokens(slot, tokens_delta):
    if not slot or tokens_delta <= 0:
        return
    conn = get_db()
    conn.execute('UPDATE work_sessions SET tokens_used=tokens_used+? WHERE slot=?', (int(tokens_delta), slot))
    conn.commit()
    conn.close()


# ── Roles (central DB — both global and project-scoped) ───────────────────────

def get_roles(project_id=None):
    conn = get_db()
    if project_id is None:
        rows = conn.execute('SELECT * FROM roles WHERE project_id IS NULL ORDER BY name').fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM roles WHERE project_id IS NULL OR project_id=? '
            'ORDER BY project_id IS NOT NULL, name',
            (project_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_role(role_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM roles WHERE id=?', (role_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_role(name, system_prompt='', default_model='', context_scope='',
                is_template=0, project_id=None):
    conn = get_db()
    conn.execute(
        'INSERT INTO roles (project_id, name, system_prompt, default_model, context_scope, is_template) '
        'VALUES (?,?,?,?,?,?)',
        (project_id, name, system_prompt or '', default_model or '', context_scope or '', int(is_template))
    )
    conn.commit()
    role_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    row = conn.execute('SELECT * FROM roles WHERE id=?', (role_id,)).fetchone()
    conn.close()
    return dict(row)


def update_role(role_id, **kwargs):
    allowed = {'name', 'system_prompt', 'default_model', 'context_scope', 'is_template'}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ', '.join(f'{k}=?' for k in fields)
    conn = get_db()
    conn.execute(f'UPDATE roles SET {set_clause} WHERE id=?', list(fields.values()) + [role_id])
    conn.commit()
    conn.close()


def delete_role(role_id):
    # Clear role_id on tasks across all project DBs
    reg = get_db()
    proj_paths = [r['project_path'] for r in reg.execute('SELECT DISTINCT project_path FROM task_registry').fetchall() if r['project_path']]
    reg.execute('DELETE FROM roles WHERE id=?', (role_id,))
    reg.commit()
    reg.close()
    for pp in proj_paths:
        try:
            pconn = get_project_db(pp)
            pconn.execute('UPDATE tasks SET role_id=NULL WHERE role_id=?', (role_id,))
            pconn.commit()
            pconn.close()
        except Exception:
            pass


def get_role_templates():
    conn = get_db()
    rows = conn.execute('SELECT * FROM project_type_templates ORDER BY id').fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d['role_names'] = json.loads(d.get('role_names') or '[]')
        except Exception:
            d['role_names'] = []
        result.append(d)
    return result


def apply_role_template(project_id, template_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM project_type_templates WHERE id=?', (template_id,)).fetchone()
    if not row:
        conn.close()
        return []
    try:
        role_names = json.loads(row['role_names'] or '[]')
    except Exception:
        role_names = []
    existing = {r['name'] for r in conn.execute(
        'SELECT name FROM roles WHERE project_id=?', (project_id,)
    ).fetchall()}
    created = []
    for name in role_names:
        if name in existing:
            continue
        global_row = conn.execute(
            'SELECT * FROM roles WHERE project_id IS NULL AND name=?', (name,)
        ).fetchone()
        src = dict(global_row) if global_row else {'name': name, 'system_prompt': '', 'default_model': '', 'context_scope': ''}
        conn.execute(
            'INSERT INTO roles (project_id, name, system_prompt, default_model, context_scope, is_template) '
            'VALUES (?,?,?,?,?,?)',
            (project_id, src['name'], src.get('system_prompt', ''), src.get('default_model', ''),
             src.get('context_scope', ''), 0)
        )
        new_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        created.append(dict(conn.execute('SELECT * FROM roles WHERE id=?', (new_id,)).fetchone()))
    conn.commit()
    conn.close()
    return created
