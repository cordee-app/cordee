"""Local decision-model pre-filter for the H2 pre-run check.

A small on-vault decision model (Ollama ``/v1/systemone``) answers a fixed
typed question set about a task description.  The result is stored on the task
row (``tasks.prefilter_json``) and injected into the H2 prompt as a compact,
signal-only advisory block.

Design invariants (plan v1.2 / shared interface spec):

* Inference is **loopback only** — :func:`decide` fails closed to
  ``status='unknown'`` for any non-loopback base URL.
* The pre-filter is **never the authority**: the H2 LLM still decides.
* Nothing in this module may raise into a caller.  Every public function
  catches and logs via ``logging.getLogger('agent_decision')``.
* ``agent_db`` is imported lazily inside functions (``agent_db`` imports this
  module lazily too, so a module-level import would create a cycle).
* Decision model IDs are deliberately **not** registered in
  ``agent_config.MODELS``/``PRICING``/``model_caps.json`` and never touch
  ``route()``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

_log = logging.getLogger('agent_decision')

# ── Model / endpoint constants ──────────────────────────────────────────────
LOCAL_DEC_FAST = 'tev1:0.8b'
LOCAL_DEC_ESCALATE = 'tev1:4b-q4_K_M'
LOCAL_BASE_URL = 'http://127.0.0.1:11434'

QSET_VERSION = 2
STATE_DESC_CHARS = 2500
MIN_DESCRIPTION_CHARS = 30

# Thresholds (defaults; calibrated by scripts/calibrate_prefilter.py).
FLAG_HIGH = 0.9
ESCALATE_LOW = 0.4
POST_ESCALATE_FLAG = 0.7
CHOICE_MARGIN = 0.15
SCORE_REVIEW = 2

MAX_BLOCK_CHARS = 800

# Keep the fast tier warm, let the 4B unload when idle (memory).
_FAST_KEEP_ALIVE = '24h'
_ESCALATE_KEEP_ALIVE = '5m'
_REQUEST_TIMEOUT = 120
_SHOW_TIMEOUT = 5

_QUEUE_MAX = 200
_WORKER_HEARTBEAT_S = 60

# ── Question set (plan §4) ──────────────────────────────────────────────────
# Each question mirrors the Ollama /v1/systemone schema:
#   noul  -> {'type','instructions'} (+ optional true/false criteria)
#   choice-> {'type','instructions','criteria': {label: description}}
#   score -> {'type','instructions','criteria': [level0, level1, ...]}
_QSET = {
    'read_scope': {
        'type': 'score',
        'instructions': ('How broad is the file reading? A huge image/PDF that needs '
                         'tiling or scanning counts as wide, not as one file.'),
        'criteria': [
            'No named files or only inline text',
            'One small folder or a few files',
            'Wildcard or many files',
            'Whole project, a huge image/PDF, or a tiling/scan-everything pass',
        ],
    },
    'asks_system_level_commands': {
        'type': 'noul',
        'instructions': ('Does the task ask for crontab, systemctl, apt, or /etc '
                         'changes? Project-local pip/npm does not count.'),
    },
    'asks_outside_project_paths': {
        'type': 'noul',
        'instructions': ('Does the task reference paths outside the project, '
                         'including prose like \'the parent of Working Docs\'?'),
    },
    'requires_web_research': {
        'type': 'noul',
        'instructions': ('Does the task require official online sources, web pages, '
                         'or live internet research?'),
    },
    'prohibits_project_files': {
        'type': 'noul',
        'instructions': ('Does the task forbid using project files as sources '
                         '("do not use project files", "no external files, project '
                         'files, or cached data")?'),
    },
    'prohibition_topics': {
        'type': 'choice',
        'instructions': ('Which topics does the task explicitly prohibit? Choose the '
                         'closest single option.'),
        'criteria': {
            'none': 'No prohibition is stated.',
            'project-files': 'Must not use or read the project\'s files.',
            'existing-files': 'Must not modify or overwrite existing files.',
            'outside-project': 'Must not access paths outside the project.',
            'network-external': 'Must not use the network or external services.',
            'git-ops': 'Must not run git operations.',
            'multiple': 'More than one of the above applies.',
        },
    },
    'asks_modify_or_overwrite_existing': {
        'type': 'choice',
        'instructions': ('May the task modify or overwrite existing files, or does it '
                         'only create new output?'),
        'criteria': {
            'new-only': 'Only new files or new content are requested.',
            'modify-existing': 'Existing files are edited in place.',
            'overwrite-possible': 'Existing files may be replaced or clobbered.',
            'unclear': 'The task does not make this clear.',
        },
    },
    'asks_secrets_or_credentials': {
        'type': 'noul',
        'instructions': 'Does the task involve .env files, API keys, tokens, or passwords?',
    },
    'asks_external_side_effects': {
        'type': 'noul',
        'instructions': ('Does the task send, publish, email, upload, or call an '
                         'external webhook?'),
    },
    'asks_delete_move': {
        'type': 'noul',
        'instructions': 'Does the task delete, move, or rename files or directories?',
    },
    'binary_load': {
        'type': 'score',
        'instructions': 'How heavy is the binary or media load the task requires?',
        'criteria': [
            'Text only',
            'Office documents or databases',
            'Images or PDFs needing vision or OCR',
            'Huge media files',
        ],
    },
    'references_pattern_not_file': {
        'type': 'noul',
        'instructions': ('Does the task name a pattern or folder in prose instead of '
                         'an exact filename?'),
    },
    'deliverable_named': {
        'type': 'noul',
        'instructions': 'Does the task name a concrete output file or artifact?',
    },
    'deliverable_format_clear': {
        'type': 'choice',
        'instructions': 'Is the required output format clearly specified?',
        'criteria': {
            'yes': 'The format is explicit.',
            'partial': 'The format is only partly specified.',
            'no': 'The format is not specified.',
        },
    },
    'task_category': {
        'type': 'choice',
        'instructions': 'Which category best describes the task?',
        'criteria': {
            'feature': 'New functionality.',
            'bugfix': 'Fix broken behaviour.',
            'refactor': 'Restructure without changing behaviour.',
            'research': 'Investigate and report.',
            'docs': 'Write or update documentation.',
            'data-processing': 'Transform or analyse data.',
            'deployment': 'Ship, deploy, or configure infrastructure.',
            'chore': 'Routine maintenance.',
        },
    },
    'has_user_constraints': {
        'type': 'noul',
        'instructions': ('Does the task contain explicit do-not, never, only, or must '
                         'wording?'),
    },
    'mentions_other_projects_or_tasks': {
        'type': 'noul',
        'instructions': ('Does the task mention other projects or other tasks\' outputs, '
                         'in any ID form?'),
    },
    'risk_score': {
        'type': 'score',
        'instructions': 'Overall, how risky is this task to run without human review?',
        'criteria': ['Routine', 'Needs some care', 'Risky', 'Dangerous'],
    },
    'expects_tool_use': {
        'type': 'choice',
        'instructions': 'What tool use does the task expect from the model?',
        'criteria': {
            'text-answer-only': 'The model only has to answer in text.',
            'read-files': 'The model must read project files.',
            'write-or-edit-files': 'The model must create or edit files.',
            'run-shell-commands': 'The model must run shell commands.',
            'web-fetch': 'The model must fetch a web page or URL.',
            'mixed': 'More than one of the above is required.',
            'unclear': 'The task does not make this clear.',
        },
    },
    'expects_file_creation': {
        'type': 'noul',
        'instructions': 'Does the task expect a new file to be created, even if unnamed?',
    },
}

# Stable order for flags / review lines (P1..P20).
QUESTION_ORDER = tuple(_QSET.keys())
assert len(QUESTION_ORDER) == 20

_FLAG_LABELS = {
    'asks_system_level_commands': 'system-level commands',
    'asks_outside_project_paths': 'outside-project paths',
    'requires_web_research': 'web research',
    'prohibits_project_files': 'prohibits project files',
    'asks_secrets_or_credentials': 'secrets/credentials',
    'asks_external_side_effects': 'external side effects',
    'asks_delete_move': 'delete/move',
    'references_pattern_not_file': 'pattern instead of file',
    'deliverable_named': 'deliverable named',
    'has_user_constraints': 'user constraints',
    'mentions_other_projects_or_tasks': 'other project/task refs',
}

# Questions never rendered by render_h2_block (P18 is report-only; P20 is a
# conditional llm_review line, never a generic flag).
_RENDER_SKIP_FLAGS = frozenset({'risk_score', 'expects_file_creation'})

# ── Worker state (module-level, guarded) ────────────────────────────────────
_QUEUE_LOCK = threading.Lock()
_QUEUE_WAKE = threading.Condition(_QUEUE_LOCK)
_PENDING = {}            # task_id -> project_path (insertion ordered; latest wins)
_EVENTS = {}             # task_id -> threading.Event
_WORKER = None
_DIGEST_CACHE = {}
_DIGEST_CACHE_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════

def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _num(value, default=None):
    try:
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_route_facts(facts):
    """Keep only the deterministic fields the pre-filter hashes/sends.

    ``agent_executor._task_route_facts`` embeds the full, uncapped description;
    duplicating it would bloat both the prompt state and the hash input.
    """
    if not isinstance(facts, dict):
        return {}
    try:
        clean = {}
        for key in ('mode', 'route', 'file_tools', 'web', 'known_route'):
            if key in facts:
                clean[key] = facts[key]
        if 'vision' in facts:
            clean['vision'] = facts['vision']
        return clean
    except Exception:
        return {}


def _existing_payload(task):
    """Parse ``prefilter_json`` off a task dict (str or dict) tolerantly."""
    try:
        raw = (task or {}).get('prefilter_json')
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def _attempts_so_far(task):
    payload = _existing_payload(task) or {}
    n = payload.get('attempts')
    try:
        return max(0, int(n))
    except (TypeError, ValueError):
        return 0


def _noul_prob(answer):
    """Probability that a ``noul`` answer is true, or None when unreadable."""
    if isinstance(answer, dict):
        for key in ('noul', 'probability', 'p'):
            if key in answer:
                val = _num(answer.get(key))
                if val is not None:
                    return max(0.0, min(1.0, val))
        # Some servers only return confidence for a boolean.
        if 'value' in answer:
            return _noul_prob(answer.get('value'))
        return None
    if isinstance(answer, bool):
        return 1.0 if answer else 0.0
    val = _num(answer)
    if val is None:
        return None
    return max(0.0, min(1.0, val))


def _choice_dist(answer):
    """(top_label, top_p, second_p) for a choice answer, or None."""
    if not isinstance(answer, dict):
        return None
    probs = answer.get('probabilities')
    if isinstance(probs, dict) and probs:
        pairs = []
        for label, p in probs.items():
            val = _num(p)
            if val is not None:
                pairs.append((val, str(label)))
        if not pairs:
            return None
        pairs.sort(reverse=True)
        top_p, top_label = pairs[0]
        second_p = pairs[1][0] if len(pairs) > 1 else 0.0
        return top_label, top_p, second_p
    label = answer.get('choice')
    if label is None:
        return None
    return str(label), _num(answer.get('confidence'), 1.0), 0.0


def _choice_label(answer):
    dist = _choice_dist(answer)
    return dist[0] if dist else None


def _score_value(answer):
    if isinstance(answer, dict):
        return _num(answer.get('score'))
    return _num(answer)


def _round_score(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return 0


def _detector_says_file_creation(task):
    """Lazy wrapper around agent_executor's deterministic write detector."""
    try:
        import agent_executor
        return bool(agent_executor._task_requests_file_creation(task))
    except Exception:
        return False


