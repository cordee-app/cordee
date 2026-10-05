"""Project archive (zip export) + restore (zip import).

A "project" in AIngel is a folder `<PROJECTS_ROOT>/<name>/` PLUS a handful of
central `aingel.db` rows (the projects row, roles, members, the global
task/exec/chat registries, dependencies, and spend-history rows). This module
snapshots both into a single `.aingel.zip` and can rebuild them on the same
instance.

Two subtleties drive most of the code here and are repeated at each site so a
future edit cannot quietly break them:

1. WAL. `project.db` runs in WAL mode, so a plain file copy of `project.db`
   misses everything committed to the `-wal` sidecar. We therefore snapshot it
   with SQLite's online backup API (`src.backup(dst)`), which produces a single
   self-contained file, and zip THAT — never the live file. `-wal`/`-shm` are
   excluded from the tree.

2. Global ids. The `task_registry`/`exec_registry`/`chat_registry` rows hold the
   GLOBALLY-unique task/exec/chat ids, and the matching per-project
   `project.db` primary keys are exactly those numbers. Restore therefore
   re-inserts every id VERBATIM (explicit `INSERT ... (id, ...)`) instead of
   letting SQLite allocate new ones. SQLite bumps `sqlite_sequence` to the new
   max after an explicit-id insert, so subsequent AUTOINCREMENT rows stay safe.
   v1 archives are same-instance only: any id already present is a hard 409.

Non-destructive by design: building an archive never closes/shreds the Scaleway
session and never deletes anything. The existing DELETE route performs the
crypto-shred; the user archives first, then deletes.
"""
import errno
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import zipfile
from datetime import datetime, timezone

import agent_config
import agent_db
from agent_db import ArchiveConflictError

# Module logger for best-effort post-commit steps (path repointing, schema
# cache discard, aingel.json write, guide regen, audit). Those must never turn a
# successful, fully-committed restore into a 500 — see restore_archive.
_log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# Format tag written into manifest.json. Restore refuses anything else.
ARCHIVE_FORMAT = 'aingel-project-archive'

# Bump when the manifest schema changes incompatibly. Restore accepts
# `version <= ARCHIVE_VERSION` and refuses a newer archive (an old build must
# not silently ignore fields it does not understand).
ARCHIVE_VERSION = 1

# Built zips live OUTSIDE any project tree so quota storage walks do not count
# them. `<PROJECTS_ROOT>/.archives/`.
ARCHIVES_DIRNAME = '.archives'
ARCHIVE_RETENTION_DAYS = 7

# Archive layout, kept as constants so export and import cannot drift.
_MANIFEST_NAME = 'manifest.json'
_PROJECT_PREFIX = 'project/'

# Restore hard caps. The free-tier storage limit (agent_quotas: 50 MB) is far
# too small to use as the zip-slip bomb guard — a legitimate project archive can
# exceed it — and free-tier storage is already enforced separately by
# check_project_create()/storage_stock_bytes. These caps exist purely to stop a
# malicious/corrupt archive from exhausting disk or inodes.
MAX_RESTORE_ENTRIES = 20000
MAX_RESTORE_BYTES = 5 * 1024 * 1024 * 1024  # 5 GiB uncompressed
# manifest.json is read into memory whole, so it needs its own small cap: a
# tiny, highly-compressible zip can otherwise force a multi-GB allocation.
# We read it through a bounded stream and refuse anything larger BEFORE json
# parsing (see _load_and_validate_manifest).
_MAX_MANIFEST_BYTES = 1 * 1024 * 1024  # 1 MiB

# Manifest project.name must satisfy the SAME rule create_project enforces
# (agent_api.py) so a crafted archive can never write outside PROJECTS_ROOT.
# The name becomes a folder under PROJECTS_ROOT; anything with '/', '\', '..'
# or NUL is rejected here before any path is built.
_PROJECT_NAME_RE = re.compile(r'^[A-Za-z0-9 _\-]+$')
# slug is the URL/DB-safe form (slugify output shape). We accept only the
# canonical `[a-z0-9-]` alphabet; separators/dots/NUL are impossible then.
_PROJECT_SLUG_RE = re.compile(r'^[a-z0-9-]+$')

# Files/dirs never copied out of a project tree. `.uploads/` is chunked-upload
# scratch, `.trash/` legacy in-tree soft-delete, and the project.db sidecars /
# backups only make sense next to the live file (we snapshot project.db instead).
_EXCLUDED_DIRS = frozenset({'.uploads', '.trash'})
_EXCLUDED_FILES = frozenset({'project.db-wal', 'project.db-shm'})

