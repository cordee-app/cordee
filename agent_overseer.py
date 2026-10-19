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
# Artifacts/outputs/<task>/exec-<id>-output.md; H3 gets its head and its tail
# (~6k chars, ~2k tokens). Tail-heavy: an agentic transcript ends with the
# model's own verdict. RP-087 E5 said "deliverable not produced" at char 7661
# of 15612 and H3, shown only the first 4000, approved it.
_OUTPUT_HEAD_CHARS = 2000
_OUTPUT_TAIL_CHARS = 4000
_OUTPUT_SNIPPET_CHARS = _OUTPUT_HEAD_CHARS + _OUTPUT_TAIL_CHARS
# The task description H3 judges the output against (it used to get the title only).
# 1500 chars was too tight for description-heavy tasks: RP-088's H3 never saw
# the Output section (the mandated deliverable paths) or the clarifications
# block, so it 'correctly' judged against requirements it was never shown —
# flagging the mandated shared/ paths as a containment breach and dedup as a
# spec violation. Long enough to carry instructions + outputs + clarifications.
_H3_DESC_CHARS = 4000
_OVERVIEW_MAX_CHARS = 8000  # ~2k words


def _budget_keys(model: str, caller: str) -> List[str]:
    """kv keys remembering the smallest max_tokens that worked, most specific first."""
    return [f'overseer_budget:{model}:{caller}', f'overseer_budget:{model}']


def _remembered_budget(model: str, caller: str, budget: int, ceiling: int) -> Optional[int]:
    """Read the remembered max_tokens for this model/caller, clamped to [budget, ceiling].

    Returns None when nothing usable is stored, or when ``app_settings`` is
    unavailable — the caller then starts the ladder exactly as before.
    """
    try:
        from agent_db import get_app_settings
        settings = get_app_settings() or {}
    except Exception:
        return None
    for key in _budget_keys(model, caller):
        try:
            val = int(str(settings.get(key)).strip())
        except Exception:
            continue
        return max(budget, min(val, ceiling))
    return None


def _remember_budget(model: str, caller: str, max_tokens: int) -> None:
    """Persist the max_tokens that succeeded under both keys. Never raises."""
    try:
        from agent_db import set_app_setting
        for key in _budget_keys(model, caller):
            try:
                set_app_setting(key, str(max_tokens))
            except Exception:
                pass
    except Exception:
        pass


def _build_ladder(budget: int, ceiling: int, start: Optional[int] = None) -> List[int]:
    """Retry ladder: ``start`` (or ``budget``) then doubling up to ``ceiling``.

    At most 4 rungs. A remembered start skips the rungs that already failed:
    starting at 8192 with a 12000 ceiling yields ``[8192, 12000]``.
    """
    first = budget if start is None else max(budget, min(int(start), ceiling))
    ladder = [first]
    for _k in range(3):
        nxt = min(ladder[-1] * 2, ceiling)
        if nxt > ladder[-1]:
            ladder.append(nxt)
    return ladder


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
    from model_caps import effective_max_tokens, get_cap

    def _ok(t):
        if not t or not t.strip():
            return False
        return _safe_json(t) is not None if expect_json else True

    budget = effective_max_tokens(model, want_tokens)
    last_err = None
    # Retry ladder doubles until the model cap or 8192, up to 4 attempts.
    # Two attempts (want, want*2) were not enough for reviewers whose JSON
    # runs verbose: mistral-large-4 truncated at 900 AND 1800 on the RP-089
    # H3 (findings + brief exceed the terse-model budget), and the failure
    # reads as 'oversight unavailable' — which then degrades every gate
    # decision downstream of it.
    # The ceiling is the model's own max_tokens (at least 8192, at most
    # 16384): a reasoning model's thinking counts against it, and GLM-5.3
    # (cap 12000) came back empty at both 4096 and 8192 on RP-094's H2, so the
    # run went ahead with no pre-run check.
    try:
        ceiling = int(get_cap(model).get('max_tokens') or 8192)
    except Exception:
        ceiling = 8192
    ceiling = max(8192, min(ceiling, 16384))
    # Start the ladder where this model/caller last succeeded, so a reviewer
    # that needs 8192 does not burn the 2048 and 4096 rungs (each returning
    # empty content or truncated JSON) on every single call.
    ladder = _build_ladder(budget, ceiling,
                           _remembered_budget(model, caller, budget, ceiling))
    for attempt, mt in enumerate(ladder, start=1):
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
            _remember_budget(model, caller, mt)
            return text, None
        last_err = ('empty response' if not (text or '').strip()
                    else 'reply was not parseable JSON')
        _log.info('[%s] %s at max_tokens=%d (attempt %d): %s',
                  caller, last_err, mt, attempt, (text or '')[:200])
        if mt >= ceiling:
            break
    return None, f'{last_err} after {attempt} attempts (model={model})'


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


def context_ref_names(task: Optional[Dict[str, Any]]) -> List[str]:
    """Basenames of the task's attached files (``context_refs``, a JSON list of
    project-relative paths, or already a list)."""
    raw = (task or {}).get('context_refs')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    if not isinstance(raw, list):
        return []
    return [os.path.basename(r) for r in raw if isinstance(r, str) and r.strip()]