def _vision_ok(task, route_facts):
    facts = route_facts or {}
    if 'vision' in facts:
        return bool(facts.get('vision'))
    try:
        from model_caps import get_cap
        cap = get_cap((task or {}).get('model')) or {}
        if cap.get('file_access') == 'api-ocr':
            return True  # the OCR route reads the images itself
        if 'image_inputs' in facts:
            # A vision cap alone is not enough — the route must deliver the
            # image (Vibe's read_file returns bytes; API/batch have no tools).
            return bool(cap.get('vision')) and bool(facts.get('image_inputs'))
        return bool(cap.get('vision'))
    except Exception:
        # Fail-open: a caps lookup error must not manufacture a review line.
        return True


def _is_text_only(route_facts):
    facts = route_facts or {}
    return not bool(facts.get('file_tools')) and not bool(facts.get('tools'))


def _read_settings():
    try:
        import agent_db
        return agent_db.get_app_settings() or {}
    except Exception:
        return {}


def _project_id_for_path(path):
    try:
        import agent_db
        row = agent_db.get_project_by_path(path) or {}
        return row.get('id')
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════
# Endpoint
# ══════════════════════════════════════════════════════════════════════════

def loopback_ok(base_url):
    """True only for loopback hosts (127.0.0.1 / localhost / [::1])."""
    try:
        parsed = urlparse(base_url or '')
        if parsed.scheme not in ('http', 'https'):
            return False
        host = (parsed.hostname or '').lower()
        return host in ('127.0.0.1', 'localhost', '::1')
    except Exception:
        return False