# Serializes the whole restore body. The exists-check -> move -> insert sequence
# is TOCTOU-prone: two concurrent restores of the same archive would otherwise
# nest one staging dir inside the other's freshly-created folder, and the loser's
# cleanup could rmtree the winner's live project. One process-wide lock, plus a
# rename (not shutil.move) with a lexists re-check, makes the create atomic.
_RESTORE_LOCK = threading.Lock()


class ArchiveValidationError(Exception):
    """Bad archive: wrong format/version, missing keys, zip-slip, or a cap
    exceeded. Caller maps this to HTTP 400."""


# ── Helpers ──────────────────────────────────────────────────────────────────


def archives_dir():
    """Absolute path of the built-zip parent directory (does not create it).

    Layout is `<PROJECTS_ROOT>/.archives/<pid>/<filename>.aingel.zip`, one
    subdirectory per project (see project_archives_dir). The parent used to be
    flat; the per-project subdir isolates downloads so GET can only ever serve
    the requested project's own zips (IDOR fix)."""
    return os.path.join(agent_config.PROJECTS_ROOT, ARCHIVES_DIRNAME)


def project_archives_dir(pid):
    """Absolute path of the built-zip directory for one project (does not
    create it). Confining GET to this directory is what stops a predictable
    flat filename from serving another project's archive."""
    return os.path.join(archives_dir(), str(pid))


def _archive_suffix():
    """Short random suffix for an archive filename. The timestamp is only
    second-granular, so two builds in the same second would otherwise write the
    same path and corrupt each other; 4 hex chars makes a collision negligible.
    Mirrors agent_scw_session._rand6's intent (kept local to avoid the import)."""
    return secrets.token_hex(2)


def _slugify(name):
    """Slugify for the archive filename. Mirrors agent_importer.slugify so the
    artifact name matches the project slug used elsewhere."""
    return re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-') or 'project'


def _is_excluded_file(name):
    if name in _EXCLUDED_FILES:
        return True
    # Every SQLite side file is scratch and must not ride along: historical
    # backups (project.db.bak-*), legacy pre-merge snapshots
    # (project.db.premerge-*.bak), and any future project.db.* variant. The live
    # project.db is replaced by the WAL-safe snapshot, so the only thing worth
    # capturing is project.db itself.
    if name.startswith('project.db.'):
        return True
    return False


def _snapshot_sqlite(src_path, dst_path):
    """WAL-safe snapshot of a live SQLite DB via the online backup API.

    A plain shutil.copy would miss committed pages still sitting in the `-wal`
    sidecar; `Connection.backup` reads through the live connection and writes a
    consistent single-file database. Returns True if src existed and was
    snapshot, False otherwise. Never leaves a half-written dst behind.
    """
    if not src_path or not os.path.exists(src_path):
        return False
    src = None
    dst = None
    try:
        src = sqlite3.connect(src_path, timeout=30.0)
        dst = sqlite3.connect(dst_path)
        src.backup(dst)
        dst.commit()
        return True
    except Exception:
        try:
            if dst is not None:
                dst.close()
        except Exception:
            pass
        try:
            if os.path.exists(dst_path):
                os.remove(dst_path)
        except Exception:
            pass
        raise
    finally:
        for c in (dst, src):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass


def _walk_project_files(project_dir, snapshot_db, excluded_symlinks,
                        excluded_specials=None):
    """Return (entries, excluded_specials) for every regular file to archive.

    `entries` is a list of (source_path, arcname).

    - Symlinks are SKIPPED (a followed symlink would copy its target's content
      into the archive, potentially exfiltrating files outside the project) and
      recorded in `excluded_symlinks`. Symlinked directories are never
      descended into.
    - FIFOs, sockets, block/char devices and any other non-regular,
      non-directory node are SKIPPED and recorded in `excluded_specials`. A
      `zipfile.write` on a FIFO blocks until a writer appears (or forever) and
      can stream unbounded data; a socket/device read is equally unsafe.
    - `.uploads/`, legacy `.trash/`, project.db-wal/-shm and project.db.bak-*
      are skipped.
    - The live `project.db` is replaced by `snapshot_db` (the WAL-safe copy).
    - `.git/` IS included on purpose: the archive carries full history.
    """
    if excluded_specials is None:
        excluded_specials = []
    entries = []
    for root, dirs, files in os.walk(project_dir, followlinks=False):
        rel_root = os.path.relpath(root, project_dir)

        kept_dirs = []
        for d in dirs:
            full = os.path.join(root, d)
            rel = d if rel_root == '.' else os.path.join(rel_root, d)
            if d in _EXCLUDED_DIRS:
                continue
            if os.path.islink(full):
                excluded_symlinks.append(rel.replace(os.sep, '/'))
                continue
            # os.walk puts non-directory entries in `files`, but a symlinked dir
            # is filtered above; a FIFO/socket would not appear here either.
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for f in files:
            full = os.path.join(root, f)
            rel = f if rel_root == '.' else os.path.join(rel_root, f)
            rel_posix = rel.replace(os.sep, '/')
            # lstat (not stat) so a symlink to a special file is classified by
            # the link itself and still caught by the islink branch below.
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                excluded_symlinks.append(rel_posix)
                continue
            if not stat.S_ISREG(st.st_mode):
                # FIFO / socket / device / anything else non-regular.
                excluded_specials.append(rel_posix)
                continue
            if _is_excluded_file(f):
                continue
            if rel_posix == 'project.db' and snapshot_db and os.path.exists(snapshot_db):
                entries.append((snapshot_db, _PROJECT_PREFIX + 'project.db'))
                continue
            entries.append((full, _PROJECT_PREFIX + rel_posix))

    # Deterministic-ish ordering (os.walk order is filesystem-dependent).
    entries.sort(key=lambda e: e[1])
    excluded_specials.sort()
    return entries, excluded_specials


