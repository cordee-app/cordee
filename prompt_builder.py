"""Prompt builder for SuperAgent tasks.

Constructs the full execution prompt and decides whether the request fits in a
single synchronous call (``inline``) or must be processed by splitting a large
referenced file into chunks (``batch``).

Two responsibilities:

1. **Catch referenced files** — when a task description names a file that exists
   in the project's Working Docs folder, its text is injected into the prompt so
   the model actually sees the content (no tool round-trip needed). Files small
   enough to fit the context window are inlined; files too large are routed to
   the chunk-and-aggregate batch path. Binary files (``.db``, ``.docx`` …) are
   noted but not inlined — they are meaningless as raw text.

2. **Choose execution mode** — the real limit of the Scaleway synchronous
   endpoint is the model context window (~131k tokens), *not* any HTTP body cap.
   So a prompt is sent inline whenever it fits the inline token budget; only a
   genuinely oversized file triggers batch mode.
"""

from typing import Dict
import os
import time
import logging
import agent_files

_log = logging.getLogger(__name__)

# Fraction of a model's context window we allow a prompt to fill before chunking.
# The remainder is headroom for tool schemas, role/context blocks, and output tokens.
_INLINE_BUDGET_FRACTION = 0.50

# Extensions whose bytes are not meaningful as UTF-8 text — never inline these.
_BINARY_EXTS = {
    '.pdf', '.db', '.sqlite', '.xlsx', '.xls', '.doc', '.docx', '.pptx',
    '.odt', '.ods', '.odp', '.png', '.jpg', '.jpeg', '.gif', '.zip',
}