def _extract_referenced_files(description: str,
                              known_names: Optional[List[str]] = None) -> List[str]:
    """Pull file-like tokens out of the task description.

    Catches: `foo.txt`, "foo.json", 'foo.pdf', /path/to/foo.py, FULL_TEXT_OCR.txt,
    file.md, etc.  Used by H2 to compare against the prompt builder's
    `caught_files` list and detect references that were not inlined.

    Excludes URLs, URL fragments, email addresses, and bare @domain tokens —
    these are not filesystem paths and previously caused false-positive
    "containment breach" gates (e.g. `//www.example.com`, `@example.com`,
    `user@example.org`). See `_looks_like_url_or_email`.

    ``known_names`` are the basenames of the task's attached files
    (``context_refs``, see `context_ref_names`); their @-mentions are matched
    whole even when the name contains spaces.
    """
    if not description:
        return []
    # URL-encoded paths (copied from a Y:/WebDAV address, e.g.
    # `Doświadczenia%20z%20niemieckiego%20rynku.pdf`) name the file on disk
    # only once decoded; undecoded they also yield fake refs like `20rynku.pdf`.
    description = _decode_url_escapes(description)
    # An extension needs a letter: 'DPN-WPOA.501.282.2026' is a case number,
    # not a file with a '.2026' extension (#10001184, strict-mode false hold).
    refs = set()
    # Spans consumed as whole file names; the bare scans below must not re-read
    # them, or `@Report after 2031.docx` also yields the fragment `2031.docx`,
    # which does not exist and strict-holds the task (#10001193).
    consumed = []

    def _quoted(pattern, text):
        for m in re.finditer(pattern, text):
            consumed.append(m.span())
            # A leading '@' is the UI's @-mention marker, not part of the name;
            # left on, `@Name.docx` was dropped as an "@domain" token.
            name = m.group(1).lstrip('@')
            if name and not _looks_like_url_or_email(name):
                refs.add(name)

    # Backtick-quoted: `foo.txt` or `/path/to/foo.py`
    _quoted(r'`([^`]+\.(?=[0-9]*[a-zA-Z])[a-zA-Z0-9]{1,10})`', description)
    # Double-quoted: "foo.json"
    _quoted(r'"([^"]+\.(?=[0-9]*[a-zA-Z])[a-zA-Z0-9]{1,10})"', description)
    # Single-quoted: 'foo.json' or 'Working Documents/CUPT/II Etap/foo.pdf'
    _quoted(r"'([^']+\.(?=[0-9]*[a-zA-Z])[a-zA-Z0-9]{1,10})'", description)
    # Unquoted @-mentions of attached files: the UI inserts `@<file name>`, and
    # names may contain spaces, so match the known names exactly (longest first).
    for name in sorted({n for n in (known_names or ()) if n}, key=len, reverse=True):
        _quoted(r'(@?' + re.escape(name) + r')', description)
    bare = description
    for a, b in consumed:
        bare = bare[:a] + ' ' * (b - a) + bare[b:]
    # Bare paths: /foo/bar/baz.ext  or  ./foo/bar.ext
    for m in re.findall(r'(?<![\w/])(\.{0,2}/[\w./\-]+\.(?=[0-9]*[a-zA-Z])[a-zA-Z0-9]{1,10})', bare):
        if not _looks_like_url_or_email(m):
            refs.add(m)
    # Bare filenames: word chars + .ext  (only if the stem looks identifier-ish
    # to avoid catching English prose like "this is")
    for m in re.findall(r'(?<![\w./\-])([A-Za-z0-9_\-./]{4,}\.(?=[0-9]*[a-zA-Z])[a-zA-Z0-9]{1,10})\b', bare):
        if _looks_like_url_or_email(m):
            continue
        stem = os.path.splitext(m)[0]
        if any(c in stem for c in '_-') or any(c.isdigit() for c in stem) or any(c.isupper() for c in stem):
            refs.add(m)
    # A short name that ends exactly one attached file's name is shorthand for
    # it: a clarification answer "2031.docx" means "...Framework after 2031.docx"
    # and was strict-held as a missing file (#10001193).
    known = {n for n in (known_names or ()) if n}
    for r in list(refs):
        if r in known or os.path.dirname(r):
            continue
        owners = [n for n in known if n.endswith(r) and n[:-len(r)][-1:] in (' ', '_', '-')]
        if len(owners) == 1:
            refs.discard(r)
            refs.add(owners[0])
    # Name templates ("e.g., 'DPP-XXXXXX.md'", '<decision>.md') describe a file
    # to create, not one to read; H2 reported them as missing inputs (#10001185).
    return sorted(r for r in refs
                  if not _PLACEHOLDER_RE.search(os.path.basename(r)) and _has_file_extension(r)
                  and not _only_excluded(description, r))


# "Do not reference `READMEFIRST.md`" names a file the task must NOT use; it is
# not an input (#10001186). Only use-verbs count: "do not modify `a.pdf`"
# still implies the file exists and is read.
_EXCLUDED_REF_RE = re.compile(
    r"\b(?:do\s+not|don't|never|must\s+not|should\s+not|avoid)\s+"
    r"(?:\w+\s+){0,2}?(?:reference|referencing|use|using|read|reading|open|opening|"
    r"include|including|cite|citing|consult|consulting|rely\s+on|look\s+at)\b[^.\n]{0,80}$",
    re.I)


def _only_excluded(description: str, ref: str) -> bool:
    """True when every mention of ref sits in a "do not use/read/reference …" phrase."""
    positions = [m.start() for m in re.finditer(re.escape(ref), description)]
    if not positions:
        return False
    for i in positions:
        sent_start = max(description.rfind('.', 0, i - 1), description.rfind('\n', 0, i)) + 1
        before = re.sub(r'[*_`]', '', description[sent_start:i]).rstrip('"\' ')
        if not _EXCLUDED_REF_RE.search(before):
            return False
    return True


# Extensions accepted in any case ('.PDF', '.Xlsx'). Anything else must be
# written in lowercase: an unknown all-caps ending is a case-number suffix
# ('DPN-WPOA.501.282.2026.1.DK', 'DPP-WOPN.718.4.2021.PP'), not a file type
# (#10001186). Kept broad on purpose — a missed extension once meant tasks
# could not reference .xlsx files.
_KNOWN_FILE_EXTS = frozenset("""
    pdf doc docx docm dot dotx odt rtf txt text md markdown mdx rst tex pages wpd
    xls xlsx xlsm xlsb xlt xltx ods csv tsv numbers
    ppt pptx pptm pps ppsx pot potx odp key
    eml msg mbox ics vcf
    html htm xhtml xml xsd xsl rels json jsonl ndjson yaml yml toml ini cfg conf
    env properties sql db sqlite sqlite3 parquet feather avro orc
    log err out lock bak tmp timer service cron
    py ipynb js mjs cjs ts tsx jsx css scss sass less vue svelte php rb go rs java
    kt kts scala c h cc cpp cxx hpp cs swift m mm r jl lua pl pm dart ex exs erl
    hs clj sh bash zsh fish ps1 psm1 bat cmd mk cmake gradle dockerfile tf hcl
    mmd puml dot graphml
    png jpg jpeg gif bmp tif tiff webp svg ico heic heif avif psd ai eps raw
    mp3 wav flac ogg m4a aac opus wma mid midi
    mp4 mov avi mkv webm wmv flv m4v mpg mpeg 3gp
    zip tar gz tgz bz2 xz 7z rar zst
    stl step stp iges igs 3mf obj fbx dxf dwg scad gcode f3d sldprt sldasm blend
    gpx kml kmz geojson shp
    epub mobi azw3 djvu xps oxps
    srt vtt sub ass
    pem crt key pub p12
    """.split())