# ── Build (export) ───────────────────────────────────────────────────────────


def build_archive(project):
    """Build a `.aingel.zip` for `project` (a central projects row dict).

    Non-destructive: never closes/shreds the Scaleway session and never deletes
    local data. The caller (route) has already enforced the running-execution /
    confirmed-task guards and resolved the project.

    If the project has an active Scaleway session, bucket objects are synced
    into `<project>/Working Documents` FIRST so bucket-only files are captured.
    A sync failure is FAIL-CLOSED (raises): the archive is non-destructive but
    the subsequent DELETE crypto-shreds the bucket irreversibly, so we must not
    hand the user an archive that is silently missing synced-but-unsynced data.

    Returns {'filename', 'size', 'path'} (plus 'sync_warning' if a sync ran but
    reported a non-fatal condition).
    """
    pid = project.get('id')
    project_path = project.get('path') or ''
    if not project_path or not os.path.isdir(project_path):
        raise ArchiveValidationError(f'project folder not found: {project_path}')

    # 1. Optional SCW bucket -> Working Documents sync (before snapshot/build).
    sync_warning = None
    if project.get('scw_session_enabled'):
        import agent_scw_session
        region = project.get('scw_session_region') or 'fr-par'
        res = agent_scw_session.sync_bucket_to_working_docs(
            project_id=pid, project_path=project_path, region=region)
        if not res or not res.get('ok'):
            # Fail closed — see docstring. The route turns this into a 500.
            raise RuntimeError(
                'SCW bucket sync failed before archiving: '
                f'{res.get("error") if isinstance(res, dict) else res!r}')
        if res.get('skipped_tombstoned'):
            sync_warning = f"{res.get('skipped_tombstoned')} tombstoned object(s) skipped"

    # 2. Build manifest from central rows + origin info.
    central = agent_db.export_project_central_rows(pid)
    if not central or not central.get('project'):
        raise ArchiveValidationError(f'project {pid} has no central DB row')

    excluded_symlinks = []
    manifest = {
        'format': ARCHIVE_FORMAT,
        'version': ARCHIVE_VERSION,
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'app_instance': getattr(agent_config, 'INSTANCE_NAME', '') or '',
        'original': {
            'id': pid,
            'name': project.get('name'),
            'slug': project.get('slug'),
            'path': project_path,
        },
        'project': central['project'],
        'roles': central.get('roles', []),
        'project_hf_models': central.get('project_hf_models', []),
        'project_members': central.get('project_members', []),
        'task_dependencies': central.get('task_dependencies', []),
        'registries': central.get('registries', {'tasks': [], 'execs': [], 'chats': []}),
        'scw_deployments': central.get('scw_deployments', []),
        'scw_deployment_calls': central.get('scw_deployment_calls', []),
        'scw_session_costs': central.get('scw_session_costs', []),
        # Populated during the walk below.
        'excluded_symlinks': excluded_symlinks,
        'excluded_specials': [],
    }

    slug = project.get('slug') or _slugify(project.get('name'))
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    filename = f'{slug}-{ts}-{_archive_suffix()}.aingel.zip'
    # Per-project subdir: keeps each project's built zips separate so the
    # download route can be scoped to the requested pid (IDOR fix).
    adir = project_archives_dir(pid)
    os.makedirs(adir, exist_ok=True)
    zip_path = os.path.join(adir, filename)

    tmp_dir = tempfile.mkdtemp(prefix='aingel-archive-')
    snapshot_db = os.path.join(tmp_dir, 'project.db')
    try:
        # 3. WAL-safe snapshot of the per-project DB.
        _snapshot_sqlite(os.path.join(project_path, 'project.db'), snapshot_db)

        # 4. Walk the tree FIRST so excluded_symlinks/excluded_specials are
        #    populated into the manifest before it is serialized. (The manifest
        #    must record what was left out, so it has to be written after the
        #    walk, not before.)
        entries, excluded_specials = _walk_project_files(
            project_path, snapshot_db, excluded_symlinks)
        manifest['excluded_specials'] = excluded_specials

        # Streamed file-by-file with ZipFile.write so large files are never read
        # whole into memory.
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            manifest_bytes = json.dumps(manifest, indent=2, default=str).encode('utf-8')
            zf.writestr(_MANIFEST_NAME, manifest_bytes)
            for src, arcname in entries:
                try:
                    zf.write(src, arcname)
                except OSError as e:
                    # A file that vanished or is unreadable mid-walk should not
                    # lose the whole archive; skip it and log. (The manifest was
                    # already serialized above, so this cannot be recorded there
                    # without a second pass — logging is the honest trade-off.)
                    print(f'[archive] skipping unreadable {arcname}: {e}')
    except Exception:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            pass
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    out = {'filename': filename, 'size': os.path.getsize(zip_path), 'path': zip_path}
    if sync_warning:
        out['sync_warning'] = sync_warning
    return out