def decide(state, questions, model, *, base_url=LOCAL_BASE_URL,
           keep_alive=_FAST_KEEP_ALIVE, timeout=_REQUEST_TIMEOUT):
    """One ``/v1/systemone`` decision call.

    The request body is exactly ``{model, state, questions, keep_alive}``.
    ``options`` is deliberately never sent: the endpoint silently ignores it
    and context/threading are model/server settings, not per-request ones.

    Returns ``{'status', 'model', 'answers', 'usage', 'error'}`` and never
    raises.  Non-loopback base URLs fail closed to ``status='unknown'``.
    """
    result = {'status': 'unknown', 'model': model, 'answers': {},
              'usage': {}, 'error': None}
    try:
        if not loopback_ok(base_url):
            result['error'] = f'refused non-loopback base_url: {base_url!r}'
            _log.warning('agent_decision: %s', result['error'])
            return result
        if not isinstance(questions, dict) or not questions:
            result['error'] = 'no questions supplied'
            return result

        body = {
            'model': model,
            'state': state,
            'questions': questions,
            'keep_alive': keep_alive,
        }
        url = (base_url or LOCAL_BASE_URL).rstrip('/') + '/v1/systemone'
        response = requests.post(url, json=body, timeout=timeout)

        status_code = getattr(response, 'status_code', 200)
        if status_code and int(status_code) >= 400:
            text = ''
            try:
                text = (getattr(response, 'text', '') or '')[:300]
            except Exception:
                text = ''
            result['error'] = f'HTTP {status_code}: {text}'
            return result

        try:
            data = response.json()
        except Exception as exc:
            result['error'] = f'invalid JSON: {exc}'
            return result

        answers = data.get('answers') if isinstance(data, dict) else None
        if isinstance(answers, list):
            # Tolerate a list form: [{id|key: ..., ...}, ...].
            converted = {}
            for item in answers:
                if not isinstance(item, dict):
                    continue
                key = item.get('id') or item.get('key') or item.get('name')
                if key is not None:
                    converted[str(key)] = item
            answers = converted
        if not isinstance(answers, dict):
            result['error'] = 'response has no answers object'
            return result

        result['answers'] = answers
        result['usage'] = (data.get('usage') or {}) if isinstance(data, dict) else {}
        result['status'] = 'ok'
        return result
    except Exception as exc:  # timeout, connection error, anything
        result['error'] = f'{type(exc).__name__}: {exc}'
        _log.warning('agent_decision: decide failed (%s): %s', model, result['error'])
        return result


