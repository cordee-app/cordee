"""AIngel Autopilot — Phase 7 overseer.

Three medium-AI hooks that give AIngel real oversight of the task lifecycle,
each using the project's existing `aingel_model` (Mistral Medium, Sonnet, …)
with a strictly limited context (~3-5k input tokens). The full task prompt
(which can hit 30k+ tokens) is NEVER sent here — only a digest.

Hooks
-----
* ``pre_run_check``         (H2) — on confirm, before route(). Combines the
  strategic pre-run brief with a prompt-completeness check (was every file
  the description references actually inlined? can the model reach the
  outside-project paths it mentions?). Returns a JSON gate decision.
* ``post_run_analysis``     (H3) — after execution done. Replaces the old
  Haiku 2-sentence brief with a structured JSON analysis: severity, findings,
  recommendation, gate_for_next. If severity != 'ok', the caller posts the
  finding to the project's AIngel chat for discussion.
* ``refresh_overview``      (H4) — after H3. Rewrites
  ``Artifacts/aingel-overview.md`` from the last 5 briefs + phase list +
  open risks.

All hooks are optional — they are only called when
``projects.aingel_autopilot = 1``. The model is always the project's
``aingel_model``; if no model is configured, the hooks no-op.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional

import logging
_log = logging.getLogger('agent_overseer')

# Hard cap on the output snippet we send to H3. The full output lives in
# Artifacts/outputs/exec-<id>-output.md; we only send the first N chars to
# keep the overseer's context under ~5k tokens.
_OUTPUT_SNIPPET_CHARS = 4000
_OVERVIEW_MAX_CHARS = 8000  # ~2k words


def _ask(model: str, prompt: str, want_tokens: int, project_path: str, caller: str,
         expect_json: bool = True):
    """Call the overseer's model, sized for the model rather than for the answer.

    Every hook here asks for a short structured reply and used to budget for the
    *answer* — 800 tokens for H2, 900 for H3. On a reasoning model the thinking is
    billed against that same allowance and exhausts it first: measured on
    ``scw-qwen3.6-35b``, 9 of 12 calls at 800 tokens returned ``content: None``
    with ``finish_reason: length``. Task 695 ran with no oversight at all because
    of it, and nothing said so.

    Two defences, because the first alone is not enough. ``effective_max_tokens``
    raises the floor for models *known* to reason; the retry then covers models
    that are not in ``model_caps.json``, newly added, or simply having a verbose
    run — the failure is probabilistic, so a static floor cannot be sufficient.

    Returns ``(text, degraded_reason)``. ``degraded_reason`` is None on success and
    a short human-readable string otherwise, so callers can report a hook that
    produced nothing instead of silently passing.
    """
    from agent_router import route
    from model_caps import effective_max_tokens

    def _ok(t):
        if not t or not t.strip():
            return False
        return _safe_json(t) is not None if expect_json else True

    budget = effective_max_tokens(model, want_tokens)
    last_err = None
    # Second attempt doubles the budget: an empty or unparseable reply from a
    # model that answered the same prompt a moment ago is nearly always truncation.
    for attempt, mt in enumerate((budget, min(budget * 2, 8192)), start=1):
        try:
            # Force the plain API route — never an agentic one. This is a lightweight
            # check, not a task run: an agentic route has real file/bash access and can
            # act on the project instead of returning JSON (task #465, where H2 wrote a
            # 1281-line document via Mistral Vibe).
            #
            # The two force_* flags only cover Anthropic and Mistral. For Scaleway it is
            # the *presence of project_path* that switches on the tool loop, so passing
            # it here re-enabled the very behaviour those flags exist to prevent —
            # observed live, H2 answering with `→ list_files` / `→ read_file` instead of
            # JSON. `policy_path` keeps the EU boundary resolving against the owning
            # project while tools stay off, the same pattern run_large_file_batch uses.
            text, _ti, _to, _c = route(model, prompt, mt, project_path=None,
                                       policy_path=project_path,
                                       force_anthropic_mode='api', force_mistral_mode='api',
                                       caller=caller)
        except Exception as e:
            _log.warning('[%s] route failed (attempt %d, max_tokens=%d): %s',
                         caller, attempt, mt, e)
            return None, f'route failed: {e}'
        if _ok(text):
            if attempt > 1:
                _log.info('[%s] recovered on retry at max_tokens=%d', caller, mt)
            return text, None
        last_err = ('empty response' if not (text or '').strip()
                    else 'reply was not parseable JSON')
        _log.warning('[%s] %s at max_tokens=%d (attempt %d): %s',
                     caller, last_err, mt, attempt, (text or '')[:200])
        if mt >= 8192:
            break
    return None, f'{last_err} after 2 attempts (model={model})'


def _safe_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract the first {...} JSON object from a model response."""
    if not text:
        return None
    # Strip markdown code fences if present
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.S)
    if m:
        text = m.group(1)
    # Otherwise find the first balanced { ... } block
    start = text.find('{')
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