# ── Restore (import) ─────────────────────────────────────────────────────────


def _load_and_validate_manifest(zf, info=None):
    """Read and validate manifest.json from an open ZipFile. Raises
    ArchiveValidationError on any problem.

    The manifest is read into memory, so it is capped at _MAX_MANIFEST_BYTES by
    BOTH the declared `info.file_size` (checked here, before any read) and the
    actual bytes read (a bounded read, in case the header lies). Callers must
    run _validate_zip_entries(zf) FIRST so `info` is one of the already-vetted
    entries; when `info` is None we look it up from the open central directory.
    """
    if info is None:
        info = next((i for i in zf.infolist() if i.filename == _MANIFEST_NAME), None)
    if info is None:
        raise ArchiveValidationError('archive has no manifest.json')
    declared = int(info.file_size or 0)
    if declared > _MAX_MANIFEST_BYTES:
        raise ArchiveValidationError(
            f'manifest.json is too large ({declared} > {_MAX_MANIFEST_BYTES} bytes)')
    # Bounded read: never trust file_size for the allocation. Read at most
    # cap+1 bytes so an understated header still cannot blow memory.
    try:
        with zf.open(info) as src:
            raw = src.read(_MAX_MANIFEST_BYTES + 1)
    except KeyError:
        raise ArchiveValidationError('archive has no manifest.json')
    except Exception as e:
        raise ArchiveValidationError(f'cannot read manifest.json: {e}')
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ArchiveValidationError(
            f'manifest.json exceeds the {_MAX_MANIFEST_BYTES} byte cap')
    try:
        manifest = json.loads(raw.decode('utf-8'))
    except Exception as e:
        raise ArchiveValidationError(f'manifest.json is not valid JSON: {e}')
    if not isinstance(manifest, dict):
        raise ArchiveValidationError('manifest.json must be a JSON object')
    if manifest.get('format') != ARCHIVE_FORMAT:
        raise ArchiveValidationError(
            f"unrecognized archive format: {manifest.get('format')!r}")
    try:
        version = int(manifest.get('version'))
    except (TypeError, ValueError):
        raise ArchiveValidationError('manifest version missing or not an integer')
    if version > ARCHIVE_VERSION:
        raise ArchiveValidationError(
            f'archive version {version} is newer than supported {ARCHIVE_VERSION}')
    project = manifest.get('project')
    if not isinstance(project, dict) or not project:
        raise ArchiveValidationError('manifest has no project row')
    for key in ('id', 'name', 'slug'):
        if project.get(key) in (None, ''):
            raise ArchiveValidationError(f'manifest project is missing {key!r}')
    return manifest


def _validate_and_normalize_project(manifest):
    """Validate manifest['project'] name/slug and NORMALIZE the manifest's
    project row in place. Returns the sanitized (name, slug).

    The archive's `name` is used verbatim as a directory under PROJECTS_ROOT, so
    it must satisfy the exact rule create_project enforces (agent_api.py):
    `^[A-Za-z0-9 _\\-]+$`, stripped, non-empty. `slug` must match the canonical
    `^[a-z0-9-]+$` shape. A NUL byte is impossible once the regexes match (they
    are anchored to the ASCII classes above), but we check explicitly for
    defence-in-depth. Normalizing the dict here means the collision checks, the
    target folder name, and the inserted projects row all use the same values.
    """
    project = manifest['project']
    raw_name = project.get('name')
    name = str(raw_name).strip() if raw_name is not None else ''
    if not name:
        raise ArchiveValidationError('manifest project name is empty')
    if '\x00' in name or not _PROJECT_NAME_RE.match(name):
        raise ArchiveValidationError(
            'manifest project name contains illegal characters '
            '(allowed: letters, digits, space, underscore, hyphen)')
    raw_slug = project.get('slug')
    slug = str(raw_slug).strip() if raw_slug is not None else ''
    if not slug:
        raise ArchiveValidationError('manifest project slug is empty')
    if '\x00' in slug or not _PROJECT_SLUG_RE.match(slug):
        raise ArchiveValidationError(
            'manifest project slug contains illegal characters '
            '(allowed: lowercase letters, digits, hyphen)')
    # Mutate once so every downstream consumer (collision check, folder, DB
    # row, archive path) agrees.
    project['name'] = name
    project['slug'] = slug
    return name, slug


