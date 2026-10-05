"""
File operations helper for AIngel Vault — Phase 1 unified file manager.

Security: all writes must stay inside Working Documents variants. Backend writes
are validated via realpath containment + forbidden checks. No Flask dependency.
"""
import os
import shutil
import time

# ── Constants ────────────────────────────────────────────────────────────────

_WORKING_DOC_FOLDERS = ('My Docs', 'Working Docs', 'Working Documents',
                        'working-docs', 'docs')
# Canonical writable variants for Phase 1 — we accept any of them but prefer
# 'Working Documents' as the canonical root.
_WRITABLE_VARIANTS = _WORKING_DOC_FOLDERS

_FORBIDDEN_SUFFIXES = ('.db', '.db-journal', '.db-wal', '.db-shm', '.bak')
_FORBIDDEN_EXACT = {'.env', '.env.example', 'aingel.db'}
_FORBIDDEN_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.claude', '.vibe', 'node_modules'}


def _is_forbidden_path(name: str) -> bool:
    low = name.lower()
    if low in _FORBIDDEN_EXACT:
        return True
    if low.startswith('.env'):
        return True
    for suf in _FORBIDDEN_SUFFIXES:
        if low.endswith(suf):
            return True
    if name.startswith('.'):
        return True
    if name in _FORBIDDEN_DIRS:
        return True
    return False


def _is_writable_rel(rel: str) -> bool:
    """True if rel is under a writable Working-Docs variant and not forbidden."""
    if not rel or not rel.strip():
        return False
    rel = rel.strip().replace(os.sep, '/')
    # Reject absolute, traversal, empty parts
    if rel.startswith('/') or rel.startswith('\\'):
        return False
    parts = rel.split('/')
    for p in parts:
        if not p or p in ('.', '..'):
            return False
        if _is_forbidden_path(p):
            return False
    # Must be under one of the writable variants
    for folder in _WRITABLE_VARIANTS:
        if rel == folder or rel.startswith(folder + '/'):
            return True
    return False


# ── Listing ──────────────────────────────────────────────────────────────────

def list_files_under(root_dir, max_depth=3, skip_hidden=True):
    """(basename, full_path) pairs found within root_dir, recursing up to
    max_depth levels (root_dir itself is level 1). First occurrence of a
    given basename wins on collision (top-down, alphabetical). Skips
    dot-prefixed entries when skip_hidden is True. Hard-capped depth —
    never walks unbounded (task #10001117: unbounded scans + catchall
    extraction hung for 28 minutes on a large project).
    """
    results = []
    seen = set()

    def _walk(d, depth):
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return
        for name in entries:
            if skip_hidden and name.startswith('.'):
                continue
            full = os.path.join(d, name)
            if os.path.isfile(full):
                if name not in seen:
                    seen.add(name)
                    results.append((name, full))
            elif os.path.isdir(full) and depth < max_depth:
                _walk(full, depth + 1)

    if root_dir and os.path.isdir(root_dir):
        _walk(root_dir, 1)
    return results