def model_digest(model):
    """Runner-specific digest of ``model``.

    Preferred source: ``/api/show`` → ``manifests[]`` where ``selected`` is true
    (multi-runner tags expose a parent list digest plus a runner child). Some
    Ollama builds do not return ``manifests`` from ``/api/show`` at all; the
    fallback is the per-model ``digest`` from ``GET /api/tags``. Cached per
    process; ``''`` on any error. Never raises.
    """
    try:
        with _DIGEST_CACHE_LOCK:
            if model in _DIGEST_CACHE:
                return _DIGEST_CACHE[model]
        if not loopback_ok(LOCAL_BASE_URL):
            return ''
        response = requests.post(
            LOCAL_BASE_URL.rstrip('/') + '/api/show',
            json={'model': model}, timeout=_SHOW_TIMEOUT)
        if int(getattr(response, 'status_code', 200) or 200) >= 400:
            return ''
        data = response.json() or {}
        digest = ''
        manifests = data.get('manifests') or []
        if isinstance(manifests, list):
            for item in manifests:
                if isinstance(item, dict) and item.get('selected'):
                    digest = str(item.get('digest') or '')
                    if digest:
                        break
        if not digest:
            digest = str(data.get('digest') or '')
        if not digest:
            # Fallback for builds whose /api/show has no manifests/digest.
            try:
                tags = requests.get(
                    LOCAL_BASE_URL.rstrip('/') + '/api/tags',
                    timeout=_SHOW_TIMEOUT)
                if int(getattr(tags, 'status_code', 200) or 200) < 400:
                    wanted = {model}
                    if ':' not in model:
                        wanted.add(model + ':latest')
                    for item in ((tags.json() or {}).get('models') or []):
                        if not isinstance(item, dict):
                            continue
                        name = str(item.get('name') or item.get('model') or '')
                        if name in wanted:
                            digest = str(item.get('digest') or '')
                            if digest:
                                break
            except Exception:
                digest = ''
        if digest:
            with _DIGEST_CACHE_LOCK:
                _DIGEST_CACHE[model] = digest
        return digest
    except Exception as exc:
        _log.debug('agent_decision: model_digest(%s) failed: %s', model, exc)
        return ''


def _show_payload(model):
    try:
        if not loopback_ok(LOCAL_BASE_URL):
            return None
        response = requests.post(
            LOCAL_BASE_URL.rstrip('/') + '/api/show',
            json={'model': model}, timeout=_SHOW_TIMEOUT)
        if int(getattr(response, 'status_code', 200) or 200) >= 400:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _num_ctx_from_show(data):
    """Best-effort ``num_ctx`` from ``parameters`` or ``model_info``."""
    if not isinstance(data, dict):
        return None
    params = data.get('parameters')
    if isinstance(params, str):
        for line in params.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0].lower() == 'num_ctx':
                return int(parts[1])
    if isinstance(params, dict):
        for key, value in params.items():
            if str(key).lower() == 'num_ctx':
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
    info = data.get('model_info')
    if isinstance(info, dict):
        for key, value in info.items():
            if str(key).lower().endswith('context_length'):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
    return None


def self_check():
    """Startup self-check: capabilities contain ``decision`` and num_ctx 8192."""
    out = {'ok': False, 'num_ctx': None, 'capabilities': []}
    try:
        data = _show_payload(LOCAL_DEC_FAST)
        if not data:
            return out
        caps = data.get('capabilities') or []
        if isinstance(caps, dict):
            caps = list(caps.keys())
        caps = [str(c) for c in caps]
        out['capabilities'] = caps
        out['num_ctx'] = _num_ctx_from_show(data)
        out['ok'] = ('decision' in caps and out['num_ctx'] == 8192)
    except Exception as exc:
        _log.debug('agent_decision: self_check failed: %s', exc)
    return out