def _validate_registry_rows(manifest):
    """Validate manifest['registries'] shape. Raises ArchiveValidationError.

    restore_project_central_rows silently skips malformed registry rows (a
    shape it does not recognise). Validating here means a project.db whose
    global ids lack registry entries cannot be created silently: an archived
    project with dangling task/exec/chat ids is rejected up front, not
    half-restored. Rows are positional tuples (id, project_id, project_path) in
    the manifest, but we accept dicts too for hand-written archives.
    """
    regs = (manifest or {}).get('registries') or {}
    if not isinstance(regs, dict):
        raise ArchiveValidationError('manifest registries is not an object')
    for key in ('tasks', 'execs', 'chats'):
        rows = regs.get(key) or []
        if not isinstance(rows, (list, tuple)):
            raise ArchiveValidationError(f'manifest registries.{key} is not a list')
        for r in rows:
            if isinstance(r, dict):
                if r.get('id') is None:
                    raise ArchiveValidationError(
                        f'manifest registries.{key} has a row with no id')
            elif isinstance(r, (list, tuple)):
                if len(r) < 3 or r[0] is None:
                    raise ArchiveValidationError(
                        f'manifest registries.{key} has a malformed row')


def _probe_extracted_project_db(staged_project):
    """Probe the extracted `project/project.db` for corruption BEFORE any move
    or central insert. Raises ArchiveValidationError on a corrupt DB.

    A valid empty project may contain no project.db at all — that is allowed and
    the probe is skipped. Opening the DB runs the file header + first page
    checks; `PRAGMA schema_version` forces it to actually parse. Any
    sqlite3.DatabaseError (file is not a database, malformed image) aborts the
    restore while nothing has been moved or committed.
    """
    db_path = os.path.join(staged_project, 'project.db')
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.execute('PRAGMA schema_version').fetchone()
    except sqlite3.DatabaseError as e:
        raise ArchiveValidationError(f'archive project.db is not a valid database: {e}')
    except Exception as e:
        raise ArchiveValidationError(f'cannot open archive project.db: {e}')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _validate_zip_entries(zf):
    """Reject dangerous archives BEFORE any extraction. Raises
    ArchiveValidationError.

    - absolute paths and `..` segments (zip-slip)
    - symlink entries (external_attr mode bits)
    - anything not under `project/` (manifest.json at root is the only
      top-level entry allowed)
    - more than MAX_RESTORE_ENTRIES entries or more than MAX_RESTORE_BYTES
      uncompressed total
    """
    infos = zf.infolist()
    if len(infos) > MAX_RESTORE_ENTRIES:
        raise ArchiveValidationError(
            f'archive has too many entries ({len(infos)} > {MAX_RESTORE_ENTRIES})')
    total = 0
    for info in infos:
        name = info.filename
        if name in ('', '.'):
            continue
        if os.path.isabs(name) or name.startswith('/'):
            raise ArchiveValidationError(f'absolute path in archive: {name!r}')
        # Normalize on '/' (zip always uses '/') and reject traversal.
        parts = name.replace('\\', '/').split('/')
        if '..' in parts:
            raise ArchiveValidationError(f'path traversal in archive: {name!r}')
        # Reject device/fifo/symlink entries. Unix mode lives in the high bits.
        mode = (info.external_attr >> 16) & 0o170000
        if mode in (stat.S_IFLNK, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO,
                    stat.S_IFSOCK):
            raise ArchiveValidationError(f'non-regular entry in archive: {name!r}')
        normalized = '/'.join(p for p in parts if p not in ('', '.'))
        if normalized == _MANIFEST_NAME:
            # The manifest counts toward the total, but only up to its own hard
            # cap (_MAX_MANIFEST_BYTES, enforced again at read time). An absurd
            # declared size is rejected here rather than inflating the total.
            declared = max(0, int(info.file_size or 0))
            if declared > _MAX_MANIFEST_BYTES:
                raise ArchiveValidationError(
                    f'manifest.json is too large ({declared} > '
                    f'{_MAX_MANIFEST_BYTES} bytes)')
            total += declared
            if total > MAX_RESTORE_BYTES:
                raise ArchiveValidationError(
                    f'archive expands beyond the {MAX_RESTORE_BYTES} byte cap')
            continue
        # Allow the bare `project` directory entry (some tools emit it) as well
        # as anything under `project/`.
        if normalized != 'project' and not normalized.startswith(_PROJECT_PREFIX):
            raise ArchiveValidationError(
                f'archive entry outside project/: {name!r}')
        total += max(0, int(info.file_size or 0))
        if total > MAX_RESTORE_BYTES:
            raise ArchiveValidationError(
                f'archive expands beyond the {MAX_RESTORE_BYTES} byte cap')