def _looks_like_url_or_email(token: str) -> bool:
    """True if `token` is a URL, URL fragment, email address, or email domain
    — NOT a filesystem path. The H2 referenced-files extractor used to feed
    these into the containment check, producing false-positive "referenced
    path outside project folder" gates on tasks that merely mentioned a
    website or an email address (e.g. `//www.example.com` from
    `https://www.example.com`, `@example.com`, `user@example.org`).
    """
    t = token.strip()
    if not t:
        return True
    # URL scheme prefix.
    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', t):
        return True
    # URL fragment left after a scheme is stripped: //host/path
    if t.startswith('//'):
        return True
    # www. / ftp. style bare hostnames (no path separator, has a dot) — treat
    # `www.example.com` as a URL, not a file.
    if re.match(r'^(www|ftp)\.', t, re.IGNORECASE):
        return True
    # Email address (user@domain) or bare @domain token.
    if t.startswith('@') or re.search(r'@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', t):
        return True
    # Bare host.tld (no path separator, no scheme) — e.g. `example.com`,
    # `example-group.cz`, `rynek-kolejowy.pl`, `cupt.gov.pl`. The filename
    # regex's stem filter already excludes stems with no `_`/digit/uppercase,
    # but hyphenated stems (`rynek-kolejowy`) pass it and reach this backstop.
    # A bare token ending in a known TLD, with no `/` path separator, is a
    # domain — not a file. This catches the task #10000699 false positives
    # (`cupt.gov.pl`, `rynek-kolejowy.pl`, `transinfo.pl`) that the earlier
    # fix (commit 1be8eae) missed because their stems contain `-`.
    if '/' not in t and re.match(r'^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?'
                                r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$',
                                t):
        # Two-segment TLDs (.gov.pl, .co.uk) and single TLDs (.pl, .com):
        # split off the last label; if it (or the last two) is a known TLD,
        # treat the whole token as a domain. We keep this conservative —
        # only treat as URL if there are ≥2 dots OR the single TLD is one
        # of the common internet TLDs, so a real file like `notes.md` or
        # `data.csv` is NOT misclassified.
        labels = t.split('.')
        tld = labels[-1].lower()
        # Common internet TLDs + country codes likely to appear in task
        # descriptions. Files use extensions like .md, .txt, .py, .csv,
        # .json, .log, .pdf, .sh, .db — none of which are in this set.
        internet_tlds = {
            'com', 'org', 'net', 'io', 'gov', 'edu', 'mil', 'int', 'eu',
            'info', 'biz', 'co', 'pl', 'cz', 'de', 'fr', 'uk', 'sk', 'at',
            'nl', 'be', 'es', 'it', 'se', 'no', 'fi', 'dk', 'pt', 'ch',
            'ro', 'hu', 'gr', 'ie', 'lt', 'lv', 'ee', 'si', 'hr', 'bg',
            'lu', 'mt', 'cy', 'sk', 'ua', 'ru', 'us', 'ca', 'au', 'jp',
            'kr', 'cn', 'in', 'br', 'mx', 'za', 'tv', 'me', 'xyz', 'ai',
            'app', 'dev', 'cloud', 'site', 'online', 'store', 'tech',
        }
        # Multi-label TLDs (gov.pl, co.uk) — check the last two labels.
        if len(labels) >= 3 and f'{labels[-2].lower()}.{tld}' in {
            'gov.pl', 'com.pl', 'co.uk', 'gov.uk', 'ac.uk', 'co.jp',
            'gov.cz', 'com.au', 'gov.au', 'co.kr',
        }:
            return True
        if len(labels) >= 2 and tld in internet_tlds:
            return True
    return False


_URL_ESCAPE_RE = re.compile(r'%[0-9A-Fa-f]{2}')


def _decode_url_escapes(text: str) -> str:
    """Percent-decode text that contains %XX escapes; leave other text as is."""
    if not text or not _URL_ESCAPE_RE.search(text):
        return text
    from urllib.parse import unquote
    return unquote(text)


def _extract_referenced_files(description: str) -> List[str]:
    """Pull file-like tokens out of the task description.

    Catches: `foo.txt`, "foo.json", 'foo.pdf', /path/to/foo.py, FULL_TEXT_OCR.txt,
    file.md, etc.  Used by H2 to compare against the prompt builder's
    `caught_files` list and detect references that were not inlined.

    Excludes URLs, URL fragments, email addresses, and bare @domain tokens —
    these are not filesystem paths and previously caused false-positive
    "containment breach" gates (e.g. `//www.example.com`, `@example.com`,
    `user@example.org`). See `_looks_like_url_or_email`.
    """
    if not description:
        return []
    # URL-encoded paths (copied from a Y:/WebDAV address, e.g.
    # `Doświadczenia%20z%20niemieckiego%20rynku.pdf`) name the file on disk
    # only once decoded; undecoded they also yield fake refs like `20rynku.pdf`.
    description = _decode_url_escapes(description)
    refs = set()
    # Backtick-quoted: `foo.txt` or `/path/to/foo.py`
    for m in re.findall(r'`([^`]+\.[a-zA-Z0-9]{2,5})`', description):
        if not _looks_like_url_or_email(m):
            refs.add(m)
    # Double-quoted: "foo.json"
    for m in re.findall(r'"([^"]+\.[a-zA-Z0-9]{2,5})"', description):
        if not _looks_like_url_or_email(m):
            refs.add(m)
    # Single-quoted: 'foo.json' or 'Working Documents/CUPT/II Etap/foo.pdf'
    for m in re.findall(r"'([^']+\.[a-zA-Z0-9]{2,5})'", description):
        if not _looks_like_url_or_email(m):
            refs.add(m)
    # Bare paths: /foo/bar/baz.ext  or  ./foo/bar.ext
    for m in re.findall(r'(?<![\w/])(\.{0,2}/[\w./\-]+\.[a-zA-Z0-9]{2,5})', description):
        if not _looks_like_url_or_email(m):
            refs.add(m)
    # Bare filenames: word chars + .ext  (only if the stem looks identifier-ish
    # to avoid catching English prose like "this is")
    for m in re.findall(r'(?<![\w./\-])([A-Za-z0-9_\-./]{4,}\.[a-zA-Z0-9]{2,5})\b', description):
        if _looks_like_url_or_email(m):
            continue
        stem = os.path.splitext(m)[0]
        if any(c in stem for c in '_-') or any(c.isdigit() for c in stem) or any(c.isupper() for c in stem):
            refs.add(m)
    return sorted(refs)


# A file the task is told to CREATE is not a missing input. The verb must sit
# right before the filename with no "of/from/using/read..." in between, so
# "Create a summary of `x.pdf`" still treats x.pdf as an input.
_OUTPUT_VERB_RE = re.compile(
    r'\b(?:save|saved|saving|write|writing|create|created|creating|produce|'
    r'generate|generated|export|store)\b(?P<gap>[^\n`"\']{0,60})$', re.I)
_INPUT_WORD_RE = re.compile(
    r'\b(?:of|from|using|based on|read|open|extract|analy[sz]e|review|'
    r'summari[sz]e|compare|parse|load|consult)\b', re.I)