def _safe_resolve(project_path: str, rel: str) -> str:
    """Resolve rel inside project_path with containment + forbidden + symlink checks.

    Returns the lexical absolute path (without resolving the final component's
    symlink target). Raises ValueError on violation.

    Security: any existing path component that is a symlink is rejected — this
    prevents a symlink planted in Working Documents from letting move/delete/
    upload write or operate outside the writable root (e.g. overwriting
    project.db via Working Documents/lnk -> ../project.db). The resolved
    target is still checked to be inside the writable root and the project.
    """
    if not project_path or not os.path.isdir(project_path):
        raise ValueError('project not found')
    if not rel or not rel.strip():
        raise ValueError('path required')
    rel = rel.strip().replace(os.sep, '/')
    if '..' in rel.split('/'):
        raise ValueError('path traversal not allowed')
    if rel.startswith('/') or rel.startswith('\\'):
        raise ValueError('absolute path not allowed')
    for part in rel.split('/'):
        if part and _is_forbidden_path(part):
            raise ValueError(f'forbidden path component: {part}')

    # Reject any existing symlink component along the lexical path
    # (including the final component if it exists). This is the fix for
    # symlink escapes: a symlink in Working Documents pointing outside the
    # writable root must not be operated on via its resolved target.
    parts = rel.split('/')
    for i in range(1, len(parts) + 1):
        prefix = '/'.join(parts[:i])
        lex_abs = os.path.join(project_path, prefix)
        if os.path.lexists(lex_abs) and os.path.islink(lex_abs):
            raise ValueError(f'symlink not allowed: {prefix}')

    # Lexical path (no symlink resolution) — this is what we operate on
    lex_abs = os.path.join(project_path, rel)
    # For containment, resolve the lexical path and verify it is inside both
    # the project and the writable root it claims to be in.
    candidate_real = os.path.realpath(lex_abs)
    root_real = os.path.realpath(project_path)
    if not (candidate_real == root_real or candidate_real.startswith(root_real + os.sep)):
        raise ValueError('path escapes project')
    # Must also be inside the claimed writable variant's real path
    writable_prefix = rel.split('/')[0]
    writable_root_real = os.path.realpath(os.path.join(project_path, writable_prefix))
    # If the writable prefix directory does not yet exist (e.g. first upload
    # creating Working Documents/foo), treat the project root as the bound for
    # the existence check — the lexical writable check (_is_writable_rel) already
    # guarantees the prefix is a valid variant. Only enforce when the prefix dir
    # exists (or its parent does).
    if os.path.isdir(os.path.join(project_path, writable_prefix)):
        if not (candidate_real == writable_root_real or candidate_real.startswith(writable_root_real + os.sep)):
            # Also allow the exact writable root itself (mkdir Working Documents)
            if rel != writable_prefix:
                raise ValueError('path escapes writable root (symlink?)')
    # Re-check forbidden on the resolved basename (covers symlink target names)
    resolved_base = os.path.basename(candidate_real)
    if resolved_base and _is_forbidden_path(resolved_base):
        raise ValueError(f'forbidden resolved path: {resolved_base}')
    return lex_abs


def _writable_root(project_path: str) -> str:
    """Return canonical Working Documents path.
    Prefer 'Working Documents' if exists else first existing variant else create Working Documents.
    """
    if not project_path or not os.path.isdir(project_path):
        raise ValueError('project not found')
    # Prefer canonical
    wd_candidates = ['Working Documents', 'Working Docs', 'My Docs']
    # also consider other variants
    all_candidates = ['Working Documents', 'Working Docs', 'My Docs', 'working-docs', 'docs']
    for name in all_candidates:
        cand = os.path.join(project_path, name)
        if os.path.isdir(cand):
            # If canonical exists, return it
            if name == 'Working Documents':
                return cand
            # otherwise first existing wins unless canonical also exists (checked first)
            # We checked Working Documents first, so if we are here and it's not canonical,
            # it means canonical didn't exist; return this one for read, but for writes
            # spec says prefer Working Documents — we should still use Working Documents
            # as writable root? Task says "prefer 'Working Documents' if exists else first existing variant else create Working Documents"
            # That implies if Working Documents doesn't exist but Working Docs does, use Working Docs.
            return cand
    # None exists -> create Working Documents
    wd = os.path.join(project_path, 'Working Documents')
    os.makedirs(wd, exist_ok=True)
    return wd