def _extract_zip(zf, staging_dir):
    """Extract validated entries one-by-one into `staging_dir` (never
    extractall — we control each destination and never follow links).

    Malformed archives surface as ArchiveValidationError (→400), not 500:
    a zip that declares `project/x` as a file and `project/x/y` as well raises
    FileExistsError; a truncated/corrupt member raises BadZipFile; an encrypted
    member raises RuntimeError. All of those are client-input problems.
    """
    real_stage = os.path.realpath(staging_dir)
    for info in zf.infolist():
        name = info.filename
        parts = [p for p in name.replace('\\', '/').split('/') if p not in ('', '.')]
        if not parts or parts == [_MANIFEST_NAME]:
            continue
        # parts[0] == 'project'; drop it so the project tree lands at
        # staging_dir/<name>.
        rel_parts = parts[1:]
        if not rel_parts:
            continue
        dest = os.path.join(staging_dir, *rel_parts)
        # Belt-and-braces containment check (validation should already cover it).
        real_dest = os.path.realpath(dest)
        if real_dest != real_stage and not real_dest.startswith(real_stage + os.sep):
            raise ArchiveValidationError(f'entry escapes staging dir: {name!r}')
        try:
            if info.is_dir():
                os.makedirs(dest, exist_ok=True)
                continue
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with zf.open(info) as src, open(dest, 'wb') as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
        except ArchiveValidationError:
            raise
        except (FileExistsError, zipfile.BadZipFile, RuntimeError, OSError) as e:
            raise ArchiveValidationError(
                f'cannot extract {name!r} from archive: {e}')


def _repoint_chat_paths(target_path):
    """Rewrite chats.file_path in the restored project.db to absolute paths
    under `target_path`.

    `_resolve_chat_file_path` self-heals by basename at read time, but doing it
    explicitly means a UI or export that reads the raw column sees a correct
    path immediately. Opens the DB directly (before schema migration) and only
    touches the chats table, which always exists in an archived project.
    """
    db_path = os.path.join(target_path, 'project.db')
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, file_path FROM chats WHERE file_path IS NOT NULL "
                "AND file_path <> ''").fetchall()
        except sqlite3.OperationalError:
            return
        for r in rows:
            fp = r['file_path'] or ''
            base = os.path.basename(fp)
            if not base:
                continue
            new_path = os.path.join(target_path, 'Artifacts', 'chats', base)
            conn.execute('UPDATE chats SET file_path=? WHERE id=?', (new_path, r['id']))
        conn.commit()
    finally:
        if conn is not None:
            conn.close()


def _audit_restore(project, source_path, target_path):
    """Append a restore line to <PROJECTS_ROOT>/.trash/_audit.log. Mirrors
    _audit_delete's loose format; best-effort, never raises."""
    try:
        trash_dir = os.path.join(agent_config.PROJECTS_ROOT, '.trash')
        os.makedirs(trash_dir, exist_ok=True)
        line = ('{ts} | action=restore | pid={pid} | name={name} | '
                'source={source} | target={target}\n').format(
            ts=datetime.now(timezone.utc).isoformat(timespec='seconds'),
            pid=project.get('id'),
            name=project.get('name'),
            source=source_path or '',
            target=target_path or '',
        )
        with open(os.path.join(trash_dir, '_audit.log'), 'a') as f:
            f.write(line)
    except Exception as e:
        print(f'[restore-project] audit log write failed: {e}')


def _discard_project_schema_cache(db_path):
    """Drop the per-process schema-migration cache for a restored project.db so
    _init_project_db_schema runs on the next open. Guarded by the same lock as
    agent_db.get_project_db (see agent_db._PROJECT_DB_READY)."""
    try:
        key = os.path.realpath(db_path)
        with agent_db._PROJECT_DB_READY_LOCK:
            agent_db._PROJECT_DB_READY.discard(key)
    except Exception:
        pass