def _has_file_extension(ref: str) -> bool:
    ext = os.path.splitext(os.path.basename(ref))[1][1:]
    if not ext:
        return False
    if ext.lower() in _KNOWN_FILE_EXTS:
        return True
    return 2 <= len(ext) <= 5 and not ext.isupper()


# A file the task is told to CREATE is not a missing input. The verb must sit
# right before the filename with no "of/from/using/read..." in between, so
# "Create a summary of `x.pdf`" still treats x.pdf as an input.
# 'output' is the verb of the most common instruction in this repo —
# "Output a single markdown file named `X.md`" — and its absence made
# every such deliverable look like a missing input (RP-087: H2 held the
# map-task for a file the task itself was told to create). The gap check
# still protects the noun use: "the output of `X.md`" keeps X an input.
_OUTPUT_VERB_RE = re.compile(
    r'\b(?:save|saved|saving|write|writing|create|created|creating|produce|'
    r'generate|generated|export|store|output|outputs|outputting)\b(?P<gap>[^\n`"\']{0,60})$', re.I)
# Imperative naming at the start of a sentence: "Name the output file … 'X.md'".
# No input-word check: "using the decision number" there describes the name.
# Only consulted for files that do not exist yet, so a renamed source is safe.
_NAME_VERB_RE = re.compile(
    r'(?:^|[.;:!?]\s+|\(\s*)(?:name|rename)\b[^\n`"\']{0,90}$', re.I)
# Runs of X / YYYY must stand alone so Roman numerals ('Uchwała_XXXIV.pdf') survive.
_PLACEHOLDER_RE = re.compile(
    r'(?<![A-Za-z])(?:X{3,}|x{3,}|YYYY)(?![A-Za-z])|<[^>]*>|\{[^}]*\}|\.{3}|…')
# The description asks for the shared library: "RAG" as a word, or
# "rag library/system/corpus" in any case.
_RAG_REQUEST_RE = re.compile(r'\bRAG\b|\b(?i:rag)\s+(?i:library|system|corpus)\b')
# "… a single Markdown file named `X.md`": the noun + "named/called" names
# the file to produce even without a create/write verb next to it (RP-094:
# "list them in a single Markdown file named `P30a_stops.md`" was missed, so
# the pause report showed no deliverable and H3 couldn't hold on it). Only the
# clause after the last quoted reference counts, and an input verb in it
# ("Read the file named `x.pdf`") keeps it an input.
_NAMED_FILE_RE = re.compile(
    r'\b(?:file|document|report|table|sheet|spreadsheet|deck|page|note|list)\s+'
    r'(?:named|called|titled)\s*$', re.I)
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
            clause = re.split(r'[`"\']', before)[-1]
            named = _NAMED_FILE_RE.search(clause) and not _INPUT_WORD_RE.search(clause)
            if (m and not _INPUT_WORD_RE.search(m.group('gap'))) or _NAME_VERB_RE.search(before) \
                    or named or _list_parent_is_output(desc, line_start, before):
                targets.add(ref)
                break
    return targets


_LIST_MARKER_RE = re.compile(r'^\s*(?:[-*•+]|\d+[.)])\s+')
# A parent line that announces outputs: imperative verb first, or an
# "Output(s):" heading — after list markers and markdown emphasis.
_OUTPUT_PARENT_RE = re.compile(
    r'^(?:save|write|create|produce|generate|export|store)\b|^outputs?\b', re.I)
# A line that already names a file: "Save the report as `final.md`". A wrapped
# reference on the next line is not that line's output (RP-099).
_QUOTED_FILE_RE = re.compile(r'[`"\'][^`"\']+\.[A-Za-z0-9]{1,6}[`"\']')


def _list_parent_is_output(desc: str, line_start: int, before: str) -> bool:
    """True when the reference is a list item whose parent line asks for outputs:

        - Produce two separate text files:
          - `UTK_reply_option1.txt` (…)

    RP-099: the filename may be wrapped onto the next line, leaving `before`
    empty; that is accepted only as an indented continuation line, e.g.
        - Save the modified PowerPoint as:
          `Deck_with_Polish_Plan.pptx`

    The parent is the nearest earlier line with less indentation (#10001186).
    """
    indent = len(before) - len(before.lstrip())
    if _LIST_MARKER_RE.match(before):
        if _LIST_MARKER_RE.sub('', before).strip(' `*_"\''):
            return False  # not a bare list item holding just the reference
    elif before.strip() or indent == 0:
        return False  # not a marker-less, indented wrapped continuation (RP-099)
    lines = desc[:line_start].rstrip('\n').split('\n')
    for line in reversed(lines[-12:]):
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) < indent:
            if _QUOTED_FILE_RE.search(line):
                continue  # parent names its own file: not this ref's parent
            head = _LIST_MARKER_RE.sub('', line).strip().lstrip('*_ ')
            return bool(_OUTPUT_PARENT_RE.match(head))
    return False


def _scan_folders() -> List[str]:
    """Folders prompt_builder scans for referenced files, in its priority order."""
    try:
        from agent_tools import _WORKING_DOC_FOLDERS
    except Exception:
        _WORKING_DOC_FOLDERS = ('My Docs', 'Working Docs', 'Working Documents', 'working-docs', 'docs')
    return list(_WORKING_DOC_FOLDERS) + [os.path.join('Artifacts', 'outputs')]