def _approx_token_count(text: str) -> int:
    """Approximate token count.

    Uses a conservative 2 chars/token ratio. Polish/Central-European text
    with diacritics tokenises denser than English (~1.7 chars/token measured
    on Scaleway Qwen models for tender documents). Using 2 chars/token for
    the budget check ensures the inline content stays within the context
    window even with worst-case tokenisation.
    """
    return max(0, len(text or "") // 2)


def _extract_docx_text(filepath, max_chars=200_000):
    """Extract text from a .docx file using python-docx.

    Returns the extracted text, or None if extraction fails.
    Caps at max_chars to avoid overwhelming the prompt.
    """
    try:
        import docx
        doc = docx.Document(filepath)
        parts = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                parts.append(text)
        for table in doc.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(' | '.join(cells))
        result = '\n'.join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + '\n...[truncated]'
        return result if result.strip() else None
    except Exception:
        return None


def _extract_xlsx_text(filepath, max_chars=200_000):
    """Extract text from an .xlsx file using openpyxl.

    Returns the extracted text, or None if extraction fails.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    parts.append(' | '.join(cells))
        wb.close()
        result = '\n'.join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + '\n...[truncated]'
        return result if result.strip() else None
    except Exception:
        return None


def _extract_pdf_text(filepath, max_chars=200_000):
    """Extract text from a .pdf file using pdfplumber.

    Returns the extracted text, or None if extraction fails.
    """
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ''
                if text.strip():
                    parts.append(text)
        result = '\n'.join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + '\n...[truncated]'
        return result if result.strip() else None
    except Exception:
        return None


def _extract_pptx_text(filepath, max_chars=200_000):
    """Extract text from a .pptx file using python-pptx.

    Returns the extracted text, or None if extraction fails.
    """
    try:
        from pptx import Presentation
        prs = Presentation(filepath)
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            slide_texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = para.text.strip()
                        if text:
                            slide_texts.append(text)
                if shape.has_table:
                    for row in shape.table.rows:
                        cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                        if cells:
                            slide_texts.append(' | '.join(cells))
            if slide_texts:
                parts.append(f'--- Slide {i} ---\n' + '\n'.join(slide_texts))
        result = '\n'.join(parts)
        if len(result) > max_chars:
            result = result[:max_chars] + '\n...[truncated]'
        return result if result.strip() else None
    except Exception:
        return None


def _extract_doc_text(filepath, max_chars=200_000):
    """Extract text from a legacy .doc (Word 97-2003) file using antiword.

    Returns the extracted text, or None if antiword is not available
    or extraction fails.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ['antiword', filepath],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        result = proc.stdout.strip()
        if not result:
            return None
        if len(result) > max_chars:
            result = result[:max_chars] + '\n...[truncated]'
        return result
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _resolve_referenced_files(task, project_path, inline_budget, single_file_max,
                              catchall_disabled=False):
    """Find Working-Docs files named in the description and classify them.

    ``inline_budget``   – max cumulative prompt tokens to keep inline.
    ``single_file_max`` – a single file larger than this is always chunked.
    ``catchall_disabled`` – when True, the broad catchall triggers ("review all
        files", "in the project folder", …) are ignored. Only files *explicitly*
        named in the description are caught. Used when the task shows specific-
        file access intent (see ``_has_file_access_intent``): the model will read
        files via read_file, so we must NOT pre-inline/extract every file in the
        project (regression: task #10001117 — 54 PDFs extracted, 28-min hang).

    Returns ``(inline, batch)`` where each is a list of ``(filename, content)``:
      * ``inline`` – text small enough to inject directly (``content`` is the
        text, or ``None`` for a binary file that can only be noted).
      * ``batch``  – text too large for one call; becomes the chunk payload.
    """
    import os, re
    inline, batch = [], []
    if not project_path:
        return inline, batch

    desc = task.get('description') or ''
    # A path copied from a Y:/WebDAV address is URL-encoded (`a%20b.pdf`).
    if re.search(r'%[0-9A-Fa-f]{2}', desc):
        from urllib.parse import unquote
        desc = unquote(desc)
    try:
        from agent_tools import _WORKING_DOC_FOLDERS
    except Exception:
        _WORKING_DOC_FOLDERS = ('My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs')

    # Scan every working-doc folder (a project may have more than one, e.g. both
    # "Working Docs" and "Working Documents") plus Artifacts/outputs, where task
    # deliverables (Artifacts/outputs/<task-slug>/) and exec logs that follow-up
    # tasks reference live.
    # The repo root is deliberately NOT scanned: it holds CLAUDE.md and other files a
    # task may mention only to exclude.
    scan_folders = list(_WORKING_DOC_FOLDERS) + [os.path.join('Artifacts', 'outputs')]
    file_paths = {}  # basename -> absolute path (first folder wins on collision)
    for folder in scan_folders:
        wd = os.path.join(project_path, folder)
        for f, full in agent_files.list_files_under(wd, max_depth=3):
            file_paths.setdefault(f, full)
    if not file_paths:
        return inline, batch

    desc_lower = desc.lower()
    _catchall_triggers = ('review the files', 'review all files', 'review the project',
                          'review uploaded files', 'review the documents',
                          'review all documents', 'analyze the files',
                          'analyze all files', 'read the files', 'read all files',
                          'review files in', 'files in the bucket', 'files in the project',
                          'contract files', 'project files', 'source files',
                          'project directory', 'attached files', 'uploaded files',
                          'uploaded documents', 'project documents', 'any attached',
                          'any uploaded', 'source documents', 'reference files',
                          'from the files', 'from the documents', 'from the project',
                          'in the project directory', 'summarize the files',
                          'summarize the documents', 'consolidate', 'extract and summarize',
                          'from the contract', 'review the contract',
                          # broader triggers — natural phrasings
                          'all the tender', 'all the documentation', 'all the documents',
                          'analyze the attached', 'analyze the documentation',
                          'analyze all tender', 'analyze all documentation',
                          'review the attached', 'review all the',
                          'read the documentation', 'read the attached',
                          'tender documentation', 'attached documentation',
                          'the documentation', 'all documentation',
                          'review all', 'analyze all', 'read all',
                          # project folder references
                          'in this project folder', 'in this projet folder',
                          'in the project folder', 'in this project',
                          'in this folder', 'in the folder',
                          'contract in this', 'file in this project',
                          )
    _catchall = (not catchall_disabled) and any(t in desc_lower for t in _catchall_triggers)

    # Lead-token frequency across this scan's files — a naming-convention
    # prefix shared by many files (e.g. "CUPT_", "Task_NNNNN_") is not a
    # distinctive identifier fragment, even though a one-off prefix like
    # "Wyrok-13.07.2026" is. Without this, a project-name word appearing
    # anywhere in the description (or in the task's own required OUTPUT
    # filename) sweeps in every unrelated file sharing that prefix, blowing
    # the inline budget and forcing batch mode (regression: task #10001145 —
    # "CUPT" and "Task" matched 25 unrelated reports via this fallback).
    _LEAD_TOKEN_DISTINCTIVE_MAX = 2
    _lead_counts = {}
    for _fn in file_paths:
        _lead_key = re.split(r'[_\-\s]', os.path.splitext(_fn)[0], 1)[0].lower()
        _lead_counts[_lead_key] = _lead_counts.get(_lead_key, 0) + 1

    def _referenced(fname):
        if _catchall:
            return True
        if fname in desc:
            return True
        stem = os.path.splitext(fname)[0]
        identifierish = any(c in stem for c in '_-') or any(c.isdigit() for c in stem) or any(c.isupper() for c in stem)
        if len(stem) >= 6 and identifierish:
            if re.search(r'(?<!\w)' + re.escape(stem) + r'(?!\w)', desc) is not None:
                return True
            # Partial/prefix reference (e.g. "Wyrok..." for "Wyrok-13.07.2026"):
            # match a distinctive leading token of the stem (up to the first
            # separator/digit boundary) so a truncated name still catches the
            # file — but only when that prefix isn't shared by many other
            # files in the project (see _lead_counts above).
            lead = re.split(r'[_\-\s]', stem, 1)[0]
            if (len(lead) >= 4
                    and _lead_counts.get(lead.lower(), 0) <= _LEAD_TOKEN_DISTINCTIVE_MAX
                    and re.search(r'(?<!\w)' + re.escape(lead) + r'(?!\w)', desc) is not None):
                return True
        return False

    running = _approx_token_count(desc)
    referenced = [f for f in file_paths if _referenced(f)]
    for fname in referenced:
        full = file_paths[fname]
        ext = os.path.splitext(fname)[1].lower()
        _t0 = time.time()
        if ext in _BINARY_EXTS:
            extracted = None
            if ext == '.docx':
                extracted = _extract_docx_text(full)
            elif ext == '.doc':
                extracted = _extract_doc_text(full)
            elif ext == '.xlsx':
                extracted = _extract_xlsx_text(full)
            elif ext == '.pdf':
                extracted = _extract_pdf_text(full)
            elif ext == '.pptx':
                extracted = _extract_pptx_text(full)

            if extracted is not None:
                ftok = _approx_token_count(extracted)
                if ftok > single_file_max or running + ftok > inline_budget:
                    batch.append((fname, extracted))
                else:
                    inline.append((fname, extracted))
                    running += ftok
                _log.info('caught file %s (%.1fs, %d chars)', fname, time.time() - _t0, len(extracted))
                continue
            inline.append((fname, None))
            _log.info('caught binary file %s (%.1fs, not inlined)', fname, time.time() - _t0)
            continue
        try:
            with open(full, 'r', encoding='utf-8', errors='replace') as fh:
                content = fh.read()
        except OSError:
            continue
        ftok = _approx_token_count(content)
        if ftok > single_file_max or running + ftok > inline_budget:
            batch.append((fname, content))
        else:
            inline.append((fname, content))
            running += ftok
        _log.info('caught file %s (%.1fs, %d chars)', fname, time.time() - _t0, len(content))
    return inline, batch


def _has_file_access_intent(desc, file_paths):
    """Detect whether a task description shows specific-file access intent.

    When a task names a *specific* file (one that exists in Working Documents or
    Artifacts/outputs) AND uses a file-access verb (read/validate/check/...),
    the task needs the agentic tool loop (read_file/list_files) — not the
    toolless batch route. Catchall triggers like "all the files in the folder"
    would otherwise force such tasks into batch mode, where the model has no
    tools and can only simulate tool calls as text (tasks #43/#44 on the Vault).

    Returns True when the task should be forced to standard (agentic) mode.
    """
    if not desc or not file_paths:
        return False
    import re, os
    desc_lower = (desc or '').lower()
    _file_access_verbs = (
        'read', 'validate', 'check', 'verify', 'review', 'inspect',
        'compare', 'cross-check', 'cross check', 'open', 'look at',
        'examine', 'assess against', 'compare against',
    )
    if not any(v in desc_lower for v in _file_access_verbs):
        return False
    # Specific filename named in the description: either a real Working Docs
    # file basename, or an output-NN.md pattern (prior execution outputs).
    # Conservative on purpose: forcing agentic mode (forced_agentic) DROPS all
    # pre-inlined file content, so a weak/fuzzy match is more costly than a miss.
    # Ambiguous references (e.g. description says "whoswho" but the file is
    # "whoswho.md") are handled by the H2a pre-run clarification check,
    # which asks the user and appends the exact filename to the description —
    # making the exact basename/stem match below succeed on the re-run.
    for fname in file_paths:
        if fname in desc:
            return True
        stem = os.path.splitext(fname)[0]
        if stem and len(stem) >= 4 and \
                re.search(r'(?<!\w)' + re.escape(stem) + r'(?!\w)', desc) is not None:
            return True
    # output-NN.md / exec-NN-output.md patterns
    if re.search(r'output-\d+\.md|exec-\d+-output\.md', desc_lower):
        return True
    return False


def build(task, project_path, project=None) -> Dict:
    """Construct the prompt and decide execution mode.

    ``project`` (optional) supplies project-level RAG settings (``use_rag``,
    ``project_type``) so legal-intent detection can opt the task into a RAG
    pre-fetch. When omitted (legacy callers), RAG detection degrades gracefully
    to the task's own ``requires_rag`` flag.

    Lane B — Task Context Isolation:
      * task.context_refs is an explicit JSON array of rel_path strings.
      * If non-empty: inject ONLY those files (do NOT run catch-all phrase scan).
      * If empty/None: fallback = capped index of Working Docs (≤5k tokens) +
        context_review_needed flag + footer.

    Returns a dict with keys:
        - ``prompt``        : the base prompt (inlined small files, markers for big ones)
        - ``mode``          : ``"inline"`` or ``"batch"``
        - ``max_tokens``    : output-token budget from model capabilities
        - ``batch_payload`` : concatenated text of oversized files (chunked by the executor)
        - ``rag_block``     : inlined legal context ('' when not applicable)
        - ``rag_provenance``: retrieval provenance dict ({'label','via',...}) or {}
        - ``context_refs``  : normalized explicit refs from task
        - ``used_context_refs``: subset that were readable and injected
        - ``context_review_needed``: True when fallback index was used
    """
    from agent_executor import _build_prompt, DEFAULT_MODEL

    model_id = (task.get('model') or DEFAULT_MODEL).strip()

    try:
        from model_caps import model_capabilities
        caps = model_capabilities(model_id)
    except Exception:
        caps = {}
    ctx = caps.get('context_window', 128_000)
    out_max = caps.get('max_tokens', 8192)
    inline_budget   = int(ctx * _INLINE_BUDGET_FRACTION)
    single_file_max = int(ctx * (_INLINE_BUDGET_FRACTION - 0.10))

    # ── Lane B: explicit context_refs extraction ──────────────────────────────
    raw_refs = task.get('context_refs')
    context_refs = []
    if isinstance(raw_refs, str):
        try:
            import json as _json
            parsed = _json.loads(raw_refs) if raw_refs.strip() else []
            if isinstance(parsed, list):
                context_refs = parsed
        except Exception:
            context_refs = []
    elif isinstance(raw_refs, list):
        context_refs = raw_refs
    else:
        context_refs = []
    # normalize (strip, forward slash, reject absolute/traversal)
    norm_refs = []
    for _r in (context_refs or []):
        if not isinstance(_r, str):
            continue
        _s = _r.strip().replace(os.sep, '/')
        if not _s:
            continue
        if os.path.isabs(_s):
            continue
        if any(p == '..' for p in _s.split('/')):
            continue
        if _s not in norm_refs:
            norm_refs.append(_s)
    context_refs = norm_refs

    # Determine explicit vs fallback
    has_explicit = bool(context_refs)
    context_review_needed = False
    used_context_refs = []
    inline_files, batch_files = [], []
    fallback_index_text = ''
    fallback_footer = ''

    if has_explicit:
        # ── Explicit mode: inject only those files, no catch-all scan ────────
        try:
            from agent_tools import _WORKING_DOC_FOLDERS as _WD_FOLDERS
        except Exception:
            _WD_FOLDERS = ('My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs')
        scan_folders = list(_WD_FOLDERS) + [os.path.join('Artifacts', 'outputs')]
        desc_for_tokens = task.get('description') or ''
        running = _approx_token_count(desc_for_tokens)
        for rel in context_refs:
            rel_norm = rel.strip().replace(os.sep, '/')
            if not rel_norm or os.path.isabs(rel_norm) or '..' in rel_norm.split('/'):
                continue
            # ── Lane B security: forbidden path component ──────────────────
            _blocked = False
            for part in rel_norm.split('/'):
                if agent_files._is_forbidden_path(part):
                    _log.warning('[Lane B] blocked forbidden path component %r in context_ref %r', part, rel_norm)
                    _blocked = True
                    break
            if _blocked:
                continue
            # ── symlink walk on intermediate components ────────────────────
            if project_path:
                parts = rel_norm.split('/')
                for i in range(1, len(parts) + 1):
                    prefix = '/'.join(parts[:i])
                    lex_abs = os.path.join(project_path, prefix)
                    if os.path.lexists(lex_abs) and os.path.islink(lex_abs):
                        _log.warning('[Lane B] blocked symlink component %r in context_ref %r', prefix, rel_norm)
                        _blocked = True
                        break
                if _blocked:
                    continue
                # ── realpath containment + resolved basename forbidden ─────
                try:
                    candidate_real = os.path.realpath(os.path.join(project_path, rel_norm))
                    root_real = os.path.realpath(project_path)
                    if not (candidate_real == root_real or candidate_real.startswith(root_real + os.sep)):
                        _log.warning('[Lane B] blocked path escapes project %r -> %r', rel_norm, candidate_real)
                        continue
                    base = os.path.basename(candidate_real)
                    if base and agent_files._is_forbidden_path(base):
                        _log.warning('[Lane B] blocked forbidden resolved basename %r for %r', base, rel_norm)
                        continue
                except Exception:
                    pass
            full = os.path.join(project_path, rel_norm) if project_path else None
            # Support basename-only refs by searching Working Docs folders
            if project_path and full and not os.path.lexists(full) and '/' not in rel_norm:
                found = None
                for folder in scan_folders:
                    cand = os.path.join(project_path, folder, rel_norm)
                    if os.path.lexists(cand):
                        # ── also validate fallback candidate ──────────────
                        _cand_blocked = False
                        _cand_parts = os.path.join(folder, rel_norm).split('/')
                        for i in range(1, len(_cand_parts) + 1):
                            _pfx = '/'.join(_cand_parts[:i])
                            _lex = os.path.join(project_path, _pfx)
                            if os.path.lexists(_lex) and os.path.islink(_lex):
                                _log.warning('[Lane B] blocked symlink in fallback candidate %r for %r', _pfx, rel_norm)
                                _cand_blocked = True
                                break
                        if _cand_blocked:
                            continue
                        try:
                            _cand_real = os.path.realpath(cand)
                            _root_real = os.path.realpath(project_path)
                            if not (_cand_real == _root_real or _cand_real.startswith(_root_real + os.sep)):
                                _log.warning('[Lane B] blocked fallback escapes project %r -> %r', cand, _cand_real)
                                continue
                            _base = os.path.basename(_cand_real)
                            if _base and agent_files._is_forbidden_path(_base):
                                _log.warning('[Lane B] blocked forbidden fallback basename %r for %r', _base, rel_norm)
                                continue
                        except Exception:
                            pass
                        full = cand
                        found = True
                        break
                if not found:
                    continue
            else:
                # For non-fallback or already-existing full, validate resolved path again
                # (covers case where fallback path was direct and symlink blocked above,
                #  but also re-validates the exact full if it was lexists before)
                if project_path and full:
                    try:
                        # If full was resolved via lex path, re-check its realpath containment
                        # (already checked rel_norm, but full may differ via previous fallback)
                        _full_real = os.path.realpath(full)
                        _root_real2 = os.path.realpath(project_path)
                        if not (_full_real == _root_real2 or _full_real.startswith(_root_real2 + os.sep)):
                            _log.warning('[Lane B] blocked full escapes project %r -> %r', full, _full_real)
                            continue
                        _fbase = os.path.basename(_full_real)
                        if _fbase and agent_files._is_forbidden_path(_fbase):
                            _log.warning('[Lane B] blocked forbidden full basename %r for %r', _fbase, full)
                            continue
                    except Exception:
                        pass
            if not full or not os.path.lexists(full) or not os.access(full, os.R_OK):
                continue
            if not os.path.isfile(full):
                continue
            ext = os.path.splitext(os.path.basename(full))[1].lower()
            display = rel_norm
            _t0 = time.time()
            if ext in _BINARY_EXTS:
                extracted = None
                if ext == '.docx':
                    extracted = _extract_docx_text(full)
                elif ext == '.doc':
                    extracted = _extract_doc_text(full)
                elif ext == '.xlsx':
                    extracted = _extract_xlsx_text(full)
                elif ext == '.pdf':
                    extracted = _extract_pdf_text(full)
                elif ext == '.pptx':
                    extracted = _extract_pptx_text(full)
                if extracted is not None:
                    ftok = _approx_token_count(extracted)
                    if ftok > single_file_max or running + ftok > inline_budget:
                        batch_files.append((display, extracted))
                    else:
                        inline_files.append((display, extracted))
                        running += ftok
                    used_context_refs.append(rel_norm)
                    _log.info('context_refs caught %s (%.1fs, %d chars)', display, time.time() - _t0, len(extracted))
                    continue
                inline_files.append((display, None))
                used_context_refs.append(rel_norm)
                _log.info('context_refs binary %s (%.1fs, not inlined)', display, time.time() - _t0)
                continue
            try:
                with open(full, 'r', encoding='utf-8', errors='replace') as fh:
                    content = fh.read()
            except OSError:
                continue
            ftok = _approx_token_count(content)
            if ftok > single_file_max or running + ftok > inline_budget:
                batch_files.append((display, content))
            else:
                inline_files.append((display, content))
                running += ftok
            used_context_refs.append(rel_norm)
            _log.info('context_refs caught %s (%.1fs, %d chars)', display, time.time() - _t0, len(content))
        context_review_needed = False
        # additions will be built below from inline_files/batch_files
        additions = ''
        for name, content in inline_files:
            if content is None:
                additions += (f"\n\n[Binary file `{name}` is present in Working Docs but cannot be "
                              f"inlined as text. Use a code-execution model to parse it.]\n")
            else:
                additions += f"\n\n**File: {name}**\n```\n{content}\n```\n"
        for name, _content in batch_files:
            additions += f"\n\n[Large file `{name}` is supplied in chunks below.]\n"
        # no forced_agentic listing needed; explicit refs already injected
    else:
        # ── Fallback mode: capped index of Working Docs (≤5k tokens) ─────────
        context_review_needed = True
        used_context_refs = []
        additions = ''
        # collect Working Docs files (reference category) via filecat or manual scan
        fallback_files = []
        total_bytes = 0
        try:
            project_id = task.get('project_id') or (project or {}).get('id')
            if project_id and project_path:
                try:
                    from agent_filecat import list_project_files
                    catalog = list_project_files(project_path, int(project_id))
                    # filter to reference files (Working Docs) or anything under Working Docs folders
                    for f in catalog:
                        if f.get('category') == 'reference' or any(
                            f.get('rel','').startswith(fd + '/') or f.get('rel','') == fd
                            for fd in ('My Docs','Working Docs','Working Documents','working-docs','docs')
                        ):
                            fallback_files.append(f)
                            total_bytes += int(f.get('size',0) or 0)
                except Exception:
                    fallback_files = []
            # if catalog empty or no project_id, manual scan
            if not fallback_files and project_path and os.path.isdir(project_path):
                try:
                    from agent_tools import _WORKING_DOC_FOLDERS as _WD2
                except Exception:
                    _WD2 = ('My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs')
                for folder in list(_WD2):
                    wd = os.path.join(project_path, folder)
                    if not os.path.isdir(wd):
                        continue
                    for root, _, files in os.walk(wd):
                        for fn in sorted(files):
                            if fn.startswith('.'):
                                continue
                            full = os.path.join(root, fn)
                            try:
                                if not os.path.isfile(full):
                                    continue
                                rel = os.path.relpath(full, project_path).replace(os.sep, '/')
                                sz = os.path.getsize(full)
                                fallback_files.append({'rel': rel, 'size': sz, 'modified': ''})
                                total_bytes += sz
                            except Exception:
                                continue
        except Exception:
            fallback_files = []
            total_bytes = 0
        # sort for determinism
        fallback_files.sort(key=lambda x: x.get('rel',''))
        # Build capped index text (≤5k tokens ≈ 10k chars)
        cap_tokens = 5000
        cap_chars = cap_tokens * 2  # conservative 2 chars/token
        lines = []
        running_chars = 0
        for f in fallback_files:
            rel = f.get('rel') or f.get('name') or ''
            sz = f.get('size',0)
            line = f"- {rel} ({sz} bytes)"
            # truncate line if too long
            if len(line) > 200:
                line = line[:200] + '…'
            if running_chars + len(line) + 1 > cap_chars:
                break
            lines.append(line)
            running_chars += len(line) + 1
        truncated = len(lines) < len(fallback_files)
        index_body = "\n".join(lines) if lines else "(no Working Docs files)"
        if truncated:
            index_body += f"\n…[{len(fallback_files) - len(lines)} more files not shown — capped at {cap_tokens} tokens]"
        fallback_footer = f"Working Documents — {len(fallback_files)} files, {total_bytes} bytes (use explicit context_refs for large tasks)"
        additions = f"\n\n**Working Documents Index (fallback, capped at {cap_tokens} tokens)**\n{index_body}\n\n*{fallback_footer}*\n"
        inline_files, batch_files = [], []
        # Even with no context_refs, files explicitly named in the description must
        # reach the model — the capped index can truncate them away. catchall_disabled
        # restricts the catch to exact basename/stem/lead-token mentions only (the
        # catchall path mass-extracts every file: task #10001117 — 54 PDFs, 28-min hang).
        fallback_inline, fallback_batch = _resolve_referenced_files(
            task, project_path, inline_budget, single_file_max, catchall_disabled=True)
        for name, content in fallback_inline:
            if content is None:
                additions += (f"\n\n[Binary file `{name}` is present in Working Docs but cannot be "
                              f"inlined as text. Use a code-execution model to parse it.]\n")
            else:
                additions += f"\n\n**File: {name}**\n```\n{content}\n```\n"
        for name, _content in fallback_batch:
            additions += f"\n\n[Large file `{name}` is supplied in chunks below.]\n"
        fallback_caught = [name for name, _ in fallback_inline] + [name for name, _ in fallback_batch]
        used_context_refs.extend(fallback_caught)
        inline_files.extend(fallback_inline)
        batch_files.extend(fallback_batch)

    # Build task_for_prompt with additions (single assignment)
    task_for_prompt = {**task, 'description': (task.get('description') or '') + additions}

    # 2b. RAG pre-fetch: if the project has RAG enabled and the task has legal
    # intent (or is explicitly flagged requires_rag), retrieve citations from the
    # shared legal library and inline them. Provider-agnostic — works for text-only
    # batch, vibe (Mistral), claude-code (Anthropic) and tool-loop models alike,
    # so a legal task is grounded in the library regardless of which model runs it.
    # Free-tier RAG gate
    _rag_allowed = True
    try:
        import agent_quotas
        _owner = (project or {}).get('owner_id') if isinstance(project, dict) else None
        if _owner:
            _rag_allowed = agent_quotas.can_use_rag(_owner)
    except Exception:
        pass

    rag_block = ''
    rag_provenance = {}
    rag_intent = None
    if _rag_allowed:
        try:
            import agent_rag
            rag_intent = agent_rag.detect_rag_intent(task, project)
            if rag_intent:
                rag_block, rag_provenance = agent_rag.prefetch_citations(rag_intent)
        except Exception:
            rag_intent = None
            rag_block, rag_provenance = '', {}

    # 2c. Block D1 — local rag-index opt-in (host-local /opt/RAG, no SCW).
    # Retrieval is ONLY injected when task has context_refs containing a
    # rag-index file OR description contains "rag:" keyword — never auto-injected.
    # EU-only projects still use local RAG (on-host, no egress), and no US model
    # touches rag-index files (embedding is BGE-M3 host-local).
    local_rag_block = ''
    local_rag_provenance = {}
    if _rag_allowed:
        try:
            import agent_rag as _rag_local
            if _rag_local.should_inject_local_rag(task, project_path):
                local_rag_block, local_rag_provenance = _rag_local.retrieve_local_rag(task, project_path, project)
        except Exception:
            local_rag_block, local_rag_provenance = '', {}

    # 3. Mode: batch only when there is genuinely an oversized file to chunk
    # AND the task does not need file-access tools. Computed before _build_prompt
    # so the prompt can emit a batch-specific constraints block (no "use your
    # tools" — batch mode has no tools).
    mode = 'batch' if batch_files else 'inline'

    # 4. Build the base prompt via the historic implementation.
    prompt = _build_prompt(task_for_prompt, project_path, mode=mode)

    # 4b. Append the retrieved legal context so every model sees it regardless
    # of tool support. Placed after the base prompt so it reads as authoritative
    # context the model must ground on for statute/regulation questions.
    if rag_block:
        prompt = prompt.rstrip() + '\n\n---\n\n' + rag_block
    # 4c. Append local rag-index context (Block D1, host-local /opt/RAG).
    # Only when should_inject_local_rag() is True — never auto-injected.
    if local_rag_block:
        prompt = prompt.rstrip() + '\n\n---\n\n' + local_rag_block

    # 5. Assemble the chunk payload from oversized files.
    batch_payload = ''
    for name, content in batch_files:
        batch_payload += f"\n\n===== FILE: {name} =====\n{content}\n"

    # 5b. Dynamically cap max_tokens so prompt + output fits the context window.
    # The token count from _approx_token_count is rough (chars // 4); add a
    # safety margin to account for tokeniser rounding and system overhead.
    # Note: batch_payload is sent in separate chunked calls, not in the initial
    # prompt, so it is NOT included in the prompt_tokens calculation here.
    prompt_tokens = _approx_token_count(prompt)
    safety_margin = 500  # tokens reserved for tokeniser rounding + system tokens
    # For small-context models (e.g. a 4096-token custom fine-tune) a flat 2048
    # output floor can overflow the window on its own, so the floor scales with
    # the context instead of being hardcoded.
    out_floor = min(2048, max(256, ctx // 8))
    dynamic_max = max(out_floor, ctx - prompt_tokens - safety_margin)
    if dynamic_max < out_max:
        out_max = dynamic_max

    # 6. Phase 7 + Lane B: expose what was caught so the AIngel overseer can
    # compare against files referenced in the description and detect misses /
    # binary mismatches before the model is called.
    # Lane B: caught_files is the set of explicit refs that were readable and
    # injected (inline or batch). In fallback, it holds description-named files
    # caught via _resolve_referenced_files (empty when nothing was named).
    caught_files = list(used_context_refs) if used_context_refs else []
    noted_binary = [name for name, content in inline_files if content is None]

    # RAG label for the UI badge — derived from the prefetch result and intent.
    # Block D1: local rag-index has its own provenance/label (RAG-LOCAL).
    rag_label = None
    try:
        import agent_rag
        if rag_intent:
            rag_label = agent_rag.provenance_label(task, project, bool(rag_block))
        if local_rag_block:
            # Prefer local label when local RAG injected (otherwise keep factory label)
            rag_label = 'RAG-LOCAL'
            # Merge provenance for UI
            if local_rag_provenance:
                rag_provenance = {**rag_provenance, 'local_rag': local_rag_provenance}
    except Exception:
        rag_label = None
    # Expose merged block: factory + local (for UI/debug)
    if local_rag_block:
        # Use combined block for display; keep rag_block as combined so callers see injected content
        rag_block = (rag_block + '\n\n---\n\n' + local_rag_block) if rag_block else local_rag_block
        if local_rag_provenance and 'local_rag' not in rag_provenance:
            rag_provenance['local_rag'] = local_rag_provenance

    return {
        'prompt': prompt,
        'mode': mode,
        'max_tokens': out_max,
        'batch_payload': batch_payload,
        'caught_files': caught_files,
        'noted_binary': noted_binary,
        'rag_block': rag_block,
        'rag_provenance': rag_provenance,
        'rag_label': rag_label,
        # Lane B isolation fields
        'context_refs': context_refs,
        'used_context_refs': used_context_refs,
        'context_review_needed': context_review_needed,
        'fallback_footer': fallback_footer if not has_explicit else '',
    }