def _best_effort(label, fn, *args, **kwargs):
    """Run a post-commit side-effect, swallowing+logging any failure.

    AFTER restore_project_central_rows commits, the restore has succeeded: the
    folder is in place and the central rows exist. Anything that follows is
    cosmetic (path repointing, schema cache, aingel.json, GUIDE regen, audit) and
    must not turn a successful restore into a 500. Each step is attempted
    independently so one failure does not skip the rest.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        _log.warning('restore: %s failed (restore already committed): %s', label, e)
        return None


def restore_archive(zip_path, uploader_id, uploader_user=None,
                    write_aingel_json=None):
    """Restore a project from an archive built by build_archive().

    Steps (see module docstring for the id/WAL rationale):
      1. reject zip-slip/symlinks/caps FIRST, then validate + normalize the
         manifest and its project name/slug;
      2. refuse fatal name/slug/folder/id collisions BEFORE any write;
      3. extract to a staging dir, probe the extracted project.db, then rename
         into <PROJECTS_ROOT>/<name>;
      4. insert central rows in one transaction with original ids;
      5. repoint absolute paths, clear scw_*, run schema migrations.

    The whole body is serialized by _RESTORE_LOCK: the exists-check -> rename ->
    insert sequence is otherwise a TOCTOU race. The folder is moved with
    os.rename (which fails rather than nesting on an existing non-empty target)
    and the destination is re-checked under the lock immediately before it.

    Failure discipline: BEFORE the central insert commits, any error removes the
    freshly-renamed folder (nothing else has changed). AFTER the insert commits
    every remaining step is best-effort and logged, never raised — deleting the
    folder then would orphan committed rows and permanently burn the global ids.

    `write_aingel_json` is an optional callable(project_path, project_row); the
    route supplies agent_api._write_aingel_json. Passed as a callback rather
    than imported so agent_archive does not create a circular import with
    agent_api (which imports agent_archive).

    Raises ArchiveValidationError (→400) or agent_db.ArchiveConflictError
    (→409). Returns the restored projects row dict.
    """
    if not zip_path or not os.path.exists(zip_path):
        raise ArchiveValidationError('uploaded archive is empty or missing')

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ArchiveValidationError(f'uploaded file is not a valid zip: {e}')

    with zf:
        # ORDER MATTERS: screen the central directory (entry caps, zip-slip,
        # symlinks) BEFORE reading manifest.json, so a zip bomb cannot force a
        # huge manifest allocation during validation.
        _validate_zip_entries(zf)
        manifest = _load_and_validate_manifest(zf)
        name, slug = _validate_and_normalize_project(manifest)
        _validate_registry_rows(manifest)

        project = dict(manifest['project'])
        pid = project.get('id')

        with _RESTORE_LOCK:
            # --- conflict checks BEFORE any write (nothing partial can happen) ---
            collisions = agent_db.check_archive_id_collisions(manifest)
            if isinstance(collisions, dict):
                fatal = collisions.get('fatal') or []
                skippable = collisions.get('skippable') or []
            else:
                fatal = collisions or []
                skippable = []
            if fatal:
                raise ArchiveConflictError(
                    'archive was created on another instance or IDs were reused; '
                    f'restore refused ({"; ".join(fatal[:5])})')
            if skippable:
                # Cross-project rows (task_dependencies / scw_deployment_calls)
                # may legitimately appear in more than one archive and are
                # inserted OR IGNORE; log rather than refuse.
                _log.info('restore: %d shared row(s) already present will be '
                          'skipped (%s)', len(skippable),
                          '; '.join(skippable[:5]))

            existing = agent_db.get_projects()
            if any((p.get('slug') or '') == slug for p in existing):
                raise ArchiveConflictError(
                    f'a project with slug {slug!r} already exists')
            if any((p.get('name') or '') == name for p in existing):
                raise ArchiveConflictError(
                    f'a project named {name!r} already exists')

            # Containment: name is regex-sanitized, but we still require the
            # resolved target to be strictly inside PROJECTS_ROOT. Mirrors the
            # spirit of the delete route's containment check.
            target_path = os.path.join(agent_config.PROJECTS_ROOT, name)
            real_root = os.path.realpath(agent_config.PROJECTS_ROOT)
            real_target = os.path.realpath(target_path)
            if not real_target.startswith(real_root + os.sep):
                raise ArchiveValidationError(
                    f'project name would escape PROJECTS_ROOT: {name!r}')
            if os.path.exists(target_path):
                raise ArchiveConflictError(
                    f'folder already exists at {target_path}')

            # --- extract into staging, probe, then rename into place --------
            staging = tempfile.mkdtemp(prefix='aingel-restore-')
            staged_project = os.path.join(staging, 'project')
            moved = False
            central_committed = False
            try:
                os.makedirs(staged_project, exist_ok=True)
                _extract_zip(zf, staged_project)
                # Defensive: the archive should not contain WAL sidecars, but
                # strip any that slipped through rather than let a stale -wal
                # shadow the restored project.db on first open.
                for sidecar in ('project.db-wal', 'project.db-shm'):
                    try:
                        os.remove(os.path.join(staged_project, sidecar))
                    except OSError:
                        pass

                # Probe the extracted DB BEFORE moving or committing anything.
                # A corrupt project.db must 400 here, not mutate the instance.
                _probe_extracted_project_db(staged_project)

                os.makedirs(agent_config.PROJECTS_ROOT, exist_ok=True)
                # Re-check under the lock immediately before the rename. rename
                # (not move) refuses to nest into an existing non-empty dir.
                if os.path.lexists(target_path):
                    raise ArchiveConflictError(
                        f'folder already exists at {target_path}')
                try:
                    os.rename(staged_project, target_path)
                except OSError as e:
                    if e.errno == errno.EXDEV:
                        # Staging (/tmp) and PROJECTS_ROOT are on different
                        # filesystems; rename cannot span them. We still hold
                        # _RESTORE_LOCK and re-checked lexists above, so the
                        # copy fallback is race-free here.
                        if os.path.lexists(target_path):
                            raise ArchiveConflictError(
                                f'folder already exists at {target_path}')
                        shutil.copytree(staged_project, target_path)
                    else:
                        # ENOTEMPTY/EEXIST (and anything else) → conflict.
                        raise ArchiveConflictError(
                            f'folder already exists at {target_path} ({e})')
                moved = True

                # --- central rows in one transaction (parents before children) ---
                owner_for_insert = uploader_id if uploader_user is not None else None
                restored = agent_db.restore_project_central_rows(
                    manifest, target_path, owner_id=owner_for_insert,
                    allowed_user_ids=None)
                # From here on the restore is committed: never delete the folder.
                central_committed = True

                # --- post-steps: all best-effort, never fatal ------------------
                _best_effort('repoint chat paths', _repoint_chat_paths, target_path)

                _best_effort('discard schema cache',
                             _discard_project_schema_cache,
                             os.path.join(target_path, 'project.db'))
                _best_effort('project.db schema init',
                             _init_restored_project_db, target_path)

                if callable(write_aingel_json):
                    _best_effort('aingel.json write', write_aingel_json,
                                 target_path, restored)

                # GUIDE.md travels in the zip and self-corrects; regenerate is a
                # best-effort convenience so a slightly stale guide is refreshed.
                _best_effort('GUIDE.md regenerate', _regen_guide, target_path, pid)

                _audit_restore(restored, manifest.get('original', {}).get('path'),
                               target_path)
                return restored
            except Exception:
                # Only undo the folder if we moved it AND the central insert did
                # NOT commit. After commit the rows exist; deleting the folder
                # would orphan them and permanently burn the global ids.
                if moved and not central_committed:
                    shutil.rmtree(target_path, ignore_errors=True)
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)


def _init_restored_project_db(target_path):
    """Open the restored project.db once so schema migrations run, then close.
    Best-effort wrapper target (called after the central rows have committed)."""
    pconn = agent_db.get_project_db(target_path)
    pconn.close()


def _regen_guide(target_path, pid):
    """Regenerate GUIDE.md for a restored project. Imported lazily/caught by the
    caller because agent_guide_sync is optional and never worth failing a restore
    over."""
    import agent_guide_sync
    agent_guide_sync.regenerate_guide(target_path, pid)


# ── Reaper ───────────────────────────────────────────────────────────────────


def reap_archives(archives_path=None, retention_days=ARCHIVE_RETENTION_DAYS):
    """Delete built-zip entries older than `retention_days` (by mtime).

    Handles both the current nested layout `<adir>/<pid>/<file>.aingel.zip` and
    any legacy flat `<adir>/<file>.aingel.zip` that predates the per-project
    subdir. Wired at startup and in the daily scheduler tick, next to
    _reap_trash. Never raises — a failure must not block boot or a scheduler
    tick. Returns the number of entries removed.
    """
    try:
        adir = archives_path or archives_dir()
        if not os.path.isdir(adir):
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - (retention_days * 86400)
        reaped = 0
        for entry in os.listdir(adir):
            full = os.path.join(adir, entry)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    # Per-project subdir: reap the zips inside it, then drop the
                    # subdir once empty (keeps .archives tidy).
                    for sub in os.listdir(full):
                        subfull = os.path.join(full, sub)
                        try:
                            if (os.path.isfile(subfull)
                                    and not os.path.islink(subfull)
                                    and os.path.getmtime(subfull) < cutoff):
                                os.remove(subfull)
                                reaped += 1
                        except Exception:
                            continue
                    try:
                        os.rmdir(full)
                    except OSError:
                        pass
                    continue
                # Legacy flat zip (or any stray file at the top level).
                if not os.path.isfile(full) or os.path.islink(full):
                    continue
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
                    reaped += 1
            except Exception:
                continue
        return reaped
    except Exception as e:
        print(f'[archive-reaper] WARNING: archives reaper failed, continuing anyway: {e}')
        return 0