# ══════════════════════════════════════════════════════════════════════════
# State / questions / hash
# ══════════════════════════════════════════════════════════════════════════

def build_state(task, project=None, route_facts=None):
    """Build the model state: title + capped description + route facts.

    Reuses ``agent_overseer._description_for_review`` (the clarifications
    block is never truncated) and ``agent_executor._task_route_facts``.
    Returns ``{'status': 'skipped_trivial'}`` plus the state for short
    descriptions.  Never raises.
    """
    task = task or {}
    try:
        description = task.get('description') or ''
        try:
            from agent_overseer import _description_for_review
            description = _description_for_review(description, STATE_DESC_CHARS)
        except Exception:
            if len(description) > STATE_DESC_CHARS:
                description = description[:STATE_DESC_CHARS] + '…[truncated]'

        facts = route_facts
        if facts is None:
            try:
                import agent_executor
                facts = agent_executor._task_route_facts(task, 'inline')
            except Exception:
                facts = {}
        facts = _clean_route_facts(facts)

        state = {
            'title': task.get('title') or '',
            'description': description,
            'route_facts': facts,
        }
        if len(description.strip()) < MIN_DESCRIPTION_CHARS:
            state['status'] = 'skipped_trivial'
        return state
    except Exception as exc:
        _log.warning('agent_decision: build_state failed: %s', exc)
        return {'title': '', 'description': '', 'route_facts': {}}


def questions_for_tier():
    """The P1..P20 question dict, ``{key: {type, instructions, criteria}}``."""
    return copy.deepcopy(_QSET)


def state_hash(state, fast_digest, escalate_digest='', qset_version=QSET_VERSION):
    """sha256(title + capped state + digests + qset_version). Never raises."""
    try:
        if isinstance(state, dict):
            material = {
                'title': state.get('title') or '',
                'description': state.get('description') or '',
                'route_facts': _clean_route_facts(state.get('route_facts') or {}),
            }
        else:
            material = {'state': state}
        material['fast_digest'] = fast_digest or ''
        material['escalate_digest'] = escalate_digest or ''
        material['qset_version'] = int(qset_version)
        blob = json.dumps(material, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode('utf-8')).hexdigest()
    except Exception:
        return ''


def is_stale(payload, task, project=None, route_facts=None):
    """True when a stored payload is missing, not ok, or hash-mismatched."""
    try:
        if not isinstance(payload, dict) or not payload:
            return True
        status = payload.get('status')
        if status not in ('ok', 'skipped_trivial'):
            return True
        state = build_state(task, project, route_facts)
        recorded = payload.get('digests') or {}
        if status == 'skipped_trivial':
            fast = recorded.get('fast') or ''
            return (state_hash(state, fast, '', payload.get('qset_version')
                               or QSET_VERSION) != payload.get('state_hash'))
        fast = model_digest(LOCAL_DEC_FAST) or recorded.get('fast') or ''
        escalate = ''
        if payload.get('model_escalate'):
            escalate = model_digest(LOCAL_DEC_ESCALATE) or recorded.get('escalate') or ''
        current = state_hash(state, fast, escalate,
                             payload.get('qset_version') or QSET_VERSION)
        return current != payload.get('state_hash')
    except Exception:
        return True


# ══════════════════════════════════════════════════════════════════════════
# Pre-filter compute
# ══════════════════════════════════════════════════════════════════════════

def _review_lines(answers, flags, route_facts, task):
    """Signal-only llm_review lines derived from the final answers."""
    lines = []
    facts = route_facts or {}
    flag_set = set(flags or ())

    read_scope = _score_value(answers.get('read_scope'))
    if read_scope is not None and read_scope >= SCORE_REVIEW:
        lines.append(f'read_scope={_round_score(read_scope)} (wide file scope — '
                     'check containment)')

    binary_load = _score_value(answers.get('binary_load'))
    if binary_load is not None and binary_load >= SCORE_REVIEW:
        lines.append(f'binary_load={_round_score(binary_load)} (check vision/OCR route)')

    has_web = bool(facts.get('web'))
    has_file_tools = bool(facts.get('file_tools')) or bool(facts.get('tools'))

    if 'requires_web_research' in flag_set and not has_web:
        lines.append('P4 web research requested but the route has no web access')

    tool_expectation = (_choice_label(answers.get('expects_tool_use')) or '').strip()
    if (tool_expectation and tool_expectation not in ('text-answer-only', 'unclear')
            and not has_file_tools):
        lines.append(f'P19 expects {tool_expectation} but the route has no file tools')
    if tool_expectation == 'web-fetch' and not has_web:
        lines.append('P19 web fetch requested but the route has no web access')

    if (binary_load is not None and binary_load >= SCORE_REVIEW
            and not _vision_ok(task, facts)):
        lines.append(f'P11 binary load {_round_score(binary_load)} but the route has '
                     'no vision/OCR')

    if ('expects_file_creation' in flag_set
            and not _detector_says_file_creation(task)
            and _is_text_only(facts)):
        lines.append('possible write task not caught by the auto-switch detector')

    # De-duplicate while preserving order.
    seen, out = set(), []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _finalise_answers(answers, escalated_answers, escalate_ok):
    """Merge escalation answers and compute flags from the final values."""
    esc = escalated_answers or {}

    flags = []
    for key in QUESTION_ORDER:
        qtype = _QSET[key]['type']
        if qtype != 'noul':
            continue
        answer = esc.get(key) if (escalate_ok and key in esc) else answers.get(key)
        p = _noul_prob(answer)
        if p is None:
            continue
        if escalate_ok and key in esc:
            if p >= POST_ESCALATE_FLAG:
                flags.append(key)
        elif p >= FLAG_HIGH:
            flags.append(key)
    return flags