def _output_target_refs(description: str, refs: List[str]) -> set:
    """Subset of ``refs`` that the description instructs the task to create.

    Regression: task 10001148 ("Save as `ALL-RAIL_PressRelease_2026-09-25.md`")
    and task 10001139 were held by H2 because the file they were asked to
    produce did not exist yet.
    """
    targets = set()
    desc = description or ''
    for ref in refs or []:
        pos = 0
        while True:
            i = desc.find(ref, pos)
            if i < 0:
                break
            pos = i + len(ref)
            line_start = desc.rfind('\n', 0, i) + 1
            # Drop the quote/backtick that opens the reference itself.
            before = desc[line_start:i].rstrip('`"\'')[-90:]
            m = _OUTPUT_VERB_RE.search(before)
            if m and not _INPUT_WORD_RE.search(m.group('gap')):
                targets.add(ref)
                break
    return targets


def _scan_folders() -> List[str]:
    """Folders prompt_builder scans for referenced files, in its priority order."""
    try:
        from agent_tools import _WORKING_DOC_FOLDERS
    except Exception:
        _WORKING_DOC_FOLDERS = ('My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs')
    return list(_WORKING_DOC_FOLDERS) + [os.path.join('Artifacts', 'outputs')]


def _resolve_referenced_path(path: str, project_path: str) -> Optional[str]:
    """Resolve `path` to a file on disk, or None.

    Tries, in order: the absolute path, the path relative to project_path, and —
    only for *bare* filenames — the same name inside any Working-Docs folder or
    Artifacts/outputs. That last fallback mirrors prompt_builder's basename
    matching, without which H2 reports false "missing" for files that live in a
    Working Docs subfolder (e.g. `My Docs/foo.md`) rather than the project root.

    A path-qualified reference (`src/config.json`) is NOT satisfied by a
    same-named file elsewhere (`docs/config.json`) — that produced a false
    "exists" verdict telling the model to self-serve a file that isn't there.
    See `_find_by_basename` for reporting the near-miss.
    """
    if not path:
        return None
    if os.path.isabs(path):
        return path if os.path.exists(path) else None
    direct = os.path.join(project_path, path)
    if os.path.exists(direct):
        return direct
    if os.path.dirname(path):
        return None
    for folder in _scan_folders():
        candidate = os.path.join(project_path, folder, path)
        if os.path.exists(candidate):
            return candidate
    # Same depth prompt_builder matches basenames at — task outputs live one
    # level down, in Artifacts/outputs/<task-slug>/.
    import agent_files
    for folder in _scan_folders():
        for name, full in agent_files.list_files_under(os.path.join(project_path, folder), max_depth=3):
            if name == path:
                return full
    return None


def _find_by_basename(path: str, project_path: str) -> Optional[str]:
    """Project-relative location of a same-named file elsewhere, or None.

    Used to turn an unresolvable path-qualified reference into an actionable
    reason ("no file at src/config.json, but docs/config.json exists").
    """
    if not path or os.path.isabs(path) or not os.path.dirname(path):
        return None
    basename = os.path.basename(path)
    for folder in _scan_folders():
        if os.path.exists(os.path.join(project_path, folder, basename)):
            return os.path.join(folder, basename)
    return None


def _scan_for_blocked_tools(output_text: str) -> List[str]:
    """Detect 'Bash(...) blocked' style notes in claude-code output.

    Matches patterns like:
        Bash(find /home/...); Bash(ls /opt/agent ...)
    Returns a list of the blocked tool signatures found.
    """
    if not output_text:
        return []
    # The exec-676 pattern: "Bash(find ...); Bash(ls ...); Bash(python3 -c ...)"
    # Also catches "tool calls were blocked by the permission system"
    hits = []
    for m in re.findall(r'(Bash\([^)]{0,120}\))', output_text):
        hits.append(m)
    if 'blocked by the permission system' in output_text and not hits:
        hits.append('permission-system-block')
    return hits[:10]  # cap to keep H3 input small


# ── H2 — Pre-run brief + completeness check (merged) ──────────────────────────