def _cited_project_folders(description: str, project_path: str) -> List[str]:
    """Folders under the scanned roots (depth <= 3) whose name the description
    cites, as paths relative to the project root.

    H2 only knew about cited *files*; asked to work in a cited folder
    ('Search the folder OCR_Mistral'), it could not tell the folder exists
    and held the run under strict mode (#10001184).
    """
    if not description or not project_path or not os.path.isdir(project_path):
        return []
    norm_desc = _h2a_norm(description)
    found = []

    def _walk(d, depth):
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return
        for name in entries:
            full = os.path.join(d, name)
            if name.startswith('.') or not os.path.isdir(full):
                continue
            if len(name) >= 3 and name.lower() not in _H2A_GENERIC_DIRS \
                    and _h2a_mentions(norm_desc, _h2a_norm(name)):
                found.append(os.path.relpath(full, project_path))
            if depth < 3:
                _walk(full, depth + 1)

    for root in _scan_folders():
        if root.replace('\\', '/').lower() == 'artifacts/outputs':
            continue  # task-output slugs, not folders a description cites
        _walk(os.path.join(project_path, root), 1)
    return found


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
                  dep_outcomes: Optional[List[Dict[str, Any]]] = None,
                  rag_provenance: Optional[Dict[str, Any]] = None,
                  dep_unmet: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One medium-AI call that returns both strategic brief + completeness check.

    Returns a dict with keys:
        completeness        — 'complete' | 'partial' | 'incomplete'
        missing_files       — list of {name, exists, reason}
        binary_unparsable   — list of {name, model_can_parse, reason}
        capability_warnings — list of strings
        dep_blockers        — list of {task_id, title, gate_for_next, severity, brief_2s}
                              for dependencies whose H3 gate_for_next is hold/skip
        self_serve          — list of {name, exists, reason}
        output_targets      — referenced files this task is told to create
        strategic_advice    — 1-3 bullet string
        gate                — 'run' | 'hold' | 'skip'
        reason              — one sentence
    On failure, returns a permissive default (gate='run', no warnings).

    ``dep_unmet`` (dependency gate, 2026-10-17) short-circuits everything:
    a non-empty list of unmet dependencies returns an immediate deterministic
    ``gate='hold'`` (with the list under ``dep_status``) WITHOUT the _ask LLM
    call — no model must be given the chance to wave a blocked task through.
    """
    # H2 step 0 — deterministic dependency gate (TODO item 15 Phase 3.3,
    # pulled forward after RP-096). The caller recomputes the check here
    # (the entry gate's value can be stale after the up-to-1800s project
    # lock wait). Same key set as the normal return so callers reading
    # h2['missing_files'] / h2['self_serve'] / h2['output_targets'] / …
    # (run_task, test_agent_outputs) never see a missing key.
    if dep_unmet:
        def _dep_label(d):
            return (d.get('display_code') or d.get('title')
                    or ('#' + str(d.get('id') or '?')))
        first = dep_unmet[0]
        return {
            'completeness': 'incomplete',
            'missing_files': [],
            'binary_unparsable': [],
            'capability_warnings': [
                f'Dependency {_dep_label(d)} is not done' for d in dep_unmet],
            'dep_status': dep_unmet,
            'dep_blockers': [],
            'self_serve': [],
            'output_targets': [],
            'strategic_advice': '',
            'gate': 'hold',
            'reason': (f'Dependency {_dep_label(first)} '
                       f'({first.get("status") or "?"}) is not done'),
        }

    aingel_model = (project or {}).get('aingel_model') or ''
    project_path = (project or {}).get('path') or ''
    if not aingel_model:
        return _permissive_h2()

    referenced = _extract_referenced_files(task.get('description') or '',
                                           context_ref_names(task))
    # Distinguish caught (inlined/batched) from missing
    caught_set = set(caught_files or [])
    # Lane B catches by project-relative path; a description names the file
    # by basename (RP-085: a caught PNG was reported as out of scope).
    caught_set |= {os.path.basename(c) for c in caught_set}
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
    # An OCR route (file_access 'api-ocr') has no chat vision but reads the
    # referenced PDFs/images itself — agent_router._call_mistral_ocr.
    ocr_reads = (('.pdf', '.png', '.jpg', '.jpeg') if file_access == 'api-ocr' else ())
    for bn in (noted_binary or []):
        ext = os.path.splitext(bn)[1].lower()
        if ext in ocr_reads:
            continue
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
    caps = {'file_access': file_access, 'vision': vision or ('via OCR' if ocr_reads else False),
            'tools': (model_caps or {}).get('tools', False),
            'context_window': (model_caps or {}).get('context_window', 0)}
    if ocr_reads:
        caps['ocr'] = ('OCR-only model: reads the referenced PDF/PNG/JPEG files '
                       'itself and returns their text as markdown, saved as '
                       'Working Documents/OCR_mistral/<file name>.md; it cannot '
                       'follow instructions, filter, or write other files')
    caps_block = json.dumps(caps)

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
        # Parenthesised: unbracketed, the conditional took the whole implicitly
        # concatenated header, so with no budget the prompt began at
        # "Budget used: n/a" — no role, schema, task or model (RP-085).
        + (f"Budget used: {budget_pct}%" if budget_pct is not None else "Budget used: n/a"))
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
    cited_dirs = _cited_project_folders(task.get('description') or '', project_path)
    if cited_dirs:
        prompt += (
            "Folders named in the description that EXIST inside the project "
            f"(an agentic model can list and read them itself): {json.dumps(cited_dirs[:8], ensure_ascii=False)}\n"
        )
    # RAG intent: does this task need the shared legal library?
    _rag_intent = None
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
            # Retrieval already ran while the prompt was built: state its outcome,
            # so the gate is not decided on "availability unconfirmed" (#10001184).
            if rag_provenance:
                if rag_provenance.get('error'):
                    prompt += f"RAG retrieval FAILED: {str(rag_provenance.get('error'))[:200]}\n"
                elif rag_provenance.get('n_hits'):
                    prompt += (f"RAG retrieval already ran: {rag_provenance.get('n_hits')} citations "
                               "inlined in the prompt — the library is available.\n")
                else:
                    prompt += "RAG retrieval already ran and returned 0 citations.\n"
    except Exception:
        pass
    # A description that asks for the RAG library while no retrieval is set up
    # (Requires RAG off, no legal markers, no tagged rag-index refs) gets
    # sources the run never retrieved (#10001186). Held deterministically below.
    # Scaleway/Ollama models run the in-process tool loop, which offers the
    # rag_query tool when the project has the library on: wired as well.
    _model_id = (task.get('model') or '').strip()
    _has_rag_tool = (_model_id.startswith(('scw-', 'oll-'))
                     and bool((project or {}).get('use_rag')))
    rag_unwired = False
    if not _rag_intent and not _has_rag_tool \
            and _RAG_REQUEST_RE.search(task.get('description') or ''):
        try:
            import agent_rag as _rag_refs
            rag_unwired = not _rag_refs._task_rag_index_refs(task, project_path)
        except Exception:
            rag_unwired = True
    if rag_unwired:
        prompt += ("RAG: the description asks for the RAG library, but no retrieval is set up "
                   "for this run; the model cannot consult it.\n")
    prompt += f"Description:\n{desc}\n\n"
    prompt += (
        "Rules for `gate`:\n"
        "- run: every referenced file is inlined, OR the route is agentic and the file is inside the project (self-serve); no blocking binary mismatches. A folder listed above as existing inside the project is reachable by an agentic model and is never a reason to hold.\n"
        + ("- run (OCR route): the model capabilities include `ocr`, so the referenced PDF/PNG/JPEG files are "
           "read by the OCR route itself — they need not be inlined and are not binary mismatches. Hold or "
           "skip only if the task asks for more than a transcription (filtering, analysis, other output files).\n"
           if ocr_reads else '')
        + "- hold: partial — minor warnings the user should see but can override (e.g. agentic self-serve of a file that may not exist).\n"
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
    # The model sometimes answers with H3's vocabulary ('proceed') or another
    # synonym; strict mode aborts on anything but 'run', so a "proceed" verdict
    # failed RP-082·E1. Map run-synonyms to 'run'; any other unknown → 'hold'.
    _gate = str(parsed.get('gate') or 'run').strip().lower()
    if _gate in ('proceed', 'go', 'ok', 'pass', 'open', 'continue', 'allow'):
        _gate = 'run'
    elif _gate not in ('run', 'hold', 'skip'):
        _gate = 'hold'
    parsed['gate'] = _gate
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
        # Basename OR a generic "the deliverable file" wording — RP-087 E6
        # held with "deliverable file is missing" without ever naming the
        # file, which slipped past a basename-only match.
        _cites_output = (any(os.path.basename(t).lower() in _reason_l
                             for t in output_expected)
                         or 'deliverable' in _reason_l)
        _other = bool(missing or binary_unparsable or any(not x.get('exists') for x in self_serve))
        if _cites_output and not _other:
            parsed['gate'] = 'run'
            parsed['reason'] = (
                f"{', '.join(os.path.basename(t) for t in output_expected[:3])} is this task's "
                "own output and will be created; no blocking issue found")
    # Same for an OCR route: the gate model keeps reading "no chat vision" as
    # "cannot read the PNG" despite the `ocr` capability (RP-085, 4 skips).
    # Only a vision/parse objection is overruled — "asks for filtering or
    # interpretation" still blocks.
    if ocr_reads and parsed.get('gate') in ('hold', 'skip'):
        _reason_l = (parsed.get('reason') or '').lower()
        _other = bool(missing or binary_unparsable or any(not x.get('exists') for x in self_serve))
        if ('vision' in _reason_l or 'parse' in _reason_l) and not _other:
            parsed['gate'] = 'run'
            parsed['binary_unparsable'] = []
            parsed['reason'] = ('OCR route reads the referenced PDF/image files itself; '
                                'no blocking issue found')
    if rag_unwired and parsed.get('gate') != 'skip':
        parsed['gate'] = 'hold'
        parsed['reason'] = (
            'The description asks for the RAG library, but none will be consulted: tick '
            '"Requires RAG library" on the task (the project must have the RAG library on) '
            'and name the provisions to look up, or remove the RAG instruction.')
    # Force skip on containment breaches regardless of model opinion.
    if any('OUTSIDE the project folder' in (m.get('reason') or '') for m in missing):
        parsed['gate'] = 'skip'
        parsed['capability_warnings'] = list(set(
            (parsed.get('capability_warnings') or []) + capability_warnings
        ))
        parsed['reason'] = 'referenced path outside project folder — containment breach'
    return parsed


_H2A_FILE_CAP = 120
_H2A_FOLDER_CAP = 60
# A filename token shared by more files than this is a naming convention
# (e.g. 'wopn' in 41 DPP-WOPN.*.md files), not evidence of relevance.
_H2A_TOKEN_DISTINCTIVE_MAX = 5
# Path components that say nothing about the task: the working-doc roots and
# the Artifacts/outputs prefix. Task-output slug folders are skipped separately.
_H2A_GENERIC_DIRS = {'my docs', 'working docs', 'working documents', 'working-docs',
                     'docs', 'artifacts', 'outputs'}


def _h2a_norm(s: str) -> str:
    return re.sub(r'[_\-\s]+', ' ', s.lower()).strip()


def _h2a_mentions(norm_desc: str, norm_term: str) -> bool:
    """Whole-word mention of an already-normalised term in the description."""
    if not norm_term:
        return False
    return re.search(r'(?<![^\W_])' + re.escape(norm_term) + r'(?![^\W_])', norm_desc) is not None


def _rank_h2a_paths(paths: List[str], desc: str):
    """Order H2a's file list by relevance to the description and cap it, and
    build the folder list shown beside it.

    Tiers (best first): 4 = the file's own name is cited; 3 = the file sits
    in a folder the description cites; 2 = a distinctive filename token is
    cited; 0 = everything else. Scoring looks at the basename and at
    real folder names only — never at the working-doc root names or at the
    Artifacts/outputs/<task-slug> folders, whose slugs are built from task
    titles full of common words ('find', 'file', 'decision'...).

    History: #10001126 added relevance ordering on bare basenames. #10001145
    switched the list to relative paths so nested references could be matched,
    which silently turned the "stem in description" check into a full-path
    check that never fired; #10001185 then had its file buried among
    old outputs and its cited output folder cut off by the cap.

    Returns (file_list, folder_list); folder_list entries are
    "<relative folder> (<n> files)".
    """
    norm_desc = _h2a_norm(desc)
    token_df: Dict[str, int] = {}
    for p in paths:
        stem = os.path.splitext(os.path.basename(p))[0]
        for t in {t for t in re.split(r'[_\-\s.]+', stem.lower()) if len(t) >= 4}:
            token_df[t] = token_df.get(t, 0) + 1

    def _meaningful_dirs(p: str) -> List[str]:
        parts = p.replace('\\', '/').split('/')[:-1]
        out = []
        for i, part in enumerate(parts):
            if part.lower() in _H2A_GENERIC_DIRS:
                continue
            if i == 2 and [x.lower() for x in parts[:2]] == ['artifacts', 'outputs']:
                continue  # task-output slug
            out.append(part)
        return out

    def _score(p: str) -> int:
        stem = os.path.splitext(os.path.basename(p))[0]
        base = os.path.basename(p)
        # A one-word stem ('TASKS', '_summary', '_index') is just an English word
        # in most descriptions; it needs its extension to count as cited.
        stem_n = _h2a_norm(stem)
        if _h2a_mentions(norm_desc, _h2a_norm(base)) or (
                len(stem) >= 4 and re.search(r'[\d\s.]', stem_n)
                and _h2a_mentions(norm_desc, stem_n)):
            return 4
        if any(len(d) >= 3 and _h2a_mentions(norm_desc, _h2a_norm(d)) for d in _meaningful_dirs(p)):
            return 3
        tokens = {t for t in re.split(r'[_\-\s.]+', stem.lower()) if len(t) >= 4}
        if any(token_df.get(t, 0) <= _H2A_TOKEN_DISTINCTIVE_MAX and _h2a_mentions(norm_desc, t)
               for t in tokens):
            return 2
        return 0

    file_list = sorted(paths, key=lambda p: (-_score(p), p.lower()))[:_H2A_FILE_CAP]

    counts: Dict[str, int] = {}
    for p in paths:
        d = os.path.dirname(p)
        if d:
            counts[d] = counts.get(d, 0) + 1

    def _folder_key(d: str):
        base = os.path.basename(d)
        cited = base.lower() not in _H2A_GENERIC_DIRS and _h2a_mentions(norm_desc, _h2a_norm(base))
        is_output = d.replace('\\', '/').lower().startswith('artifacts/outputs')
        return (0 if cited else 1, 1 if is_output else 0, d.lower())

    folder_list = [f'{d} ({counts[d]} files)'
                   for d in sorted(counts, key=_folder_key)[:_H2A_FOLDER_CAP]]
    return file_list, folder_list


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
    file_list, folder_list = _rank_h2a_paths(file_names or [], desc)

    # The list above covers Working Docs and Artifacts/outputs only. A file the
    # description names that exists at the project root instead is real, but
    # the run won't receive it as input (task #10001149 got a bogus "is this
    # another file?" question). Report it for what it is.
    root_refs = []
    _pp = (project or {}).get('path') or ''
    if _pp:
        for ref in _extract_referenced_files(desc, context_ref_names(task)):
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
        f"Folders in the project (with file counts; folders named in the description first — "
        f"a folder listed here exists even if none of its files appear in the file list above):\n"
        f"{json.dumps(folder_list, ensure_ascii=False)}\n\n"
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

    snippet = _output_snippet(output_text)
    description = ((task or {}).get('description') or '').strip()
    if len(description) > _H3_DESC_CHARS:
        description = description[:_H3_DESC_CHARS] + '\n…[truncated]'

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

    # Deterministic completion checks (code, not the Guide model): a deliverable
    # the description names must exist, and a run cut off by its turn limit is
    # not finished. Both apply even when the Guide's reply is unusable.
    missing_deliverables = _missing_deliverables(task, project_path,
                                                 (execution or {}).get('started_at'))
    hit_limit = _hit_turn_limit(output_text)
    completion_findings = [f'Deliverable not written: {name}' for name in missing_deliverables]
    if hit_limit:
        completion_findings.append('Run stopped at its turn limit before the model finished')
    completion_block = ''
    if completion_findings:
        completion_block = ('Completion check (by code — treat as facts):\n'
                            + ''.join(f'  - {f}\n' for f in completion_findings) + '\n')

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
        f"Task description (what was asked):\n```\n{description or '(none)'}\n```\n"
        f"Model used: {execution.get('model','n/a')}\n"
        f"Status: {status}  Finish: {finish}  Tokens: {tok_in}↑ {tok_out}↓\n"
        f"{_parts_line(execution)}"
        f"Blocked-tool hits: {json.dumps(blocked_hits)}\n\n"
        f"{completion_block}"
        f"{file_check_block}"
        f"{rag_block}"
        f"Output (head and tail, {len(output_text or '')} chars in total):\n```\n{snippet}\n```\n\n"
        "Rules:\n"
        "- Judge the output against the task DESCRIPTION, not the title. Read the END of the "
        "output: an agentic run ends with the model's own verdict (e.g. 'deliverable not produced').\n"
        "- severity=ok: output addresses the task; no issues; nothing suggests a referenced file was ignored.\n"
        "- severity=warning: minor gaps — e.g. some tool calls blocked but output still valid.\n"
        "- severity=issue: output does not address the task; or a deliverable the description names was not written; or the run stopped before finishing; or tokens truncated; or a referenced file was fabricated instead of read; or output references paths outside the project folder (containment breach); or a task that required RAG shows no retrieved-citation evidence and no Dz.U./CELEX citations.\n"
        "- recommendation=discuss when severity != ok.\n"
        "- gate_for_next=hold when severity=warning or issue (next task may depend on this one being clean).\n"
        "- gate_for_next=skip when severity=issue and the next task is clearly unreachable.\n"
        "- unread_files: copy the UNVERIFIED list above if non-empty; else []. It is informational: "
        "on its own it NEVER raises severity above ok and must not go into findings. Raise severity "
        "only if the output ignores, contradicts or fabricates that file's content.\n"
        "- brief_2s: 2-3 sentences: what was done, then what the USER could do next, written as "
        "options for the user (\"You can re-run …\", \"You may want to check …\"). Never an "
        "instruction to an agent: no \"Next step: run …\", no imperatives. This text is shown to "
        "the user in the Guide chat. Plain text, no JSON.\n"
        "Return ONLY the JSON object."
    )

    text, degraded = _ask(aingel_model, prompt, 900, project_path, 'autopilot:H3')
    if degraded:
        _log.warning('[overseer H3] exec %s: %s', execution.get('id'), degraded)
        return _apply_completion_findings(_permissive_h3(degraded), completion_findings,
                                          bool(missing_deliverables))

    parsed = _safe_json(text)
    if not parsed:
        _log.warning('[overseer H3] exec %s: could not parse JSON: %s',
                     execution.get('id'), (text or '')[:200])
        return _apply_completion_findings(_permissive_h3('reply was not parseable JSON'),
                                          completion_findings, bool(missing_deliverables))
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
    # Deterministic backstop (mirror of H2's output-target rule): when the
    # code-level file check proves every named deliverable exists on disk,
    # the Guide model's 'deliverable not written' claims are false and are
    # stripped — it infers non-delivery from the turn-limit marker without
    # checking files (RP-089 exec 20001271: the part file was written
    # incrementally during the run and merged; the false reject then posted
    # into the project chat, where the autonomous chat agent 'helpfully'
    # moved and rewrote the deliverable). A run cut at its turn limit with
    # all deliverables present is a warning-level caveat, not a blocker:
    # the incremental-write discipline makes the shipped file the success
    # criterion, and [untraced] honest gaps are within spec.
    try:
        _refs = _extract_referenced_files(description, context_ref_names(task))
        _targets = sorted(_output_target_refs(description, _refs))
    except Exception:
        _targets = []
    all_delivered = bool(_targets) and not missing_deliverables
    if all_delivered:
        tset = {os.path.basename(t).lower() for t in _targets}
        unwritten_re = re.compile(
            r'not (?:be )?(?:written|produced|created|delivered|on disk|persisted|saved)'
            r'|never (?:persisted|written|saved|captured)'
            r'|no file|file (?:is |was )?missing|missing file|not saved')
        def _false_unwritten(finding):
            l = (finding or '').lower()
            return any(b in l for b in tset) and bool(unwritten_re.search(l))
        kept = [f for f in (parsed.get('findings') or []) if not _false_unwritten(f)]
        if len(kept) != len(parsed.get('findings') or []):
            parsed['findings'] = kept
            parsed['all_deliverables_present'] = True
            turn_limit_re = re.compile(r'turn limit|stopped at \d+ turns?|hit its turn')
            only_turn_limit = all(turn_limit_re.search(f or '')
                                  for f in (parsed.get('findings') or []))
            if parsed.get('severity') == 'issue' and only_turn_limit:
                # the issue rating was anchored on the (now stripped) false
                # non-delivery claim; what remains is the turn-limit fact,
                # which with the file delivered is warning-level
                parsed['severity'] = 'warning'
                if parsed.get('recommendation') == 'reject':
                    parsed['recommendation'] = 'discuss'
                if parsed.get('gate_for_next') in ('hold', 'skip'):
                    parsed['gate_for_next'] = 'proceed'
            if not parsed.get('findings'):
                parsed['severity'] = 'ok'
                parsed['recommendation'] = 'approve'
                parsed['gate_for_next'] = 'proceed'
    return _apply_completion_findings(parsed, completion_findings,
                                      bool(missing_deliverables),
                                      all_delivered=all_delivered)


_FILES_CREATED_HEADING = '## Files created in this run'
# The executor linkifies created files as [name](/files/<pid>/<rel path>) for
# the UI. Those are inside-project paths rendered through a UI route.
_FILES_LINK_RE = re.compile(r'\[([^\]]+)\]\(/files/\d+/[^)]*\)')
_FILES_LIST_KEEP = 10


def _parts_line(execution: Dict[str, Any]) -> str:
    """A fact line for runs that paused at their limit (TODO item 16): the
    reviewer said "paused once" for RP-094, which ran in three parts."""
    parts = int((execution or {}).get('segments') or 1)
    reason = (execution or {}).get('pause_reason') or ''
    if parts <= 1 and not reason:
        return ''
    what = 'time' if reason == 'time' else 'turn'
    if parts <= 1:
        return f"Run parts: 1 — it paused at its {what} limit and the user stopped it there.\n"
    return (f"Run parts: {parts} — it paused at its {what} limit and the user continued it "
            f"{parts - 1} time(s); a run stopped at a later pause says so in its output.\n")


def _output_snippet(output_text: str) -> str:
    """Head + tail of the run output for H3 (whole text when it fits).

    The executor's "## Files created in this run" list is cut to its first
    entries before the tail is taken: on RP-087 E5 its 69 tile links filled the
    whole tail and pushed the model's verdict out again. The /files/<pid>/
    markdown URLs are also de-linkified first: H3's rubric treats references
    to paths outside the project folder as a containment breach, and the
    reviewer read the executor's own UI links as such a breach (RP-089
    exec 20001271, finding 4 of 5 was a false positive on system
    boilerplate).
    """
    text = output_text or ''
    files_note = ''
    idx = text.rfind(_FILES_CREATED_HEADING)
    if idx >= 0:
        body, files = text[:idx].rstrip(), text[idx:]
        entries = [_FILES_LINK_RE.sub(r'\1', l)
                   for l in files.splitlines()[1:] if l.strip()]
        kept = entries[:_FILES_LIST_KEEP]
        files_note = '\n\n' + _FILES_CREATED_HEADING + '\n' + '\n'.join(kept)
        if len(entries) > len(kept):
            files_note += f'\n…[{len(entries) - len(kept)} more files]'
        text = body
    if len(text) + len(files_note) <= _OUTPUT_SNIPPET_CHARS:
        return text + files_note
    tail_chars = max(_OUTPUT_TAIL_CHARS - len(files_note), _OUTPUT_TAIL_CHARS // 2)
    skipped = len(text) - _OUTPUT_HEAD_CHARS - tail_chars
    if skipped <= 0:
        return text + files_note
    return (text[:_OUTPUT_HEAD_CHARS]
            + f'\n\n…[{skipped} chars omitted]…\n\n'
            + text[-tail_chars:] + files_note)


def _hit_turn_limit(output_text: str) -> bool:
    """True when a route recorded that the run was cut off by its turn limit."""
    try:
        from agent_router import TOOL_TURN_LIMIT_MARKER, VIBE_TURN_LIMIT_MARKER
    except Exception:  # pragma: no cover — router import failure must not break H3
        return False
    text = output_text or ''
    return TOOL_TURN_LIMIT_MARKER in text or VIBE_TURN_LIMIT_MARKER in text


def _parse_utc_ts(value) -> Optional[float]:
    """Epoch seconds for a DB timestamp ('YYYY-MM-DD HH:MM:SS' UTC, or ISO)."""
    if not value:
        return None
    from datetime import datetime, timezone
    raw = str(value).strip().replace('T', ' ')
    raw = raw.split('+')[0].rstrip('Z')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _missing_deliverables(task: Dict[str, Any], project_path: str,
                          started_at=None) -> List[str]:
    """Files the description tells the task to create that this run did not write.

    A target counts as written when a file with that name exists — at its
    project-relative path, at the project root, or anywhere under the task's
    output folder (Artifacts/outputs/<id>-*/, nested folders included) — and,
    when the run's start time is known, was modified after the run started, so
    an earlier attempt's file does not pass for this run's.

    Regression: RP-087 E5/E7 never wrote `pkp_intercity_routes_2026_2027.md`
    and H3 approved both.
    """
    task = task or {}
    if not project_path or not os.path.isdir(project_path):
        return []
    description = task.get('description') or ''
    if not description:
        return []
    try:
        refs = _extract_referenced_files(description, context_ref_names(task))
        targets = sorted(_output_target_refs(description, refs))
    except Exception as exc:  # detection must never break H3
        _log.warning('[overseer H3] deliverable detection failed: %s', exc)
        return []
    if not targets:
        return []
    since = _parse_utc_ts(started_at)
    if since is not None:
        since -= 60  # clock/rounding slack

    def _fresh(path: str) -> bool:
        try:
            return os.path.isfile(path) and (since is None or os.path.getmtime(path) >= since)
        except OSError:
            return False

    tid = task.get('id')
    out_dirs = []
    base = os.path.join(project_path, 'Artifacts', 'outputs')
    if tid is not None and os.path.isdir(base):
        try:
            out_dirs = [os.path.join(base, e) for e in os.listdir(base)
                        if e.startswith(f'{tid}-') and os.path.isdir(os.path.join(base, e))]
        except OSError:
            out_dirs = []

    def _fresh_under(dirs, name: str) -> bool:
        for d in dirs:
            for root, _dirs, files in os.walk(d):
                if name in files and _fresh(os.path.join(root, name)):
                    return True
        return False

    # One verdict per file NAME. A description often names the same file twice —
    # bare ("write `part01.md`") and with its folder given elsewhere — and each
    # reference used to become its own "not written" finding (RP-090, ×2).
    by_name: Dict[str, List[str]] = {}
    for ref in targets:
        rel = ref.lstrip('/')
        if os.path.isabs(ref) or '..' in rel.split('/'):
            continue  # outside the project — H2 handles containment
        by_name.setdefault(os.path.basename(rel), []).append(rel)

    missing = []
    other_outputs = None
    for name, rels in sorted(by_name.items()):
        if any(_fresh(os.path.join(project_path, r)) for r in rels) \
                or _fresh(os.path.join(project_path, name)) or _fresh_under(out_dirs, name):
            continue
        # Last resort: a fresh file of that name in ANY task's output folder.
        # Chained tasks write to a shared folder inside another task's outputs
        # and give that folder in prose, not in the file reference (RP-089:
        # `shared/` under 10001204-…). Missing that, the check told H3 a
        # written file was "not written" — a false fact the Guide then acted on.
        # Freshness (written during this run) keeps an older file from passing.
        if other_outputs is None:
            try:
                other_outputs = [os.path.join(base, e) for e in os.listdir(base)
                                 if os.path.isdir(os.path.join(base, e))
                                 and os.path.join(base, e) not in out_dirs]
            except OSError:
                other_outputs = []
        if since is not None and _fresh_under(other_outputs, name):
            continue
        missing.append(name)
    return missing


def _apply_completion_findings(parsed: Dict[str, Any], findings: List[str],
                               deliverable_missing: bool,
                               all_delivered: bool = False) -> Dict[str, Any]:
    """Raise an H3 verdict for code-detected incompleteness (never lowers it).

    A missing named deliverable is an ``issue``; a turn-limit cut-off on its own
    is at least a ``warning``. Either way the next task is held for the user —
    EXCEPT when ``all_delivered`` says every deliverable is on disk: then the
    cut-off stays a recorded warning but no longer blocks the chain (see the
    post_run_analysis backstop for why).
    """
    if not findings:
        return parsed
    existing = list(parsed.get('findings') or [])
    already = {f.lower() for f in existing}

    def _said_already(f: str) -> bool:
        # "Deliverable not written: X" duplicates a model finding that names X
        # and says it is missing / not written (merge by file name, not text).
        if f.lower() in already:
            return True
        if f.startswith('Deliverable not written: '):
            name = f.split(': ', 1)[1].lower()
            return any(name in e.lower() and ('not written' in e.lower() or 'missing' in e.lower())
                       for e in existing)
        return False

    parsed['findings'] = existing + [f for f in findings if not _said_already(f)]
    rank = {'ok': 0, 'warning': 1, 'issue': 2}
    floor = 'issue' if deliverable_missing else 'warning'
    if rank.get(parsed.get('severity'), 0) < rank[floor]:
        parsed['severity'] = floor
    if parsed.get('recommendation') == 'approve':
        parsed['recommendation'] = 'discuss'
    if not all_delivered and parsed.get('gate_for_next') == 'proceed':
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


# ── Pause report — the Guide's read of a run paused at its limit (item 16) ───

_PAUSE_PROGRESS = ('progressing', 'looping', 'blocked')
_PAUSE_RECOMMEND = ('continue', 'continue_with_note', 'stop')


def pause_report_summary(task: Dict[str, Any], project: Dict[str, Any],
                         facts: Dict[str, Any], transcript_text: str) -> Optional[Dict[str, Any]]:
    """One tool-less call: is the paused run progressing, and should the user
    continue it? Code gathers the facts; the Guide only reads them and the
    head + tail of the transcript. Returns None when no Guide model is set or
    its reply is unusable (the report then shows the facts only).
    """
    model = (project or {}).get('aingel_model') or ''
    if not model:
        return None
    description = ((task or {}).get('description') or '').strip()
    if len(description) > _H3_DESC_CHARS:
        description = description[:_H3_DESC_CHARS] + '\n…[truncated]'
    prompt = (
        f"You are the Guide for the project \"{(project or {}).get('name', '')}\". "
        "A task run has paused at its limit and the user must decide whether to continue it. "
        "Return JSON ONLY:\n"
        '{"progress": "progressing|looping|blocked", '
        '"recommendation": "continue|continue_with_note|stop", '
        '"suggested_note": str, "summary": str}\n\n'
        f"Task #{(task or {}).get('id')}: {(task or {}).get('title', '')}\n"
        f"Task description:\n```\n{description or '(none)'}\n```\n"
        f"Facts (from code — treat as true):\n```\n{json.dumps(facts, ensure_ascii=False, indent=1)}\n```\n"
        f"Transcript (head and tail):\n```\n{_output_snippet(transcript_text)}\n```\n\n"
        "Rules:\n"
        "- progressing: the last part made real progress toward the deliverable.\n"
        "- looping: it repeats the same actions or re-reads the same files without new results.\n"
        "- blocked: it cannot finish as asked (missing input, missing capability, wrong approach).\n"
        "- continue_with_note when a short instruction would fix the approach (for example: "
        "write the deliverable now and add rows as you go). suggested_note is that instruction, "
        "in at most 2 sentences, written to the model; empty otherwise.\n"
        "- stop when blocked, or when looping and no note would help.\n"
        "- summary: 2-3 sentences for the user: what it did, what is left, your advice. "
        "Never invent facts that are not in the facts or the transcript.\n"
        "Return ONLY the JSON object."
    )
    text, degraded = _ask(model, prompt, 700, (project or {}).get('path') or '', 'pause:report')
    if degraded:
        _log.warning('[pause report] task %s: %s', (task or {}).get('id'), degraded)
        return None
    parsed = _safe_json(text)
    if not parsed:
        return None
    progress = str(parsed.get('progress') or '').strip().lower()
    rec = str(parsed.get('recommendation') or '').strip().lower()
    return {
        'progress': progress if progress in _PAUSE_PROGRESS else '',
        'recommendation': rec if rec in _PAUSE_RECOMMEND else '',
        'suggested_note': str(parsed.get('suggested_note') or '').strip()[:500],
        'summary': str(parsed.get('summary') or '').strip()[:1200],
        'model': model,
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