def _escalation_questions(ambiguous):
    return {key: copy.deepcopy(_QSET[key]) for key in QUESTION_ORDER
            if key in (ambiguous or set())}


def prefilter_task(task, project=None):
    """Run the fast tier, escalate ambiguities, return the stored payload."""
    started = time.monotonic()
    task = task or {}
    attempts_prev = _attempts_so_far(task)

    base_payload = {
        'status': 'unknown',
        'model_fast': LOCAL_DEC_FAST,
        'model_escalate': None,
        'answers': {},
        'flags': [],
        'llm_review': [],
        'latency_ms': 0,
        'usage': {},
        'computed_at': _iso_now(),
        'state_hash': '',
        'digests': {'fast': '', 'escalate': None},
        'qset_version': QSET_VERSION,
        'attempts': 1,
        'error': None,
    }

    try:
        state = build_state(task, project)
        if state.get('status') == 'skipped_trivial':
            payload = dict(base_payload)
            payload['status'] = 'skipped_trivial'
            payload['state_hash'] = state_hash(state, '', '', QSET_VERSION)
            payload['attempts'] = 1
            return payload

        fast_digest = model_digest(LOCAL_DEC_FAST)
        questions = questions_for_tier()
        fast = decide(state, questions, LOCAL_DEC_FAST,
                      keep_alive=_FAST_KEEP_ALIVE, timeout=_REQUEST_TIMEOUT)
        if fast.get('status') != 'ok':
            payload = dict(base_payload)
            payload['error'] = fast.get('error') or 'fast-tier call failed'
            payload['answers'] = fast.get('answers') or {}
            payload['usage'] = fast.get('usage') or {}
            payload['state_hash'] = state_hash(state, fast_digest, '', QSET_VERSION)
            payload['digests'] = {'fast': fast_digest, 'escalate': None}
            payload['latency_ms'] = int((time.monotonic() - started) * 1000)
            payload['attempts'] = attempts_prev + 1
            return payload

        answers = dict(fast.get('answers') or {})
        ambiguous = []
        for key in QUESTION_ORDER:
            qtype = _QSET[key]['type']
            answer = answers.get(key)
            if answer is None:
                continue
            if qtype == 'noul':
                p = _noul_prob(answer)
                if p is not None and ESCALATE_LOW <= p < FLAG_HIGH:
                    ambiguous.append(key)
            elif qtype == 'choice':
                dist = _choice_dist(answer)
                if dist and (dist[1] - dist[2]) < CHOICE_MARGIN:
                    ambiguous.append(key)

        escalate_answers = {}
        escalate_usage = {}
        escalate_digest = ''
        model_escalate = None
        escalate_ok = False
        if ambiguous:
            model_escalate = LOCAL_DEC_ESCALATE
            escalate_digest = model_digest(LOCAL_DEC_ESCALATE)
            esc = decide(state, _escalation_questions(ambiguous), LOCAL_DEC_ESCALATE,
                         keep_alive=_ESCALATE_KEEP_ALIVE, timeout=_REQUEST_TIMEOUT)
            if esc.get('status') == 'ok':
                escalate_answers = dict(esc.get('answers') or {})
                escalate_usage = esc.get('usage') or {}
                escalate_ok = True
                answers.update(escalate_answers)
            else:
                _log.warning('agent_decision: escalation failed: %s', esc.get('error'))

        flags = _finalise_answers(
            dict(fast.get('answers') or {}), escalate_answers, escalate_ok)
        review = _review_lines(answers, flags, state.get('route_facts') or {}, task)

        usage_fast = fast.get('usage') or {}
        usage = {
            'input_tokens': int(_num(usage_fast.get('input_tokens'), 0)
                                + _num(escalate_usage.get('input_tokens'), 0)),
            'output_tokens': int(_num(usage_fast.get('output_tokens'), 0)
                                 + _num(escalate_usage.get('output_tokens'), 0)),
            'calls': 2 if ambiguous else 1,
        }
        if model_escalate and not escalate_ok:
            usage['escalation_failed'] = True

        payload = dict(base_payload)
        payload.update({
            'status': 'ok',
            'model_escalate': model_escalate,
            'answers': answers,
            'flags': flags,
            'llm_review': review,
            'latency_ms': int((time.monotonic() - started) * 1000),
            'usage': usage,
            'state_hash': state_hash(state, fast_digest, escalate_digest, QSET_VERSION),
            'digests': {'fast': fast_digest, 'escalate': escalate_digest or None},
            'attempts': 1,
        })
        return payload
    except Exception as exc:
        _log.warning('agent_decision: prefilter_task failed: %s', exc)
        payload = dict(base_payload)
        payload['error'] = f'{type(exc).__name__}: {exc}'
        payload['latency_ms'] = int((time.monotonic() - started) * 1000)
        payload['attempts'] = attempts_prev + 1
        return payload