def pre_run_check(task: Dict[str, Any],
                  project: Dict[str, Any],
                  caught_files: List[str],
                  noted_binary: List[str],
                  model_caps: Dict[str, Any],
                  dep_titles: List[str],
                  budget_snapshot: Optional[Dict[str, Any]] = None,
                  dep_outcomes: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One medium-AI call that returns both strategic brief + completeness check.

    Returns a dict with keys:
        completeness        — 'complete' | 'partial' | 'incomplete'
        missing_files       — list of {name, exists, reason}
        binary_unparsable   — list of {name, model_can_parse, reason}
        capability_warnings — list of strings
        dep_blockers        — list of {task_id, title, gate_for_next, severity, brief_2s}
                              for dependencies whose H3 gate_for_next is hold/skip
        strategic_advice    — 1-3 bullet string
        gate                — 'run' | 'hold' | 'skip'
        reason              — one sentence
    On failure, returns a permissive default (gate='run', no warnings).
    """
    aingel_model = (project or {}).get('aingel_model') or ''
    project_path = (project or {}).get('path') or ''
    if not aingel_model:
        return _permissive_h2()

    referenced = _extract_referenced_files(task.get('description') or '')
    # Distinguish caught (inlined/batched) from missing
    caught_set = set(caught_files or [])
    file_access = (model_caps or {}).get('file_access', 'none')
    agentic = file_access in ('native', 'bash', 'tools')
    missing = []
    self_serve = []   # referenced files an agentic model can read itself
    output_targets = _output_target_refs(task.get('description') or '', referenced)
    output_expected = []   # files this task is told to create (absence is expected)
    for ref in referenced:
        # Normalise: prompt_builder catches by basename; references may be paths
        basename = os.path.basename(ref)
        if basename in caught_set or ref in caught_set:
            continue
        exists = _resolve_referenced_path(ref, project_path) is not None
        # A path-qualified ref that doesn't resolve may still name a file that
        # exists elsewhere — say so, so the hold is actionable.
        elsewhere = None if exists else _find_by_basename(ref, project_path)
        near_miss = (f' (no file at `{ref}`, but `{elsewhere}` exists — '
                     'confirm the intended path)') if elsewhere else ''
        # Outside-project paths are a containment concern regardless of route.
        # An agentic model must not reach outside the project folder.
        in_project = False
        try:
            rp = os.path.realpath(ref if os.path.isabs(ref)
                                  else os.path.join(project_path, ref))
            in_project = rp.startswith(os.path.realpath(project_path) + os.sep) \
                         or rp == os.path.realpath(project_path)
        except Exception:
            in_project = False
        if not in_project:
            # Outside-project reference: always flag, never auto-serve.
            missing.append({
                'name': ref,
                'exists': exists,
                'reason': 'referenced path is OUTSIDE the project folder — '
                          'must not be read; rewrite the task or move the file into the project'
            })
            continue
        if not exists and ref in output_targets:
            # Checked after the containment test, so an output path outside the
            # project is still flagged above.
            output_expected.append(ref)
            continue
        if agentic:
            # Agentic route: model can read_file itself. Track for H3 verification.
            self_serve.append({
                'name': ref,
                'exists': exists,
                'reason': 'agentic model can self-serve via read_file; '
                          'H3 will verify it was actually read'
                          if exists else
                          'referenced file does not exist in project — model must report the gap, '
                          'not fabricate content' + near_miss
            })
        else:
            # Text-only route: not inlined = model truly won't see the content.
            missing.append({
                'name': ref,
                'exists': exists,
                'reason': ('referenced in description but not auto-inlined; '
                           'model will not see content' + near_miss)
                          if not exists else
                          'exists on disk but outside Working Docs scan scope; '
                          'text-only model cannot self-serve'
            })

    # Capability cross-check
    binary_unparsable = []
    capability_warnings = []
    vision = (model_caps or {}).get('vision', False)
    for bn in (noted_binary or []):
        ext = os.path.splitext(bn)[1].lower()
        if ext in ('.pdf', '.png', '.jpg', '.jpeg', '.gif') and not vision:
            binary_unparsable.append({
                'name': bn,
                'model_can_parse': False,
                'reason': f'model has no vision capability; {ext} file cannot be read'
            })
        elif ext in ('.db', '.sqlite', '.xlsx', '.xls', '.docx', '.pptx'):
            binary_unparsable.append({
                'name': bn,
                'model_can_parse': False,
                'reason': f'{ext} file requires code execution to parse; text-only note will be inlined'
            })
    # Outside-project references are unreachable to every route (containment).
    for m in missing:
        if 'OUTSIDE the project folder' in (m.get('reason') or ''):
            capability_warnings.append(
                f"referenced path {m['name']} is outside the project folder — "
                "model must not read it; task needs rewriting or the file moved into the project")
        elif m['exists'] and file_access == 'none':
            capability_warnings.append(
                f"text-only model; referenced path {m['name']} is outside Working Docs and unreachable")

    # Phase 7.1: compute dep_blockers from dependency H3 outcomes.
    dep_blockers = []
    for d in (dep_outcomes or []):
        gfn = (d.get('gate_for_next') or 'proceed').lower()
        if gfn in ('hold', 'skip'):
            dep_blockers.append({
                'task_id': d['task_id'],
                'title': d.get('title', ''),
                'gate_for_next': gfn,
                'severity': d.get('severity', 'ok'),
                'brief_2s': d.get('brief_2s', ''),
            })
    if dep_blockers:
        capability_warnings.append(
            f'{len(dep_blockers)} dependency task(s) have gate_for_next=hold/skip — '
            'proceeding may be risky. Review each dep before running.')

    # Build the prompt — strict JSON schema, ≤4k tokens input
    slot_labels = {1: 'Claude Pro', 2: 'Mistral Pro', 3: 'PAYG', 4: 'EU Scaleway', 5: 'Ollama Cloud', None: 'Unassigned'}
    slot_label = slot_labels.get(task.get('work_session_slot'), 'Unassigned')
    budget_pct = None
    _budget = (budget_snapshot or {}).get('budget_monthly',
               (budget_snapshot or {}).get('monthly_budget'))
    if _budget:
        spend = budget_snapshot.get('current_month_spend') or 0
        budget_pct = round(spend / _budget * 100, 1)

    dep_block = ', '.join(dep_titles[:8]) if dep_titles else '(none)'
    missing_block = json.dumps(missing[:6], ensure_ascii=False) if missing else '[]'
    self_serve_block = json.dumps(self_serve[:8], ensure_ascii=False) if self_serve else '[]'
    binary_block = json.dumps(binary_unparsable[:6], ensure_ascii=False) if binary_unparsable else '[]'
    caps_block = json.dumps({'file_access': file_access, 'vision': vision,
                             'tools': (model_caps or {}).get('tools', False),
                             'context_window': (model_caps or {}).get('context_window', 0)})

    # Build dep outcomes block for the prompt (up to 6 deps, summary only)
    dep_outcomes_block = ''
    if dep_outcomes:
        lines = []
        for d in dep_outcomes[:6]:
            lines.append(
                f"  - #{d.get('task_id')} ({d.get('severity','ok')}, "
                f"gate_for_next={d.get('gate_for_next','proceed')}): "
                f"{d.get('brief_2s','')}"[:200]
            )
        if lines:
            dep_outcomes_block = 'Dependency outcomes:\n' + '\n'.join(lines) + '\n'

    desc = (task.get('description') or '').strip()
    if len(desc) > 2500:
        desc = desc[:2500] + '…[truncated]'

    prompt = (
        f"You are the Guide (project overseer) for the project \"{project.get('name','')}\". "
        f"A task is about to run. In ONE response, return JSON ONLY (no prose) with this schema:\n"
        f"{{\"completeness\": \"complete|partial|incomplete\", "
        f"\"missing_files\": [{{\"name\": str, \"reason\": str}}], "
        f"\"binary_unparsable\": [{{\"name\": str, \"reason\": str}}], "
        f"\"capability_warnings\": [str], "
        f"\"strategic_advice\": str, "
        f"\"gate\": \"run|hold|skip\", "
        f"\"reason\": str}}\n\n"
        f"Task #{task.get('id')}: {task.get('title','')}\n"
        f"Phase: {task.get('phase_name','n/a')}\n"
        f"Model assigned: {task.get('model','n/a')} (column: {slot_label})\n"
        f"Stale dependencies: {'yes' if task.get('stale') else 'no'}\n"
        f"Budget used: {budget_pct}%" if budget_pct is not None else "Budget used: n/a")
    prompt += f"\nDepends on: {dep_block}\n"
    prompt += dep_outcomes_block
    prompt += f"Model capabilities: {caps_block}\n"
    prompt += f"Files auto-caught (inlined/batched): {json.dumps(caught_files[:12])}\n"
    prompt += f"Binary files noted (not inlined): {json.dumps(noted_binary[:6])}\n"
    prompt += f"Referenced-but-missing analysis: {missing_block}\n"
    prompt += f"Self-serve files (agentic model can read_file itself; H3 verifies): {self_serve_block}\n"
    if output_expected:
        prompt += (
            "Output files this task is instructed to CREATE (not existing yet is EXPECTED — "
            "not missing, never a reason to hold, and the model has write access): "
            f"{json.dumps(output_expected[:6], ensure_ascii=False)}\n"
        )
    prompt += f"Binary unparsable analysis: {binary_block}\n"
    # RAG intent: does this task need the shared legal library?
    try:
        import agent_rag as _rag
        _rag_intent = _rag.detect_rag_intent(task, project)
        if _rag_intent:
            prompt += (
                "RAG: this task requires the shared legal library (corpus="
                f"{_rag_intent.get('corpus_id')}, reason={_rag_intent.get('reason')}). "
                "It MUST be grounded on retrieved citations (Dz.U./CELEX/ELI). "
                "If the library is unavailable (empty corpus / service down), set gate=hold.\n"
            )
    except Exception:
        pass
    prompt += f"Description:\n{desc}\n\n"
    prompt += (
        "Rules for `gate`:\n"
        "- run: every referenced file is inlined, OR the route is agentic and the file is inside the project (self-serve); no blocking binary mismatches.\n"
        "- hold: partial — minor warnings the user should see but can override (e.g. agentic self-serve of a file that may not exist).\n"
        "- skip: incomplete OR a binary file is unparsable by this model OR a referenced path is OUTSIDE the project folder (containment breach) OR a capability mismatch makes the task unreachable as written.\n"
        "`strategic_advice`: 1-3 short bullets on model fit + risks (one string, use ' • ' as separator).\n"
        "`reason`: ONE sentence summarising the gate decision.\n"
        "Return ONLY the JSON object."
    )

    text, degraded = _ask(aingel_model, prompt, 800, project_path, 'autopilot:H2')
    if degraded:
        _log.warning('[overseer H2] task %s: %s', task.get('id'), degraded)
        return _permissive_h2(degraded)

    parsed = _safe_json(text)
    if not parsed:
        _log.warning('[overseer H2] task %s: could not parse JSON: %s', task.get('id'), (text or '')[:200])
        return _permissive_h2('reply was not parseable JSON')
    # Merge our deterministic checks with the model's analysis — the model may
    # have missed a binary mismatch we caught programmatically.
    if binary_unparsable and not parsed.get('binary_unparsable'):
        parsed['binary_unparsable'] = binary_unparsable
    if capability_warnings:
        parsed.setdefault('capability_warnings', [])
        parsed['capability_warnings'] = list(set(parsed['capability_warnings'] + capability_warnings))
    parsed.setdefault('gate', 'run')
    parsed.setdefault('reason', '')
    parsed.setdefault('strategic_advice', '')
    parsed.setdefault('completeness', 'complete')
    parsed['dep_blockers'] = dep_blockers
    parsed['self_serve'] = self_serve
    parsed['output_targets'] = output_expected
    # Deterministic backstop: a hold whose only cited cause is an output file
    # that has not been created yet is a false positive. Dependency warnings
    # stay in capability_warnings/dep_blockers; they never forced a hold here.
    if output_expected and parsed.get('gate') == 'hold':
        _reason_l = (parsed.get('reason') or '').lower()
        _cites_output = any(os.path.basename(t).lower() in _reason_l for t in output_expected)
        _other = bool(missing or binary_unparsable or any(not x.get('exists') for x in self_serve))
        if _cites_output and not _other:
            parsed['gate'] = 'run'
            parsed['reason'] = (
                f"{', '.join(os.path.basename(t) for t in output_expected[:3])} is this task's "
                "own output and will be created; no blocking issue found")
    # Force skip on containment breaches regardless of model opinion.
    if any('OUTSIDE the project folder' in (m.get('reason') or '') for m in missing):
        parsed['gate'] = 'skip'
        parsed['capability_warnings'] = list(set(
            (parsed.get('capability_warnings') or []) + capability_warnings
        ))
        parsed['reason'] = 'referenced path outside project folder — containment breach'
    return parsed


def pre_check_questions(task: Dict[str, Any],
                        project: Dict[str, Any],
                        file_names: List[str]) -> Dict[str, Any]:
    """Lightweight pre-run clarification check (H2a) — runs at confirm time.

    Unlike ``pre_run_check`` (H2), this does NOT extract or inline any file
    content. It only looks at the task description and the *names* of the files
    available in the project, and asks the overseer model whether the objective
    is ambiguous enough to warrant clarifying questions. This is the "advisory
    role" AIngel should play before a task runs — like a CLI agent asking the
    user to clarify scope instead of silently guessing (regression: task
    #10001117 — description said "whoswho" but the file is
    "whoswho.md", and the catchall pulled in 54 PDFs).

    Returns a dict with keys:
        questions  — list of {id, question, why, options?} (1-3, or empty)
        gate_state — 'hold' if questions exist, else 'open'
        reason     — one sentence
    On failure, returns a permissive default (no questions, gate_state='open').
    """
    aingel_model = (project or {}).get('aingel_model') or ''
    if not aingel_model:
        return {'questions': [], 'gate_state': 'open', 'reason': 'no aingel_model configured'}

    desc = (task.get('description') or '').strip()
    if len(desc) > 2500:
        desc = desc[:2500] + '…[truncated]'

    # File names + sizes only — never extract content here.
    # Relevance-first ordering: files whose name (or a distinctive token of it)
    # appears in the description come first, then the rest alphabetically.
    # The naive [:40] alphabetical cap hid files the task actually referenced
    # (task #10001126 — umowa file sat at index 234 of 334, so the overseer
    # wrongly concluded the file didn't exist and asked a bogus question).
    desc_l = desc.lower()
    def _relevance(name: str):
        n_l = name.lower()
        stem = os.path.splitext(n_l)[0]
        tokens = [t for t in re.split(r'[_\-\s]+', stem) if len(t) >= 4]
        score = 3 if (stem and stem in desc_l) else 2 if any(t in desc_l for t in tokens) else 0
        return (-score, n_l)
    ordered_names = sorted((file_names or []), key=_relevance)
    file_list = ordered_names[:120]

    # The list above covers Working Docs and Artifacts/outputs only. A file the
    # description names that exists at the project root instead is real, but
    # the run won't receive it as input (task #10001149 got a bogus "is this
    # another file?" question). Report it for what it is.
    root_refs = []
    _pp = (project or {}).get('path') or ''
    if _pp:
        for ref in _extract_referenced_files(desc):
            if not os.path.dirname(ref) and os.path.isfile(os.path.join(_pp, ref)) \
                    and not any(os.path.basename(n) == ref for n in file_list):
                root_refs.append(ref)
    root_block = ''
    if root_refs:
        root_block = (
            f"Files named in the description that exist at the project ROOT (outside Working Docs "
            f"and Artifacts/outputs): {json.dumps(sorted(root_refs), ensure_ascii=False)}. They exist; "
            f"do not ask whether they are missing or are another file. If the task needs one as input "
            f"(not a definition file like READMEFIRST.md), ask ONE question proposing to move it to "
            f"Working Docs (reference material) or Artifacts/outputs/ (an earlier task's output).\n\n"
        )

    prompt = (
        f"You are the Guide (project overseer) for the project \"{project.get('name','')}\". "
        f"A task is about to be confirmed for execution. Your job is to act as an advisor: "
        f"if the task objective is ambiguous or would benefit from clarification, ask the user "
        f"1-3 specific questions BEFORE it runs. In ONE response, return JSON ONLY (no prose) "
        f"with this schema:\n"
        f"{{\"questions\": [{{\"question\": str, \"why\": str, \"options\": [str]}}], "
        f"\"reason\": str}}\n\n"
        f"Task #{task.get('id')}: {task.get('title','')}\n"
        f"Phase: {task.get('phase_name','n/a')}\n"
        f"Model assigned: {task.get('model','n/a')}\n\n"
        f"Files available in the project (paths relative to project root, content not read):\n"
        f"{json.dumps(file_list, ensure_ascii=False)}\n\n"
        f"{root_block}"
        f"Description:\n{desc}\n\n"
        f"Rules:\n"
        f"- The file list above is authoritative: it contains up to {len(file_list)} paths, "
        f"with files whose names most closely relate to the task description listed FIRST. "
        f"A file matching the description appears near the top if it exists. "
        f"A nested path in the description (e.g. 'CUPT/II Etap/foo.pdf') is the SAME file as a "
        f"matching entry in the list even if the list entry includes more or less of its parent "
        f"folder path — compare by basename/stem/distinctive tokens, not exact string equality. "
        f"Before claiming a referenced file 'does not exist', check the list for its exact name, "
        f"its stem, or its distinctive tokens (e.g. 'umowa-między-pkp...' matches 'umowa-...'). "
        f"Do NOT ask a question about a file that is present in the list.\n"
        f"- Ask 1-3 questions ONLY if the objective is genuinely ambiguous or the task references "
        f"a file/scope that needs confirmation (e.g. the description names a file that doesn't "
        f"exactly match any available file, or a broad phrase like 'all the files' when a specific "
        f"file is intended).\n"
        f"- If the task is clear and actionable as written, return an EMPTY questions list.\n"
        f"- Each question should be specific and answerable in a few words. `options` is optional "
        f"and may suggest likely answers.\n"
        f"- `reason`: ONE sentence summarising why you did or did not ask questions.\n"
        f"Return ONLY the JSON object."
    )

    text, degraded = _ask(aingel_model, prompt, 600, (project or {}).get('path') or '', 'autopilot:H2a')
    if degraded:
        _log.warning('[overseer H2a] task %s: %s', task.get('id'), degraded)
        return {'questions': [], 'gate_state': 'open', 'reason': degraded}

    parsed = _safe_json(text)
    if not parsed:
        _log.warning('[overseer H2a] task %s: could not parse JSON: %s', task.get('id'), (text or '')[:200])
        return {'questions': [], 'gate_state': 'open', 'reason': 'reply was not parseable JSON'}

    questions = parsed.get('questions') or []
    # Normalise + cap at 3.
    clean = []
    for i, q in enumerate(questions[:3]):
        if not isinstance(q, dict):
            continue
        qtext = (q.get('question') or '').strip()
        if not qtext:
            continue
        clean.append({
            'id': f'q{i + 1}',
            'question': qtext,
            'why': (q.get('why') or '').strip(),
            'options': [str(o) for o in (q.get('options') or []) if str(o).strip()][:5],
        })
    gate_state = 'hold' if clean else 'open'
    return {
        'questions': clean,
        'gate_state': gate_state,
        'reason': (parsed.get('reason') or '').strip(),
    }


def _permissive_h2(degraded_reason: str = '') -> Dict[str, Any]:
    """Let the task run when oversight could not be obtained.

    Failing open is right — a transient provider error must not block work. What
    was wrong is that it failed open *invisibly*: the caller could not tell an
    approving gate from an absent one, and the EU audit line still read ALLOW
    because the call had genuinely been made. Task 695 ran ungated and every
    signal reported health. ``degraded`` now carries that distinction out.
    """
    return {
        'completeness': 'complete',
        'missing_files': [],
        'binary_unparsable': [],
        'capability_warnings': [],
        'dep_blockers': [],
        'self_serve': [],
        'strategic_advice': '',
        'gate': 'run',
        'degraded': True,
        'degraded_reason': degraded_reason,
        'reason': ('Guide oversight unavailable — proceeding WITHOUT a pre-run check'
                   + (f' ({degraded_reason})' if degraded_reason else '') + '.'),
    }


# ── H3 — Post-run analysis ────────────────────────────────────────────────────

def post_run_analysis(task: Dict[str, Any],
                      execution: Dict[str, Any],
                      project: Dict[str, Any],
                      output_text: str,
                      blocked_tool_count: int = 0,
                      referenced_files: Optional[List[str]] = None,
                      caught_files: Optional[List[str]] = None,
                      self_serve_files: Optional[List[Dict[str, Any]]] = None,
                      route_agentic: bool = False,
                      rag_label: Optional[str] = None,
                      rag_provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Structured post-run analysis. Replaces the 2-sentence Haiku brief.

    Returns a dict with keys:
        severity        — 'ok' | 'warning' | 'issue'
        findings        — list of strings
        recommendation  — 'approve' | 'discuss' | 'reject'
        gate_for_next   — 'proceed' | 'hold' | 'skip'
        brief_2s        — 2-3 sentence summary (for the inline UI element)
        unread_files    — list of referenced files H3 could not verify were read
    On failure, returns a permissive default (severity=ok, proceed).
    """
    aingel_model = (project or {}).get('aingel_model') or ''
    project_path = (project or {}).get('path') or ''
    if not aingel_model:
        return _permissive_h3()

    blocked_hits = _scan_for_blocked_tools(output_text)
    if blocked_tool_count and not blocked_hits:
        blocked_hits = [f'{blocked_tool_count} tool call(s) blocked by permission system']

    snippet = (output_text or '')[:_OUTPUT_SNIPPET_CHARS]
    if len(output_text or '') > _OUTPUT_SNIPPET_CHARS:
        snippet += '\n…[truncated]'

    finish = execution.get('finish_reason') or execution.get('status') or ''
    status = execution.get('status') or ''
    tok_in = execution.get('tokens_input') or 0
    tok_out = execution.get('tokens_output') or 0

    # Phase 7.2 — File-read verification.
    # Build the list of referenced files the model was expected to read.
    # A file counts as "verified read" if:
    #   - it was auto-inlined in the prompt (caught_files), OR
    #   - the output text mentions the filename in a read/tool-call context.
    # Anything else from referenced_files (that exists in-project) is "unread".
    referenced_files = referenced_files or []
    caught_set = set(os.path.basename(c) for c in (caught_files or []))
    self_serve_names = [os.path.basename(s.get('name', '')) for s in (self_serve_files or [])]
    unread = []
    for ref in referenced_files:
        bname = os.path.basename(ref)
        if bname in caught_set:
            continue  # was inlined in the prompt
        # Heuristic: did the output mention the file in a read context?
        # CLI routes often echo the path; Scaleway tool calls appear as tool
        # result logs in journalctl but not in the final text the model returns.
        hay = (output_text or '').lower()
        if bname.lower() in hay:
            continue  # output references the file — assume read
        # For agentic self-serve files, missing mention in the output is a gap.
        if route_agentic and bname in self_serve_names:
            unread.append(bname)

    file_check_block = ''
    if referenced_files:
        file_check_block = (
            f"File-read verification:\n"
            f"  Referenced files: {json.dumps(referenced_files[:12])}\n"
            f"  Auto-inlined in prompt: {json.dumps(sorted(caught_set)[:12])}\n"
            f"  Agentic self-serve expected: {json.dumps(self_serve_names[:12])}\n"
            f"  UNVERIFIED reads (filename not mentioned in the output; no route records "
            f"file reads, so this is NOT evidence the file was skipped): {json.dumps(unread[:12])}\n\n"
        )

    # RAG verification — did a task that required the legal library actually
    # ground its answer on retrieved citations (pre-fetch or tool)?
    rag_block = ''
    requires_rag = bool((task or {}).get('requires_rag'))
    rag_label = rag_label or ''
    if requires_rag or rag_label:
        rag_corpus = (rag_provenance or {}).get('corpus_id') or '?'
        rag_n = (rag_provenance or {}).get('n_hits') or 0
        rag_via = (rag_provenance or {}).get('via') or 'none'
        rag_block = (
            f"RAG verification:\n"
            f"  Task requires_rag={requires_rag}\n"
            f"  Pre-fetch label: {rag_label or '(none)'}\n"
            f"  Corpus: {rag_corpus}  chunks retrieved: {rag_n}  via: {rag_via}\n"
            f"  Output cites legal refs (Dz.U./CELEX/eli.gov.pl)? check below.\n\n"
        )

    # Deterministic RAG check (mirrors the unread-files supplement): if the task
    # required the library and the run produced no RAG evidence AND no legal
    # citations in the output, flag it — the answer may be grounded on model
    # knowledge rather than the library.
    rag_findings = []
    rag_missed = requires_rag and rag_label == 'RAG-REQUIRED'
    cites_legal = bool(
        re.search(r'Dz\.U\.|CELEX|eli\.gov\.pl|ELI', (output_text or ''))
    )
    if requires_rag and not rag_label:
        rag_findings.append('Task required RAG but no pre-fetch label was recorded')
    if rag_missed:
        rag_findings.append('RAG was required but no citations were retrieved (library empty/down)')
    elif requires_rag and not cites_legal:
        rag_findings.append('Task required the legal library but output cites no Dz.U./CELEX/ELI refs')

    prompt = (
        f"You are the Guide (project overseer) for the project \"{project.get('name','')}\". "
        f"A task just finished. In ONE response, return JSON ONLY (no prose) with this schema:\n"
        f"{{\"severity\": \"ok|warning|issue\", "
        f"\"findings\": [str], "
        f"\"recommendation\": \"approve|discuss|reject\", "
        f"\"gate_for_next\": \"proceed|hold|skip\", "
        f"\"brief_2s\": str, "
        f"\"unread_files\": [str]}}\n\n"
        f"Task #{task.get('id')}: {task.get('title','')}\n"
        f"Model used: {execution.get('model','n/a')}\n"
        f"Status: {status}  Finish: {finish}  Tokens: {tok_in}↑ {tok_out}↓\n"
        f"Blocked-tool hits: {json.dumps(blocked_hits)}\n\n"
        f"{file_check_block}"
        f"{rag_block}"
        f"Output (first {_OUTPUT_SNIPPET_CHARS} chars):\n```\n{snippet}\n```\n\n"
        "Rules:\n"
        "- severity=ok: output addresses the task; no issues; nothing suggests a referenced file was ignored.\n"
        "- severity=warning: minor gaps — e.g. some tool calls blocked but output still valid.\n"
        "- severity=issue: output does not address the task; or tokens truncated; or a referenced file was fabricated instead of read; or output references paths outside the project folder (containment breach); or a task that required RAG shows no retrieved-citation evidence and no Dz.U./CELEX citations.\n"
        "- recommendation=discuss when severity != ok.\n"
        "- gate_for_next=hold when severity=warning or issue (next task may depend on this one being clean).\n"
        "- gate_for_next=skip when severity=issue and the next task is clearly unreachable.\n"
        "- unread_files: copy the UNVERIFIED list above if non-empty; else []. It is informational: "
        "on its own it NEVER raises severity above ok and must not go into findings. Raise severity "
        "only if the output ignores, contradicts or fabricates that file's content.\n"
        "- brief_2s: 2-3 sentences: what was done + what's the next step. Plain text, no JSON.\n"
        "Return ONLY the JSON object."
    )

    text, degraded = _ask(aingel_model, prompt, 900, project_path, 'autopilot:H3')
    if degraded:
        _log.warning('[overseer H3] exec %s: %s', execution.get('id'), degraded)
        return _permissive_h3(degraded)

    parsed = _safe_json(text)
    if not parsed:
        _log.warning('[overseer H3] exec %s: could not parse JSON: %s',
                     execution.get('id'), (text or '')[:200])
        return _permissive_h3('reply was not parseable JSON')
    parsed.setdefault('severity', 'ok')
    parsed.setdefault('findings', [])
    parsed.setdefault('recommendation', 'approve')
    parsed.setdefault('gate_for_next', 'proceed')
    parsed.setdefault('brief_2s', '')
    parsed.setdefault('unread_files', [])
    # Informational only: no route records file reads, so a missing filename
    # mention proves nothing. Task 10001148 was downgraded to hold over a
    # 720-byte file the agent had clearly used.
    if unread and not parsed.get('unread_files'):
        parsed['unread_files'] = unread
    # Deterministic RAG supplement: a task that required the library but shows no
    # retrieval evidence + no legal citations in the output is at least a warning.
    if rag_findings:
        already = {f.lower() for f in parsed.get('findings') or []}
        merged = [f for f in rag_findings if f.lower() not in already]
        if merged:
            parsed['findings'] = list(parsed.get('findings') or []) + merged
            if parsed['severity'] == 'ok':
                parsed['severity'] = 'warning'
                parsed['recommendation'] = 'discuss'
                if parsed['gate_for_next'] == 'proceed':
                    parsed['gate_for_next'] = 'hold'
    return parsed


def _permissive_h3(degraded_reason: str = '') -> Dict[str, Any]:
    """Approve when post-run analysis could not be obtained.

    ``severity: 'ok'`` is a positive claim — "I looked and it was fine" — which is
    exactly what did not happen. It is kept so existing consumers behave, but
    ``degraded`` marks it as unexamined, and ``brief_2s`` says so in words rather
    than being stored as an empty string. An empty brief is indistinguishable from
    a model that had nothing to add; on task 695 it was the only visible trace of
    a hook that never ran.
    """
    return {
        'severity': 'ok',
        'findings': [],
        'recommendation': 'approve',
        'gate_for_next': 'proceed',
        'brief_2s': ('⚠ Oversight unavailable — this run was not analysed'
                     + (f' ({degraded_reason})' if degraded_reason else '') + '.'),
        'unread_files': [],
        'degraded': True,
        'degraded_reason': degraded_reason,
    }


# ── H4 — Project overview refresh ─────────────────────────────────────────────

def refresh_overview(project: Dict[str, Any],
                     last_5_briefs: List[Dict[str, Any]],
                     phase_list: List[Dict[str, Any]],
                     open_risks: List[str],
                     budget_snapshot: Optional[Dict[str, Any]] = None) -> str:
    """Rewrite Artifacts/aingel-overview.md from recent activity.

    Returns the markdown written. On failure, returns empty string and the
    file is left untouched.
    """
    aingel_model = (project or {}).get('aingel_model') or ''
    project_path = (project or {}).get('path') or ''
    if not aingel_model or not project_path:
        return ''

    briefs_block = '\n'.join(
        f"- Task #{b.get('task_id','?')} ({b.get('severity','?')}): {b.get('brief_2s','')}"
        for b in (last_5_briefs or [])[:5]
    ) or '(no recent executions)'

    phases_block = '\n'.join(
        f"- {p.get('name','?')}: {p.get('done',0)}/{p.get('total',0)} tasks"
        for p in (phase_list or [])[:10]
    ) or '(no phases)'

    risks_block = '\n'.join(f"- {r}" for r in (open_risks or [])) or '(none)'
    budget_line = 'n/a'
    _budget = (budget_snapshot or {}).get('budget_monthly',
               (budget_snapshot or {}).get('monthly_budget'))
    if _budget:
        spend = budget_snapshot.get('current_month_spend') or 0
        budget_line = f"${spend:.4f} / ${_budget:.2f} " \
                      f"({round(spend / _budget * 100, 1)}%)"

    prompt = (
        f"You are the Guide (project overseer) for \"{project.get('name','')}\". "
        f"Write a concise (≤2k words, ≤8000 chars) rolling project overview in Markdown. "
        f"Sections: ## Progress (one paragraph), ## Recent Activity (the briefs), "
        f"## Phase Status, ## Open Risks, ## Recommended Next Step. "
        f"Be direct and specific. No filler. Return ONLY the markdown.\n\n"
        f"Recent execution briefs:\n{briefs_block}\n\n"
        f"Phases:\n{phases_block}\n\n"
        f"Open risks (from H3 findings):\n{risks_block}\n\n"
        f"Budget: {budget_line}\n\n"
        f"Assessment rule: Only report a task as stuck if it has been running for "
        f">10 minutes with 0 tokens/commits. Tasks running <10 min are NOT stuck "
        f"even if git shows no commits or a clean working tree — Vibe CLI and batch "
        f"paths batch all writes at the end.\n"
    )

    # H4 wants markdown, not JSON — so it only needs a non-empty reply.
    text, degraded = _ask(aingel_model, prompt, 2000, project_path, 'autopilot:H4',
                          expect_json=False)
    if degraded:
        _log.warning('[overseer H4] project %s: %s', project.get('id'), degraded)
        return ''

    md = text.strip()[:_OVERVIEW_MAX_CHARS]
    try:
        overview_dir = os.path.join(project_path, 'Artifacts')
        os.makedirs(overview_dir, exist_ok=True)
        from agent_db import overview_file_path
        path = overview_file_path(project_path)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(md)
        os.replace(tmp, path)
    except Exception as e:
        _log.warning('[overseer H4] project %s: write failed: %s', project.get('id'), e)
    return md