def _ensure_parent_dir(abs_path: str):
    d = os.path.dirname(abs_path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


# ── Public ops ──────────────────────────────────────────────────────────────

def mkdir(project_path: str, rel: str) -> str:
    """Create directory at rel. Returns absolute path. Raises ValueError on violation."""
    if not _is_writable_rel(rel):
        raise ValueError('path must be under Working Documents and not forbidden')
    abs_path = _safe_resolve(project_path, rel)
    # Don't allow creating forbidden-named files (already checked)
    os.makedirs(abs_path, exist_ok=True)
    return abs_path


def move(project_path: str, src: str, dst: str) -> str:
    """Move src -> dst. Both must be writable rels. Returns dst abs path."""
    if not _is_writable_rel(src):
        raise ValueError(f'src must be under Working Documents: {src}')
    if not _is_writable_rel(dst):
        raise ValueError(f'dst must be under Working Documents: {dst}')
    src_abs = _safe_resolve(project_path, src)
    dst_abs = _safe_resolve(project_path, dst)
    if not os.path.exists(src_abs):
        raise ValueError(f'src not found: {src}')
    if os.path.exists(dst_abs):
        raise ValueError(f'dst already exists: {dst}')
    _ensure_parent_dir(dst_abs)
    shutil.move(src_abs, dst_abs)
    # Migrate tags: src and any children (src/...) -> dst
    try:
        import agent_db
        tags_map = agent_db.get_file_tags(project_path) or {}
        for rel_path, meta in list(tags_map.items()):
            if rel_path == src or rel_path.startswith(src + '/'):
                suffix = rel_path[len(src):]  # '' or '/child'
                new_rel = dst + suffix
                agent_db.set_file_tag(project_path, new_rel, meta.get('tags') or [], meta.get('note') or '')
                agent_db.delete_file_tag(project_path, rel_path)
                # For folder moves, the parent entry may have been iterated already
                # need to avoid double process for already-moved children — map was snapshot
    except Exception:
        pass
    return dst_abs


def copy(project_path: str, src: str, dst: str) -> str:
    """Copy src -> dst. Both must be writable rels. Returns dst abs path."""
    if not _is_writable_rel(src):
        raise ValueError(f'src must be under Working Documents: {src}')
    if not _is_writable_rel(dst):
        raise ValueError(f'dst must be under Working Documents: {dst}')
    src_abs = _safe_resolve(project_path, src)
    dst_abs = _safe_resolve(project_path, dst)
    if not os.path.exists(src_abs):
        raise ValueError(f'src not found: {src}')
    if os.path.exists(dst_abs):
        raise ValueError(f'dst already exists: {dst}')
    _ensure_parent_dir(dst_abs)
    if os.path.isdir(src_abs):
        shutil.copytree(src_abs, dst_abs)
    else:
        shutil.copy2(src_abs, dst_abs)
    # Copy tags: duplicate entries for src prefix -> dst
    try:
        import agent_db
        tags_map = agent_db.get_file_tags(project_path) or {}
        for rel_path, meta in list(tags_map.items()):
            if rel_path == src or rel_path.startswith(src + '/'):
                suffix = rel_path[len(src):]
                new_rel = dst + suffix
                agent_db.set_file_tag(project_path, new_rel, meta.get('tags') or [], meta.get('note') or '')
    except Exception:
        pass
    return dst_abs


def _project_id_for(project_path):
    """Resolve the project_id for a project path (for the out-of-tree trash root).
    Returns int id or None."""
    try:
        import agent_db
        proj = agent_db.get_project_by_path(project_path)
        return proj.get('id') if proj else None
    except Exception:
        return None


def _bucket_for(project_path):
    """Return (bucket, region, enabled) for a project_path, or (None, None, False)."""
    try:
        import agent_db
        proj = agent_db.get_project_by_path(project_path)
        if not proj:
            return None, None, False
        if proj.get('scw_session_enabled') and proj.get('scw_session_bucket'):
            return (proj['scw_session_bucket'],
                    proj.get('scw_session_region') or 'fr-par', True)
        return None, None, False
    except Exception:
        return None, None, False


def _bucket_key_for_rel(rel):
    """Map a Working-Docs rel to its SCW bucket key (strip the writable prefix)."""
    for pref in _WRITABLE_VARIANTS:
        if rel == pref:
            return ''
        if rel.startswith(pref + '/'):
            return rel[len(pref) + 1:]
    return rel


def _delete_bucket_object(project_path, rel):
    """Best-effort remove a file's mirrored object from the SCW session bucket,
    so a soft-delete actually removes it from the resurrectable mirror."""
    try:
        bucket, region, enabled = _bucket_for(project_path)
        if not enabled:
            return
        key = _bucket_key_for_rel(rel)
        if not key:
            return
        import agent_scw_session
        agent_scw_session.delete_object(bucket, key, region=region)
    except Exception:
        pass


def _sha256_file(abs_path):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(abs_path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
    except Exception:
        return None
    return h.hexdigest()


def _iter_files_under(project_path, rel, src_abs):
    """Yield (existing_rel, abs_path) for every file under rel (recursively if
    rel is a dir; just rel if a file)."""
    if os.path.isdir(src_abs):
        base = os.path.realpath(project_path)
        for root, dirs, files in os.walk(src_abs):
            for f in files:
                full = os.path.join(root, f)
                rel_path = os.path.relpath(full, base)
                yield rel_path.replace(os.sep, '/'), full
    elif os.path.isfile(src_abs):
        yield rel, src_abs


_TOMBSTONE_MIN_BYTES = 64


def _remove_empty_tree(path):
    """Remove `path` and every empty directory beneath it, bottom-up.

    Only `path` itself and its descendants are touched — never its parents, so
    deleting the last file in a folder does not make the folder (or Working
    Documents) disappear. Directories that still contain files are kept."""
    if not os.path.isdir(path) or os.path.islink(path):
        return
    for root, dirs, _files in os.walk(path, topdown=False):
        for d in dirs:
            full = os.path.join(root, d)
            if os.path.islink(full):
                continue
            try:
                os.rmdir(full)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def delete(project_path: str, rels, *, record_tombstone=True, remove_bucket=True) -> list:
    """Soft-delete one or more rels by moving them to the OUT-OF-TREE trash:
    <AINGEL_TRASH_ROOT>/<project_id>/files/<timestamp>/<rel>.

    Moving the trash outside the project tree means agent bash (find, globs,
    ls -la) can no longer discover deleted files and copy them back. Each
    deleted file also records a delete-tombstone (rel + sha256) in the
    project.db, and removes its mirrored SCW bucket object.

    record_tombstone / remove_bucket default True. Set both False for an
    "overwrite" (WebDAV MOVE/COPY Overwrite:T) where the destination is being
    *replaced* by new content — that is not a user deletion, so we must not
    tombstone or bucket-remove it.

    rels may be str or list of str. Returns list of trashed rels.
    Raises ValueError on first violation.
    """
    if isinstance(rels, str):
        rels = [rels]
    if not isinstance(rels, (list, tuple)):
        raise ValueError('paths must be list')
    # B4: normalize to a real, absolute path so project-id lookups always match
    # the DB (a relative path like '.' previously hit the silent in-tree fallback).
    project_path = os.path.realpath(project_path)
    if not os.path.isdir(project_path):
        raise ValueError('project not found')
    import agent_db

    try:
        from agent_config import TRASH_ROOT
    except Exception:
        TRASH_ROOT = None
    pid = _project_id_for(project_path)
    root = project_path

    trashed = []
    ts = time.strftime('%Y%m%d-%H%M%S')
    ts_full = f"{ts}_{int(time.time()*1000)%1000:03d}"

    # Pre-plan: collect all files (recursing dirs) so hashing/tombstones are
    # recorded for every file even inside a deleted folder. Keep the original
    # rel per entry so the tombstone + bucket-key are correct for a folder.
    plan = []  # (orig_rel, existing_rel, src_abs)
    src_dirs = []  # folders the caller asked to delete (removed afterwards, A2)
    for rel in rels:
        rel = (rel or '').strip().replace(os.sep, '/')
        if not rel:
            raise ValueError('empty path')
        if not _is_writable_rel(rel):
            raise ValueError(f'path must be under Working Documents: {rel}')
        src_abs = _safe_resolve(project_path, rel)
        if not os.path.exists(src_abs):
            raise ValueError(f'not found: {rel}')
        if os.path.isdir(src_abs):
            src_dirs.append(src_abs)
            for existing_rel, full in _iter_files_under(project_path, rel, src_abs):
                plan.append((existing_rel, existing_rel, full))
        elif os.path.isfile(src_abs):
            plan.append((rel, rel, src_abs))

    # Trash root: out-of-tree when we have both a configured root AND a project
    # id (production vault). Fall back to an in-tree .trash ONLY for unregistered
    # temp projects (tests) where no central DB entry exists — and even then,
    # fail loudly rather than silently, so a misconfiguration is never hidden.
    trash_base = None
    if TRASH_ROOT and not pid:
        # B4: a vault trash root is configured but this path is not a registered
        # project. Refuse rather than fall back to an in-tree .trash that agent
        # bash can see. The in-tree layout is only used when TRASH_ROOT is
        # explicitly empty (tests / non-vault installs).
        raise RuntimeError(
            f'soft-delete aborted: {project_path!r} is not a registered project '
            '(no project id for the out-of-tree trash)')
    if not TRASH_ROOT:
        tb = os.path.join(project_path, '.trash', 'files', ts_full)
        try:
            os.makedirs(tb, exist_ok=True)
            trash_base = tb
        except Exception as e:
            raise RuntimeError(f'cannot create in-tree trash {tb}: {e}')
    else:
        tb = os.path.join(TRASH_ROOT, str(pid), 'files', ts_full)
        try:
            os.makedirs(tb, exist_ok=True)
            trash_base = tb
        except Exception as e:
            raise RuntimeError(
                f'cannot create trash root {tb}: {e} — soft-delete aborted '
                '(no fallback; a deleted file must stay out of tree)')

    # Move each file and record tombstone + bucket removal.
    for orig_rel, existing_rel, src_abs in plan:
        dst_rel = existing_rel
        dst_abs = os.path.join(trash_base, dst_rel)
        os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
        if os.path.exists(dst_abs):
            dst_abs = dst_abs + f".{int(time.time()*1000)}"
        try:
            st_size = os.path.getsize(src_abs)
        except Exception:
            st_size = 0
        # A3: skip tombstone recording for tiny/empty files so a 64-byte or
        # 0-byte delete can't poison every later PUT of a stub. Only hash when
        # a tombstone will actually be recorded (overwrites skip it).
        record_sha = None
        if record_tombstone and st_size >= _TOMBSTONE_MIN_BYTES:
            record_sha = _sha256_file(src_abs)
        shutil.move(src_abs, dst_abs)
        if record_sha:
            try:
                agent_db.record_delete_tombstone(project_path, orig_rel, record_sha)
            except Exception:
                pass
        if remove_bucket:
            _delete_bucket_object(project_path, orig_rel)
        trashed.append(orig_rel)

    # A2: remove each deleted folder with its (now empty) subfolders, including
    # folders that were empty to begin with. Parents are never touched.
    for d in src_dirs:
        if d != root:
            _remove_empty_tree(d)

    # Remove tags for each deleted path and its children.
    try:
        tags_map = agent_db.get_file_tags(project_path) or {}
        for rel in rels:
            rel = (rel or '').strip().replace(os.sep, '/')
            for trel in list(tags_map.keys()):
                if trel == rel or trel.startswith(rel + '/'):
                    agent_db.delete_file_tag(project_path, trel)
    except Exception:
        pass
    return trashed


def trash_resolved(project_path, rel):
    """Move a file/dir to the out-of-tree trash WITHOUT the writable-root check.

    Used by the tombstone gate to re-trash a resurrected file that may live
    outside a Working-Docs variant (project root, Legal/, Artifacts/, ...) where
    the normal delete() would refuse. Still enforces realpath containment inside
    the project and rejects forbidden names. Records a tombstone. Returns the
    trash rel or raises ValueError.
    """
    project_path = os.path.realpath(project_path)
    root = project_path
    rel = (rel or '').strip().replace(os.sep, '/')
    # Forbidden-path guard (same per-component check as _is_writable_rel, but
    # without requiring a writable root — a resurrection at Legal/ etc. still
    # has to pass, but .git/.env secrets stay blocked).
    for part in rel.split('/'):
        if not part or part in ('.', '..'):
            raise ValueError(f'bad path component: {part!r}')
        if _is_forbidden_path(part):
            raise ValueError(f'forbidden path component: {part!r}')
    if rel.startswith('/') or rel.startswith('\\'):
        raise ValueError('absolute path not allowed')
    abs_path = os.path.join(root, rel)
    real_abs = os.path.realpath(abs_path)
    if not (real_abs == root or real_abs.startswith(root + os.sep)):
        raise ValueError(f'path escapes project: {rel!r}')

    import agent_db
    try:
        from agent_config import TRASH_ROOT
    except Exception:
        TRASH_ROOT = None
    pid = _project_id_for(project_path)
    if not TRASH_ROOT or not pid:
        raise RuntimeError(f'no resolvable vault trash root for {project_path!r}')
    ts_full = f"{time.strftime('%Y%m%d-%H%M%S')}_{int(time.time()*1000)%1000:03d}"
    trash_base = os.path.join(TRASH_ROOT, str(pid), 'files', ts_full)
    os.makedirs(trash_base, exist_ok=True)

    trashed = []
    # Move files under rel (file or dir) into the trash, preserving structure.
    for existing_rel, src_abs in _iter_files_under(project_path, rel, abs_path):
        dst_abs = os.path.join(trash_base, existing_rel)
        os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
        if os.path.exists(dst_abs):
            dst_abs = dst_abs + f".{int(time.time()*1000)}"
        sha = _sha256_file(src_abs)
        shutil.move(src_abs, dst_abs)
        try:
            if sha and os.path.getsize(dst_abs) >= _TOMBSTONE_MIN_BYTES:
                agent_db.record_delete_tombstone(project_path, existing_rel, sha)
        except Exception:
            pass
        _delete_bucket_object(project_path, existing_rel)
        trashed.append(existing_rel)
    # Remove the re-trashed folder's now-empty tree (A2); never its parents.
    if real_abs != root:
        _remove_empty_tree(real_abs)
    if not trashed:
        raise ValueError(f'not found or empty: {rel!r}')
    return trashed


def restore(project_path, rel):
    """Restore a soft-deleted file from the out-of-tree trash back to its
    original Working-Docs location, and clear its delete-tombstone so it can be
    worked on normally again (a deliberate undelete).

    Finds the newest trash entry under
    <AINGEL_TRASH_ROOT>/<project_id>/files/<ts>/<rel>, moves it back to
    project_path/<rel>, and calls clear_delete_tombstone for that rel.
    Returns {'ok': True, 'rel': rel} or raises ValueError on failure."""
    project_path = os.path.realpath(project_path)
    rel = (rel or '').strip().replace(os.sep, '/')
    if not rel:
        raise ValueError('empty path')
    if not _is_writable_rel(rel):
        raise ValueError(f'path must be under Working Documents: {rel}')
    if not os.path.isdir(project_path):
        raise ValueError('project not found')
    import agent_db
    try:
        from agent_config import TRASH_ROOT
    except Exception:
        TRASH_ROOT = None
    pid = _project_id_for(project_path)
    if not TRASH_ROOT or not pid:
        raise ValueError('no vault trash root for this project')

    # Destination: same containment + symlink checks as every other write.
    dest_abs = _safe_resolve(project_path, rel)
    if os.path.exists(dest_abs):
        raise ValueError(f'cannot restore {rel!r}: destination already exists')

    # Newest trash entry holding <rel>. Order by the <ts> directory name (the
    # deletion time): the file's own mtime is preserved by the move into the
    # trash, so it would pick the most recently *edited* version instead.
    files_root = os.path.join(TRASH_ROOT, str(pid), 'files')
    candidates = []
    if os.path.isdir(files_root):
        for ts_dir in os.listdir(files_root):
            full = os.path.join(files_root, ts_dir, rel)
            if os.path.isfile(full) and not os.path.islink(full):
                candidates.append((ts_dir, full))
    if not candidates:
        raise ValueError(f'no trashed copy of {rel!r} to restore')
    candidates.sort(key=lambda x: x[0], reverse=True)
    src = candidates[0][1]

    sha = _sha256_file(src)
    _ensure_parent_dir(dest_abs)
    shutil.move(src, dest_abs)
    # Clear the tombstones so the file can be edited/saved again (C7): by path,
    # and by content hash — the commit gate matches by hash at any path, so a
    # tombstone for the same bytes under another name would re-trash it.
    agent_db.clear_delete_tombstone(project_path, rel_path=rel)
    if sha:
        agent_db.clear_delete_tombstone(project_path, sha256=sha)
    # Clear any tag residue is left untouched; the file is back.
    return {'ok': True, 'rel': rel}