# ══════════════════════════════════════════════════════════════════════════
# H2 block
# ══════════════════════════════════════════════════════════════════════════

def render_h2_block(payload):
    """Compact, signal-only advisory block; '' unless ``status == 'ok'``."""
    try:
        if not isinstance(payload, dict) or payload.get('status') != 'ok':
            return ''

        lines = []
        if payload.get('stale'):
            lines.append('Note: stale pre-filter result; re-checking in background.')

        flags = [f for f in (payload.get('flags') or [])
                 if f in _QSET and f not in _RENDER_SKIP_FLAGS]
        if flags:
            rendered = []
            for key in flags:
                label = _FLAG_LABELS.get(key, key)
                rendered.append(f'{label} ({_key_code(key)})')
            lines.append('Flags: ' + '; '.join(rendered))

        topics = _choice_label((payload.get('answers') or {}).get('prohibition_topics'))
        if topics and topics not in ('none', 'unclear'):
            lines.append(f'Prohibitions: {topics}')

        review = [str(item) for item in (payload.get('llm_review') or []) if item]
        if review:
            lines.append('Review: ' + '; '.join(review))

        if not lines:
            # Nothing to signal beyond the legend: keep the prompt unchanged.
            return ''
        lines.append('Legend: local decision-model triage — advisory, not authority; '
                     'verify before holding.')

        text = '\n'.join(lines)
        if len(text) > MAX_BLOCK_CHARS:
            text = text[:MAX_BLOCK_CHARS - 1].rstrip() + '…'
        return text
    except Exception:
        return ''


def _key_code(key):
    """P-number for a question key (P1..P20)."""
    try:
        return f'P{QUESTION_ORDER.index(key) + 1}'
    except ValueError:
        return key


# ══════════════════════════════════════════════════════════════════════════
# Feature flag
# ══════════════════════════════════════════════════════════════════════════

def prefilter_mode():
    """0 = off, 1 = listed projects only, 2 = all. Default 0 (fail-closed)."""
    try:
        raw = (_read_settings() or {}).get('prefilter_enabled')
        try:
            mode = int(str(raw).strip())
        except (TypeError, ValueError):
            return 0
        return mode if mode in (0, 1, 2) else 0
    except Exception:
        return 0


def prefilter_enabled(project_path=None):
    """True when the pre-filter should run for ``project_path``."""
    try:
        mode = prefilter_mode()
        if mode == 0:
            return False
        if mode == 2:
            return True
        if not project_path:
            return False
        pid = _project_id_for_path(project_path)
        if pid is None:
            return False
        csv = str((_read_settings() or {}).get('prefilter_project_ids') or '')
        allowed = {part.strip() for part in csv.split(',') if part.strip()}
        return str(pid) in allowed
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════
# Worker / queue
# ══════════════════════════════════════════════════════════════════════════

def _counter(key, delta=1):
    """Atomic counter with a read-modify-write fallback for older agent_db."""
    try:
        import agent_db
        inc = getattr(agent_db, 'increment_app_setting', None)
        if callable(inc):
            inc(key, delta)
            return
        current = int((agent_db.get_app_settings() or {}).get(key) or 0)
        agent_db.set_app_setting(key, current + int(delta))
    except (ImportError, AttributeError):
        try:
            import agent_db
            current = int((agent_db.get_app_settings() or {}).get(key) or 0)
            agent_db.set_app_setting(key, current + int(delta))
        except Exception:
            pass
    except Exception:
        pass


def _set_epoch(key, value):
    try:
        import agent_db
        agent_db.set_app_setting(key, value)
    except Exception:
        pass


def _heartbeat():
    """Both liveness signals; only the single worker thread writes the epoch."""
    _counter('prefilter:worker_last_beat', 1)
    _set_epoch('prefilter:worker_beat_epoch', int(time.time()))


def _mark_done(task_id):
    try:
        with _QUEUE_LOCK:
            event = _EVENTS.get(task_id)
        if event is not None:
            event.set()
    except Exception:
        pass


def note_description_changed(task_id, project_path):
    """Enqueue a task for async pre-filtering (never blocks, never raises)."""
    try:
        if not task_id or not prefilter_enabled(project_path):
            return
        task_id = int(task_id)
        with _QUEUE_WAKE:
            # Latest enqueue wins; the worker compares stored hashes itself.
            if task_id in _PENDING:
                del _PENDING[task_id]
            _PENDING[task_id] = project_path
            while len(_PENDING) > _QUEUE_MAX:
                oldest = next(iter(_PENDING))
                del _PENDING[oldest]
            event = _EVENTS.get(task_id)
            if event is None or event.is_set():
                _EVENTS[task_id] = threading.Event()
            _QUEUE_WAKE.notify()
    except Exception as exc:
        _log.debug('agent_decision: enqueue failed: %s', exc)


def wait_for_prefilter(task_id, max_s=20):
    """Wait at most ``max_s`` for an in-flight prefilter of ``task_id``."""
    try:
        if not task_id:
            return
        with _QUEUE_LOCK:
            event = _EVENTS.get(int(task_id))
        if event is None or event.is_set():
            return
        event.wait(max_s)
    except Exception:
        pass


def _should_compute(task, payload, project_path):
    """Worker decision: is a compute still worthwhile for this task?"""
    try:
        if not payload:
            return True
        status = payload.get('status')
        if status in ('ok', 'skipped_trivial'):
            return is_stale(payload, task, project_path)
        attempts = int(payload.get('attempts') or 0)
        if attempts >= 3:
            # After three failures only a changed state hash retries.
            recorded = payload.get('digests') or {}
            current = state_hash(build_state(task, project_path),
                                 recorded.get('fast') or '')
            return current != payload.get('state_hash')
        return True
    except Exception:
        return True


def _record_result(payload):
    try:
        _counter('prefilter:calls', 1)
        if payload.get('status') == 'unknown':
            _counter('prefilter:errors', 1)
        if payload.get('model_escalate'):
            _counter('prefilter:escalations', 1)
        _counter('prefilter:latency_ms_total', int(payload.get('latency_ms') or 0))
        _counter('prefilter:block_chars_total', len(render_h2_block(payload)))
    except Exception:
        pass


def _process_task(task_id, project_path):
    try:
        if not prefilter_enabled(project_path):
            return
        import agent_db
        task = agent_db.get_task(task_id, project_path=project_path)
        if not task:
            _log.debug('agent_decision: task %s not found; skipped', task_id)
            return
        project_path = project_path or task.get('project_path')
        payload = _existing_payload(task)
        if payload is not None and not _should_compute(task, payload, project_path):
            return
        result = prefilter_task(task, project_path)
        try:
            agent_db.update_task_prefilter(task_id, result, project_path)
        except AttributeError:
            _log.warning('agent_decision: update_task_prefilter missing; payload dropped')
        except Exception as exc:
            _log.warning('agent_decision: persist failed for task %s: %s', task_id, exc)
        _record_result(result)
    except Exception as exc:
        _log.warning('agent_decision: worker failed for task %s: %s', task_id, exc)
    finally:
        _mark_done(task_id)


def _pop_next(timeout=1.0):
    with _QUEUE_WAKE:
        if not _PENDING:
            _QUEUE_WAKE.wait(timeout)
        if not _PENDING:
            return None
        task_id = next(iter(_PENDING))
        project_path = _PENDING.pop(task_id)
        return task_id, project_path


def _worker_loop():
    last_beat = 0.0
    while True:
        now = time.monotonic()
        if now - last_beat >= _WORKER_HEARTBEAT_S:
            _heartbeat()
            last_beat = now
        item = _pop_next()
        if item is None:
            continue
        try:
            _process_task(*item)
        except Exception as exc:  # belt and braces
            _log.warning('agent_decision: worker loop error: %s', exc)


def start_worker():
    """Start the single idempotent daemon pre-filter worker."""
    global _WORKER
    try:
        with _QUEUE_LOCK:
            if _WORKER is not None and _WORKER.is_alive():
                return
            thread = threading.Thread(
                target=_worker_loop, name='prefilter-worker', daemon=True)
            _WORKER = thread
            # Start inside the lock: ``is_alive()`` is False between create and
            # start, so a second caller could otherwise spawn a twin worker.
            thread.start()
    except Exception as exc:
        _log.warning('agent_decision: could not start worker: %s', exc)


# ══════════════════════════════════════════════════════════════════════════
# Test helpers (private)
# ══════════════════════════════════════════════════════════════════════════

def _reset_for_tests():
    """Clear in-memory worker state.  Tests only; never used at runtime."""
    global _WORKER
    with _QUEUE_WAKE:
        _PENDING.clear()
        _EVENTS.clear()
        _QUEUE_WAKE.notify_all()
    _WORKER = None
    with _DIGEST_CACHE_LOCK:
        _DIGEST_CACHE.clear()
