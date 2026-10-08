"""
AI provider router — Phase 2.
Routes task execution to the correct API based on model ID.
Returns: (output_text, tokens_input, tokens_output, cost_usd)
"""
import logging
import os
import re
import json
import subprocess
import time
import shutil
import requests
from model_caps import effective_max_tokens, get_cap
import agent_config
from agent_config import (
    MODELS, DEFAULT_MODEL, ANTHROPIC_API_KEY, OPENAI_API_KEY,
    MISTRAL_VIBE_KEY, MISTRAL_ORG_KEY, SCALEWAY_API_KEY, OLLAMA_API_KEY,
    CLAUDE_CODE_SKIP_PERMISSIONS, MISTRAL_MODE, VIBE_SKIP_PERMISSIONS,
    VIBE_MODELS, VIBE_MAX_PRICE, CODEX_SKIP_PERMISSIONS,
)

def _kill_process_group(proc):
    """Kill a CLI subprocess and every child it spawned.

    The claude/vibe/codex CLIs spawn their own children. Killing only the
    direct child on timeout leaves those grandchildren running, still holding
    the provider session. ``Popen`` is started with ``start_new_session=True``
    so the child leads its own process group and ``killpg`` reaches the tree.
    """
    import signal as _signal
    try:
        os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# Retry configuration for Vibe CLI subprocess calls (requires tenacity)
try:
    from tenacity import (
        retry,
        stop_after_attempt,
        wait_exponential,
        retry_if_exception_type,
    )
    VIBE_CLI_RETRY_AVAILABLE = True
    # subprocess.TimeoutExpired is deliberately NOT retryable: each attempt
    # starts the CLI cold, so a task that outran the wall-clock cap just
    # redoes the same work and times out again (task 10001187: 3 x 15 min).
    VIBE_CLI_RETRYABLE_ERRORS = (RuntimeError,)
except ImportError:
    # Fallback: no retries if tenacity is not installed
    def retry(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    
    def stop_after_attempt(n):
        return lambda retry_state: False
    
    def wait_exponential(*args, **kwargs):
        return lambda retry_state: 0
    
    def retry_if_exception_type(exc_types):
        return lambda retry_state: False
    
    VIBE_CLI_RETRY_AVAILABLE = False
    VIBE_CLI_RETRYABLE_ERRORS = ()

_log = logging.getLogger(__name__)


class _EmptyResponseError(Exception):
    """Raised when an OpenAI-compatible backend returns HTTP 200 with an empty
    or non-JSON body. Treated as a transient transport error by the @retry in
    `_post` so the call is re-issued instead of failing the whole execution.

    Observed on Scaleway's envoy LB: sporadic 200 + empty body mid-stream
    (regression: task #10001058 — failed with
    `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`).
    """


class _TransientHTTPError(Exception):
    """Raised on retryable HTTP responses — 5xx and 429 (rate limit).

    Retried by the @retry in `_post`. 4xx (other than 429) are NOT raised as
    this: they are permanent (e.g. the 400 `max_completion_tokens` payload
    validation, 401 auth, 404 model-not-found) and must surface immediately so
    the caller sees the real error instead of three wasted retry attempts.

    Regression: task #10001058 (second failure) — Scaleway envoy returned
    `504 stream timeout` after a reasoning model stalled; the call was not
    retried because only ReadTimeout/ConnectionError were retryable.
    """

# Vibe resolves VIBE_ACTIVE_MODEL strictly against the `alias` field of each
# `[[models]]` block in ~/.vibe/config.toml — never against `name` (verified
# in vibe/core/config/_settings.py:get_active_model). SuperAgent's canonical
# IDs use the `-latest` API aliases; map each one to the Vibe alias here.
# Update when new models are added to VIBE_MODELS.
_VIBE_MODEL_ID_MAP = {
    'mistral-large-latest':  'mistral-large',
    'mistral-large-4':       'mistral-large-4',
    'mistral-medium-latest': 'mistral-medium-3.5',
    'mistral-small-latest':  'mistral-small',
    'codestral-latest':      'codestral',
    'mistral-glm-5-3':       'zai-glm-5-3',
}

# Wire ID translation for the direct API paths (call_mistral API mode and
# _call_mistral_web_search). SuperAgent's canonical IDs are stable aliases;
# the Mistral API expects the real wire ID. For most models the two are
# identical, but `mistral-glm-5-3` maps to `zai-glm-5-3` (Z.ai GLM-5.3 hosted
# by Mistral). Keep in step with _VIBE_MODEL_ID_MAP — a model added to one
# without the other silently 404s on the API path or fails Vibe resolution.
_MISTRAL_WIRE_ID_MAP = {
    'mistral-glm-5-3': 'zai-glm-5-3',
}


def _mistral_wire_id(model_id):
    """Resolve a SuperAgent Mistral model ID to the wire ID Mistral expects."""
    return _MISTRAL_WIRE_ID_MAP.get(model_id, model_id)

# The vibe prompt is passed on the argv (`--prompt <prompt>`). Linux caps a
# SINGLE argv element at MAX_ARG_STRLEN = 32 pages (~128 KiB on 4 KiB-page
# hosts) — not the 2 MiB total ARG_MAX — so an oversized prompt fails at
# exec() with `OSError [Errno 7] Argument list too long: 'vibe'` before vibe
# even runs (regression: exec #20001013, AIngel review chat post whose
# auto-inlined context pushed the prompt past the per-arg limit). Cap below
# the limit with headroom for UTF-8 multibyte chars; the tail-truncation
# below keeps the newest context. If a task genuinely needs more, it should
# use a tool-loop model (Scaleway/Ollama) or batch mode, not the vibe argv.
VIBE_PROMPT_MAX_CHARS = 100_000
# Byte-level cap actually enforced at exec() time (the OS limit is a byte
# limit; a 100k-char CJK/emoji prompt encodes to 300k bytes). Keep below
# MAX_ARG_STRLEN (~131,072 bytes) with headroom for the argv/env overhead.
VIBE_PROMPT_MAX_BYTES = 120_000

# SuperAgent-owned VIBE_* variables are configuration for this Flask app, not
# for the Vibe CLI. Vibe treats VIBE_* as settings overrides, so forwarding
# VIBE_MODELS=mistral-large-latest,... makes Vibe try to parse it as its own
# complex `models` field and fail before it runs the prompt.
_SUPERAGENT_ONLY_VIBE_ENV = {
    'VIBE_MODELS',
    'VIBE_SKIP_PERMISSIONS',
    'VIBE_CLI_RETRY_ATTEMPTS',
}

CODEX_CHATGPT_DEFAULT_MODEL = 'gpt-6-luna'


def _summarize_permission_denials(denials):
    """Return a compact human-readable summary for CLI permission denials."""
    if not denials:
        return ''
    parts = []
    for denial in denials[:3]:
        tool = denial.get('tool_name') or 'unknown tool'
        tool_input = denial.get('tool_input') or {}
        if isinstance(tool_input, dict):
            # Bash: show the actual command so we know what to allow
            cmd = tool_input.get('command') or tool_input.get('cmd') or ''
            questions = tool_input.get('questions')
            if cmd:
                parts.append(f'{tool}({cmd[:80]})')
                continue
            if questions:
                qs = [q['question'] for q in questions[:3]
                      if isinstance(q, dict) and q.get('question')]
                if qs:
                    parts.append(f'{tool}: ' + ' | '.join(qs))
                    continue
        parts.append(tool)
    if len(denials) > 3:
        parts.append(f'+{len(denials) - 3} more')
    return '; '.join(parts)


def _load_result_envelope(text):
    """Parse a Claude CLI result envelope from raw stdout, tolerating wrappers."""
    if not text:
        return None
    stripped = text.strip()
    if not stripped or '"type"' not in stripped:
        return None

    candidates = [stripped]
    first = stripped.find('{')
    last = stripped.rfind('}')
    if first != -1 and last > first:
        inner = stripped[first:last + 1].strip()
        if inner and inner not in candidates:
            candidates.append(inner)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict) and data.get('type') == 'result':
            return data
    return None


def _flatten_content(raw):
    """Normalise a Mistral message `content` field to a plain string.

    Content is Union[str, List[ContentChunk]] in mistralai 2.x, and the Vibe CLI
    emits the same shape as JSON — so a chunk is either an SDK object with a
    `.text` attribute or a `{"type": "text", "text": …}` dict. Chunks carrying no
    printable payload (tool calls, thinking) are skipped rather than stringified.

    Every caller downstream treats the result as a string (`.strip()`, slicing,
    `len()`), so a list leaking through here fails far from its cause."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for chunk in raw:
            text = chunk.get('text') if isinstance(chunk, dict) else getattr(chunk, 'text', None)
            if isinstance(text, str):
                parts.append(text)
        return ''.join(parts)
    return '' if raw is None else str(raw)


# Vibe's per-run tool-turn budget (`--max-turns`). VIBE_TURN_LIMIT_MARKER is
# appended to the recovered text when a run exhausts it; the executor keys on
# it to skip the simulation detector (80 real turns prove the tools ran).
VIBE_MAX_TURNS = 80
VIBE_TURN_LIMIT_MARKER = 'Vibe CLI hit its turn limit'


class VibeCLIError(RuntimeError):
    """Raised when the Vibe CLI subprocess fails or returns malformed output."""
    def __init__(self, message: str, returncode: int = None, stderr: str = None):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class VibeCLITimeout(Exception):
    """Raised when a Vibe CLI run exceeds VIBE_CLI_TIMEOUT_SECS. Not a
    RuntimeError subclass, so the tenacity retry loop does not restart it."""


class ExecutionCancelledError(Exception):
    """Raised when a provider subprocess is killed because the execution was
    cancelled. Deliberately NOT a RuntimeError subclass (and therefore not in
    VIBE_CLI_RETRYABLE_ERRORS) so the tenacity retry loop stops instead of
    re-spawning the CLI after a cancel."""


def _execution_cancelled(exec_id, project_path=None):
    """True if the execution row is no longer 'running' (i.e. it was cancelled
    or otherwise finished while the provider subprocess was in flight). Used to
    abort the CLI retry loop so a cancelled task does not keep re-running."""
    if exec_id is None:
        return False
    try:
        import agent_db as db
        info = db.get_execution_pid(exec_id, project_path=project_path)
        return info is None or info.get('status') != 'running'
    except Exception:
        return False


def _cost(model_id, tok_in, tok_out):
    """Per-token cost from MODELS. Does not account for Anthropic cache
    read/write pricing — cache tokens are billed at the full input rate
    (conservative upper bound). See PRICING comment in agent_config.py."""
    m = MODELS.get(model_id, MODELS[DEFAULT_MODEL])
    return round((tok_in * m['cost_input'] + tok_out * m['cost_output']) / 1_000_000, 6)


def _pricing_cost(model_id, tok_in, tok_out):
    """Per-token cost from PRICING (fuzzy fallback). Same cache-pricing
    limitation as _cost() — see agent_config.py PRICING comment."""
    pricing = agent_config.get_pricing(model_id)
    if pricing:
        return round((tok_in * pricing['input'] + tok_out * pricing['output']) / 1_000_000, 6)
    _log.warning("No pricing configured for model=%s; recording OpenAI PAYG cost as 0.0", model_id)
    return 0.0


def _call_vibe_cli(model_id, prompt, max_tokens=4096, *, project_path=None, exec_id=None):
    """Route through the Vibe CLI. Tool support (Read/Edit/Write/Bash) gated by
    .vibe/config.toml. Always bills to the Mistral API key's monthly budget
    (Le Chat Pro subscription).

    cwd=project_path so the CLI auto-discovers <project>/.vibe/config.toml.
    --agent accept-edits enables Edit/Write without prompts; Bash stays gated
    by the config file (groups A+B+C globally, D per project)."""
    import subprocess, json, shutil, os
    if not shutil.which('vibe'):
        raise RuntimeError('vibe CLI not found. Install Mistral Vibe: https://github.com/mistralai/vibe')
    if not project_path:
        raise VibeCLIError('project_path is required for Vibe CLI calls')

    if not os.path.isdir(project_path):
        raise VibeCLIError(f'project_path does not exist or is not a directory: {project_path}')

    _timeout = getattr(agent_config, 'VIBE_CLI_TIMEOUT_SECS', 2700)

    @retry(
        stop=stop_after_attempt(getattr(agent_config, 'VIBE_CLI_RETRY_ATTEMPTS', 3)),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(VIBE_CLI_RETRYABLE_ERRORS),
        reraise=True,
    )
    def _run_vibe_cli(cmd, env, cwd):
        # If the execution was cancelled while we were waiting to retry, stop
        # immediately instead of re-spawning the CLI (regression: a cancelled
        # task kept re-running via the retry loop, leaving the slot stuck).
        if _execution_cancelled(exec_id, project_path):
            raise ExecutionCancelledError('execution cancelled')
        # stdin=DEVNULL: vibe's get_prompt_from_stdin() calls sys.stdin.read()
        # unconditionally (even with --prompt); if the parent's fd 0 is a
        # non-TTY (nohup / systemd / closed fd), vibe's pre-read setup can
        # invalidate fd 0 → OSError [Errno 9] Bad file descriptor. Explicit
        # DEVNULL gives vibe a clean, always-EOF stdin regardless of how
        # AIngel was launched. Mirrors _call_claude_cli / _call_codex_cli.
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, cwd=cwd,
                                start_new_session=True)
        # Record the child PID so the /cancel endpoint and orphan sweep can
        # identify/kill the actual provider process if it hangs.
        if exec_id is not None:
            try:
                import agent_db as db
                db.set_execution_pid(exec_id, proc.pid, project_path)
            except Exception:
                pass
        try:
            stdout, stderr = proc.communicate(timeout=_timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            proc.communicate()
            # Not a RuntimeError, so the retry loop stops here. Short message:
            # TimeoutExpired's own str() embeds the whole argv (the prompt).
            raise VibeCLITimeout(
                f'Vibe CLI exceeded the {_timeout // 60}-min run limit '
                f'(VIBE_CLI_TIMEOUT_SECS={_timeout}); run stopped, not retried.')
        # The cancel endpoint may have killed the child mid-run; treat that as
        # a terminal cancellation, not a retryable transport error.
        if _execution_cancelled(exec_id, project_path):
            raise ExecutionCancelledError('execution cancelled')
        # Mimic subprocess.run's CompletedProcess for the rest of the function
        import subprocess as _sp
        result = _sp.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        if result.returncode != 0:
            err = (result.stderr or '').strip() or (result.stdout or '').strip()
            # Vibe exits non-zero when the model exhausts --max-turns without a
            # final answer, emitting `<vibe_stop_event>Turn limit of N
            # reached</vibe_stop_event>`. The conversation so far is still in
            # stdout — recover it instead of discarding everything and hard-
            # failing the task (regression: task #10000698, a client project, which
            # failed with tokens_input=0/tokens_output=0 and no partial output
            # despite ~50 turns of real work). We surface a clear note so the
            # caller knows the run was incomplete.
            if 'Turn limit' in err and 'reached' in err:
                result._vibe_turn_limit = True  # type: ignore[attr-defined]
                return result
            raise RuntimeError(f'vibe CLI error (exit {result.returncode}): {err}')
        return result

    if not MISTRAL_VIBE_KEY:
        raise ValueError('MISTRAL_VIBE_KEY not set in .env (required for Vibe CLI — see CLAUDE.md Mistral routing)')
    # Scrubbed env: only the Vibe provider key(s) are forwarded, not the whole
    # server environment (session secret, DAV token, other provider keys, …).
    _vibe_extra = {
        'MISTRAL_VIBE_KEY': MISTRAL_VIBE_KEY,
        # Vibe's built-in `mistral` provider resolves credentials from
        # MISTRAL_API_KEY. SuperAgent stores the Pro/Vibe key as
        # MISTRAL_VIBE_KEY, so bridge it in without duplicating secrets in
        # ~/.vibe/.env.
        'MISTRAL_API_KEY': MISTRAL_VIBE_KEY,
        # Vibe selects its model from `active_model`; VIBE_* overrides the
        # user's ~/.vibe/config.toml for this call.
        'VIBE_ACTIVE_MODEL': _VIBE_MODEL_ID_MAP.get(model_id, model_id),
    }
    if MISTRAL_ORG_KEY:
        _vibe_extra['MISTRAL_ORG_KEY'] = MISTRAL_ORG_KEY
    env = agent_config.cli_subprocess_env(_vibe_extra)

    # Build the guardrail-prefixed prompt BEFORE constructing cmd, so the
    # final prompt is the single source of truth and we send it exactly once.
    deny_patterns = _get_deny_patterns_for_system_prompt(project_path)
    if deny_patterns:
        prompt = (
            "IMPORTANT: Do NOT run these commands under any circumstances:\n"
            f"{deny_patterns}\n\n"
            "If you need to run any of these, stop and explain why first.\n\n"
        ) + prompt

    # Cap the prompt so it fits on the argv. vibe has no --prompt-file flag and
    # stdin is unusable (see stdin=DEVNULL / EBADF), so an oversized prompt would
    # raise `OSError [Errno 7] Argument list too long` before vibe runs. Keep the
    # tail (the newest context / user request) and note the truncation so the
    # model knows older context was dropped. Measure in BYTES — the OS limit
    # is a byte limit and project content is often multibyte (Polish diacritics,
    # CJK), so a char-count cap alone can let a 100k-char prompt through at
    # >128 KiB encoded. Truncate on a UTF-8 boundary so we never split a
    # character mid-sequence.
    prompt_bytes = prompt.encode('utf-8')
    if len(prompt_bytes) > VIBE_PROMPT_MAX_BYTES:
        truncated = prompt_bytes[-VIBE_PROMPT_MAX_BYTES:].decode('utf-8', errors='ignore')
        prompt = (
            '## Context truncated\n\n'
            'The full context exceeded the size limit, so only the most recent '
            'portion is shown below. Earlier context was dropped.\n\n'
            + truncated
        )

    cmd = [
        'vibe',
        '--prompt', prompt,
        '--agent', 'accept-edits',     # MUST pass — programmatic default is 'auto-approve'
        '--auto-approve',              # approve all tool calls without prompting (programmatic mode)
        '--output', 'json',
        '--max-turns', str(VIBE_MAX_TURNS),
        '--max-price', str(VIBE_MAX_PRICE),
        '--trust',                     # trust project dir so .vibe/config.toml is loaded
    ]
    assert '--agent' in cmd, '_call_vibe_cli: --agent must be present (programmatic default is auto-approve)'
    # `bypass_tool_permissions` is a Vibe config field (vibe/core/config/_settings.py);
    # any field can be overridden via VIBE_<UPPER_CASE_NAME> per Vibe docs.
    if VIBE_SKIP_PERMISSIONS:
        env['VIBE_BYPASS_TOOL_PERMISSIONS'] = 'true'

    result = _run_vibe_cli(cmd, env, project_path)
    vibe_turn_limit = getattr(result, '_vibe_turn_limit', False)
    try:
        data = json.loads(result.stdout)
        # Vibe returns an array of messages; use the last assistant message that
        # carries text. Never fall back to the raw stdout: it is the whole
        # conversation, system prompt included, and that prompt's "do not emit
        # <bash>…</bash>" rule trips the simulation detector (exec 20001185,
        # task #10001192: a turn-limit run whose last message was a bare tool
        # call was failed as "simulated a tool call in text").
        if isinstance(data, list):
            text = ''
            for msg in reversed(data):
                if isinstance(msg, dict) and msg.get('role') == 'assistant':
                    text = _flatten_content(msg.get('content')).strip()
                    if text:
                        break
        elif isinstance(data, dict):
            if data.get('is_error'):
                raise RuntimeError(f'vibe CLI returned error: {data.get("result", "unknown error")}')
            text = _flatten_content(data.get('result')) or result.stdout.strip()
        else:
            text = result.stdout.strip()

        # When Vibe hit its turn limit, the exit was non-zero but we recovered
        # the partial conversation above. Append a clear marker so the caller
        # (and the user, via the output file) knows the run was incomplete —
        # the model kept acting but never produced a final answer.
        if vibe_turn_limit:
            text = (text or '').rstrip() or '_(The model left no final text: its last action was a tool call.)_'
            text += (
                f"\n\n---\n⚠️ **{VIBE_TURN_LIMIT_MARKER} ({VIBE_MAX_TURNS} turns) before "
                "the model produced a final answer.** The text above is the last "
                "assistant message; files it wrote may be incomplete. The task is "
                "likely too large for one run (split it into smaller tasks) or "
                "needs tools the Vibe CLI doesn't provide (e.g. Proton MCP email "
                "tools, external web research)."
            )

        # Vibe CLI output does not include cost/token fields (verified against v2.9.6).
        # We warn here so users know cost tracking is heuristic, not actual.
        _log.warning(
            'Vibe CLI token/cost tracking is heuristic (prompt_length // 4, '
            'completion_length // 4). Meta.json stats are preferred if available.'
        )

        # Token + cost come from the per-session meta.json. The interesting
        # fields are nested under "stats" (verified against Vibe v2.9.6):
        #   meta_info["stats"]["session_prompt_tokens"]
        #   meta_info["stats"]["session_completion_tokens"]
        #   meta_info["stats"]["session_cost"]
        # Heuristic fallback only fires if the meta.json or its stats block
        # is missing — in that case we log a warning so cost drift is visible.
        # Heuristic token ratio: 1 token ≈ 4 chars (empirical for English)
        HEURISTIC_TOKEN_RATIO = 4
        cost = 0.0
        tok_in, tok_out = max(1, len(prompt) // HEURISTIC_TOKEN_RATIO), max(1, len(result.stdout) // HEURISTIC_TOKEN_RATIO)
        used_stats = False
        session_root = os.path.expanduser('~/.vibe/logs/session')
        if os.path.exists(session_root):
            try:
                import glob
                session_dirs = glob.glob(os.path.join(session_root, 'session_*'))
                if session_dirs:
                    # Use the most recent session directory (by modification time)
                    latest_dir = max(session_dirs, key=os.path.getmtime)
                    meta_file = os.path.join(latest_dir, 'meta.json')
                    if os.path.exists(meta_file):
                        try:
                            with open(meta_file, 'r') as f:
                                meta_info = json.load(f)
                            stats = meta_info.get('stats') or {}
                            if 'session_prompt_tokens' in stats:
                                tok_in = stats['session_prompt_tokens']
                            if 'session_completion_tokens' in stats:
                                tok_out = stats['session_completion_tokens']
                            if 'session_cost' in stats:
                                cost = stats['session_cost']
                                used_stats = True
                        except Exception as e:
                            _log.warning(
                                'Failed to read Vibe meta.json. Falling back to heuristic. Error: %s',
                                str(e)
                            )
            except Exception as e:
                _log.warning(
                    'Failed to find Vibe session logs. Falling back to heuristic. Error: %s',
                    str(e)
                )
        if not used_stats:
            _log.warning(
                'Vibe CLI: Using heuristic token/cost (prompt=%d, completion=%d). '
                'Enable debug logging for meta.json inspection.',
                tok_in, tok_out
            )
    except (json.JSONDecodeError, ValueError) as e:
        _log.warning(f'Failed to parse Vibe CLI output: {e}')
        text = result.stdout.strip()
        cost = 0.0
        tok_in  = max(1, len(prompt) // 4)
        tok_out = max(1, len(text) // 4)
    return text, tok_in, tok_out, cost


def _get_deny_patterns_for_system_prompt(project_path):
    """Build the bash deny guardrail prefix from project permissions.
    Returns a string like:
        - rm, sudo, chmod, git push, git reset --hard, ...
    """
    if not project_path:
        return ""
    try:
        from agent_permissions import get_deny_patterns_for_project
        patterns = get_deny_patterns_for_project(project_path)
        if not patterns:
            return ""
        return "- " + "\n- ".join(sorted(patterns))
    except ImportError:
        _log.warning('agent_permissions module not available for deny patterns')
        return ""
    except Exception as e:
        _log.warning(f'_get_deny_patterns_for_system_prompt failed: {e}')
        return ""


def _call_claude_cli(model_id, prompt, max_tokens=4096, *, project_path=None, auth_mode='pro', exec_id=None):
    """Route through the claude CLI. Both auth modes get identical tool support
    (Read/Edit/Write/Bash, gated by .claude/settings.json); they differ only in
    who gets billed.

    auth_mode='pro' — strips ANTHROPIC_API_KEY from subprocess env so the CLI
        falls back to OAuth (Pro/Max subscription). No API credit charge.
    auth_mode='api' — keeps ANTHROPIC_API_KEY in env so the CLI bills via the
        API key (pay-as-you-go API credits).

    cwd=project_path so the CLI auto-discovers <project>/.claude/settings.json.
    --permission-mode acceptEdits enables Edit/Write without prompts; Bash
    stays gated by the .claude/settings.json allow list (groups A+B+C
    globally, D per project)."""
    import subprocess, json, shutil, os
    if not shutil.which('claude'):
        raise RuntimeError('claude CLI not found. Install Claude Code: https://claude.ai/code')
    if not project_path:
        _log.warning('_call_claude_cli: project_path is empty; inheriting server cwd')

    if auth_mode == 'api':
        if not ANTHROPIC_API_KEY:
            raise ValueError('ANTHROPIC_API_KEY not set in .env (required for api mode)')
        # Scrubbed env: only the Anthropic key is forwarded.
        env = agent_config.cli_subprocess_env({'ANTHROPIC_API_KEY': ANTHROPIC_API_KEY})
    elif auth_mode == 'pro':
        # Scrubbed env, no API key — the CLI falls back to its OAuth login.
        env = agent_config.cli_subprocess_env()
    else:
        raise ValueError(f'auth_mode must be "pro" or "api", got {auth_mode!r}')

    cmd = [
        'claude', '--print',
        "--model", model_id,
        '--output-format', 'json',
        '--no-session-persistence',
        '--permission-mode', 'acceptEdits',
    ]
    if CLAUDE_CODE_SKIP_PERMISSIONS:
        cmd.append('--dangerously-skip-permissions')
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env=env, cwd=project_path or None,
                            start_new_session=True)
    if exec_id is not None:
        try:
            import agent_db as db
            db.set_execution_pid(exec_id, proc.pid, project_path)
        except Exception:
            pass
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=900)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()
        raise
    import subprocess as _sp
    result = _sp.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f'claude CLI error (exit {result.returncode}): {err}')
    try:
        data = _load_result_envelope(result.stdout)
        if not data:
            raise RuntimeError(f'claude CLI returned malformed JSON: {result.stdout.strip() or "empty output"}')
        if data.get('is_error'):
            raise RuntimeError(f'claude CLI returned error: {data.get("result", "unknown error")}')
        denials = data.get('permission_denials') or []
        text = (data.get('result') or '').strip()
        # `permission_denials` is a *log* of tool calls blocked during the run,
        # NOT a task-level failure. Claude Code routinely tries a command, gets
        # denied (e.g. the intentionally-blocked `systemctl restart`), and routes
        # around it to finish the task. Only treat denials as fatal when Claude
        # produced no usable result at all — otherwise the work is done and we
        # just append a note about what was blocked.
        if not text:
            if denials:
                detail = _summarize_permission_denials(denials)
                raise RuntimeError(
                    'claude CLI permission denied (no output produced)'
                    + (f': {detail}' if detail else '')
                )
            stop_reason = data.get('stop_reason') or data.get('terminal_reason') or 'unknown'
            raise RuntimeError(f'claude CLI returned an empty result (stop_reason={stop_reason})')
        if denials:
            detail = _summarize_permission_denials(denials)
            text += (
                '\n\n---\n_Note: some tool calls were blocked by the permission '
                f'system during this run (non-fatal): {detail}_'
            )
        cost = float(data.get('total_cost_usd', data.get('cost_usd', 0.0)))
        usage = data.get('usage', {})
        tok_in  = usage.get('input_tokens') or max(1, len(prompt) // 4)
        tok_out = usage.get('output_tokens') or max(1, len(text) // 4)
    except (json.JSONDecodeError, ValueError):
        text = result.stdout.strip()
        cost = 0.0
        tok_in  = max(1, len(prompt) // 4)
        tok_out = max(1, len(text) // 4)
    return text, tok_in, tok_out, cost


def call_anthropic(model_id, prompt, max_tokens=4096, *, project_path=None,
                   force_anthropic_mode=None, exec_id=None, **_):
    """Both modes go through the claude CLI for identical tool support.
    ANTHROPIC_MODE picks the auth/billing path:
      'claude-code' → Pro/Max subscription (OAuth)
      'api'         → API credits (env-var key)

    `force_anthropic_mode` (per-call override, 'claude-code' or 'api') takes
    precedence over the global toggle. Used by the executor to force slot-tasks
    onto the Pro subscription quota regardless of the dashboard's Pro/API
    setting. Tool-execution path is identical in both auth modes."""
    effective_mode = force_anthropic_mode or agent_config.ANTHROPIC_MODE
    auth = 'api' if effective_mode == 'api' else 'pro'
    return _call_claude_cli(model_id, prompt, max_tokens,
                            project_path=project_path, auth_mode=auth,
                            exec_id=exec_id)


def _call_mistral_web_search(model_id, prompt, max_tokens=4096, *, project_path=None, **_):
    """Call Mistral via the Conversations API with web_search enabled.

    Uses the VIBE key (Pro subscription). The Conversations API is the only
    Mistral endpoint that supports the web_search tool — Chat Completions
    returns 'connector not supported' for the same request.

    Returns (text, tok_in, tok_out, cost_usd).
    """
    import urllib.request
    key = MISTRAL_VIBE_KEY
    if not key:
        raise ValueError('MISTRAL_VIBE_KEY not set in .env')

    body = json.dumps({
        'model': _mistral_wire_id(model_id),
        'inputs': prompt,
        'tools': [{'type': 'web_search'}],
        'store': False,
    }).encode()

    req = urllib.request.Request(
        'https://api.mistral.ai/v1/conversations',
        data=body,
        headers={
            'Authorization': f'Bearer {key}',
            'Content-Type': 'application/json',
        },
    )

    resp = json.load(urllib.request.urlopen(req, timeout=300))

    text_parts = []
    sources = []
    for entry in resp.get('outputs', []):
        etype = entry.get('type', '')
        if etype == 'message.output':
            content = entry.get('content', [])
            if isinstance(content, list):
                for chunk in content:
                    ct = chunk.get('type', '')
                    if ct in ('text.chunk', 'text'):
                        text_parts.append(chunk.get('text', ''))
                    elif ct in ('tool.reference.chunk', 'tool_reference') and chunk.get('url'):
                        sources.append(f"[{chunk.get('title', '')}]({chunk.get('url', '')})")
            elif isinstance(content, str):
                text_parts.append(content)

    text = ''.join(text_parts).strip()
    if sources:
        text += '\n\n---\n**Sources:**\n' + '\n'.join(sources)

    tok_in = len(prompt) // 4
    tok_out = len(text) // 4
    cost = _cost(model_id, tok_in, tok_out)

    return text, tok_in, tok_out, cost


def _is_mistral_ocr_model(model_id):
    return (model_id or '').startswith('mistral-ocr')


def _call_mistral_ocr(model_id, prompt, max_tokens=4096, *, project_path=None, exec_id=None, **_):
    """Mistral OCR / Document AI — POST /v1/ocr, not chat.

    Sends each PDF found for the task as a base64 `document_url` to
    `mistral-ocr-latest` (OCR-4.1). Billing is per-page via the Pro
    subscription's included $30 credits ($4/1k OCR, $5/1k Document AI),
    so AIngel records cost $0 like other `mistral-*-latest` models
    (see agent_config: `cost_input 0`). EU-operated, Zero-Data-Retention
    not claimed by Mistral but EU data zone.
    """
    import base64, glob as _glob
    import requests as _requests

    if not MISTRAL_VIBE_KEY:
        raise ValueError('MISTRAL_VIBE_KEY not set in .env (required for Mistral OCR)')
    if not project_path or not os.path.isdir(project_path):
        # No project — fall back to chat path (prompt-only, no files)
        _log.warning('_call_mistral_ocr: no project_path, falling back to empty OCR')
        return '', 0, 0, 0.0

    # Discover PDFs to OCR. For a DU (Dz.U.) batch, prompt will mention
    # DU/publ/OCR — catch all DU*.pdf + publ*.pdf under any Working-Docs folder.
    # Fallback: any PDF in Working Documents.
    import agent_tools as _tools
    folders = getattr(_tools, '_WORKING_DOC_FOLDERS', ('My Docs','Working Docs','Working Documents','working-docs','docs'))
    pdfs = []
    want_du = any(k in (prompt or '').lower() for k in ('du', 'publ', 'ocr', 'dziennik'))
    for fld in folders:
        base = os.path.join(project_path, fld)
        if not os.path.isdir(base):
            continue
        if want_du:
            pdfs.extend(_glob.glob(os.path.join(base, 'DU*.pdf')))
            pdfs.extend(_glob.glob(os.path.join(base, 'DU_*.pdf')))
            pdfs.extend(_glob.glob(os.path.join(base, 'publ*.pdf')))
            pdfs.extend(_glob.glob(os.path.join(base, 'publ_*.pdf')))
        # generic fallback — populated only if DU scan found nothing
        if not pdfs:
            pdfs.extend(_glob.glob(os.path.join(base, '*.pdf')))
    # Deduplicate, sort
    pdfs = sorted({os.path.realpath(p) for p in pdfs})
    if not pdfs:
        _log.warning('_call_mistral_ocr: no PDFs found under %s', project_path)
        return 'No PDFs found to OCR (looked in %s).' % ', '.join(folders), 0, 0, 0.0

    # Limit burst — 41 files / 623 pages is fine sequential; 623 pages at 2-4s avg
    # is ~2 min wall-clock, well within the 15m heartbeat guard.
    headers = {'Authorization': f'Bearer {MISTRAL_VIBE_KEY}', 'Content-Type': 'application/json'}
    all_md = []
    total_pages = 0
    total_bytes = 0
    t0 = time.time()
    for pdf in pdfs:
        try:
            b64 = base64.b64encode(open(pdf, 'rb').read()).decode()
        except Exception as e:
            _log.warning('_call_mistral_ocr read failed %s: %s', pdf, e)
            continue
        size = os.path.getsize(pdf)
        payload = {
            'model': 'mistral-ocr-latest',
            'document': {'type': 'document_url', 'document_url': f'data:application/pdf;base64,{b64}'},
            'include_image_base64': False
        }
        # retry once on 429/5xx (same tenacity as chat)
        for attempt in (1, 2):
            try:
                resp = _requests.post('https://api.mistral.ai/v1/ocr', headers=headers, json=payload, timeout=300)
                if resp.status_code == 200:
                    break
                if resp.status_code in (429, 500, 502, 503, 504) and attempt == 1:
                    time.sleep(5)
                    continue
                raise RuntimeError(f'Mistral OCR HTTP {resp.status_code}: {resp.text[:600]}')
            except _requests.exceptions.ReadTimeout:
                if attempt == 1:
                    time.sleep(5)
                    continue
                raise
        data = resp.json()
        pages = data.get('usage_info', {}).get('pages_processed', len(data.get('pages', [])))
        total_pages += pages
        total_bytes += size
        md = '\n\n'.join(p.get('markdown', '') for p in data.get('pages', []))
        # Persist per-file so the task's file-write step is trivial and verifiable
        try:
            stem = os.path.splitext(os.path.basename(pdf))[0]
            # Write to OCR_mistral (same dir as the ephemeral batch) for dedup
            out_dir = os.path.join(project_path, 'Working Documents', 'OCR_mistral')
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f'{stem}.md')
            # Only overwrite if changed or missing — skip if identical (idempotent)
            if not os.path.exists(out_path) or open(out_path, encoding='utf-8').read() != md:
                with open(out_path, 'w', encoding='utf-8') as fh:
                    fh.write(md)
        except Exception as e:
            _log.warning('_call_mistral_ocr write failed %s: %s', pdf, e)
        all_md.append(f'# {os.path.basename(pdf)} — {pages} pages\n\n' + md)
        if exec_id:
            try:
                import agent_db as _db
                _db.touch_execution_heartbeat(exec_id, _db.get_project_db(project_path) if False else None)
            except Exception:
                pass
        time.sleep(0.25)

    dt = time.time() - t0
    full = f"_OCR via {model_id}: {len(pdfs)} PDFs, {total_pages} pages, {dt:.1f}s_\n\n" + '\n\n---\n\n'.join(all_md)
    # Index CSV for audit
    try:
        idx = os.path.join(project_path, 'Working Documents', 'OCR_mistral', '_index.csv')
        if not os.path.exists(idx):
            import csv as _csv
            with open(idx, 'w', newline='', encoding='utf-8') as fh:
                w = _csv.writer(fh)
                w.writerow(['pdf','pages','chars','cost_usd'])
                for pdf in pdfs:
                    stem = os.path.splitext(os.path.basename(pdf))[0]
                    p = os.path.join(project_path, 'Working Documents', 'OCR_mistral', f'{stem}.md')
                    chars = len(open(p, encoding='utf-8').read()) if os.path.exists(p) else 0
                    # pages from per-file json if present, else 0
                    pages = 0
                    cost = pages * 4 / 1000
                    w.writerow([os.path.basename(pdf), pages, chars, f"{cost:.4f}"])
    except Exception as e:
        _log.warning('_call_mistral_ocr index write failed: %s', e)

    # Token fields are heuristic for the UI (OCR bills per-page, not per-token)
    tok_in = max(1, len(prompt or '') // 4 + total_bytes // 4)
    tok_out = max(1, len(full) // 4)
    # Cost recorded as $0 — billed via Pro subscription credits, not per-token
    _log.info('Mistral OCR done: %d PDFs %d pages %d chars %.1fs (cost $0 subscription, ~$%.2f at $4/1k pages)', len(pdfs), total_pages, len(full), dt, total_pages*4/1000)
    # EU audit: bytes_out is the PDF bytes sent (not prompt length)
    try:
        agent_config.eu_audit(project_path, model=model_id, provider='mistral', allowed=True, caller='ocr', bytes_out=total_bytes)
    except Exception:
        pass
    return full, tok_in, tok_out, 0.0


def call_mistral(model_id, prompt, max_tokens=4096, *, project_path=None,
                 force_mistral_mode=None, web_search=False, exec_id=None, **_):
    """Route Mistral models. Two modes:
    - 'vibe' (CLI path): full tool support, bills to Le Chat Pro subscription
    - 'api'  (SDK path): text-only fallback, bills to API credits

    `force_mistral_mode` (per-call override, 'vibe' or 'api') takes precedence
    over the global toggle. Used by the executor to force slot-tasks onto
    the Vibe path regardless of the dashboard's Vibe/API setting.

    `web_search` routes API-mode calls through the Conversations API with the
    web_search tool enabled (the Chat Completions API rejects it). Ignored in
    vibe mode — vibe has its own toolset.

    `mistral-ocr-latest` is routed to `_call_mistral_ocr` (POST /v1/ocr),
    not to chat. It is EU-compliant and Pro-covered like other
    `mistral-*-latest` models.
    """
    if _is_mistral_ocr_model(model_id):
        return _call_mistral_ocr(model_id, prompt, max_tokens, project_path=project_path, exec_id=exec_id)

    # Read live: Settings › General changes agent_config.MISTRAL_MODE at run
    # time; the name imported above is a startup copy and never sees that.
    effective_mode = force_mistral_mode or agent_config.MISTRAL_MODE

    if effective_mode == 'vibe' and model_id not in VIBE_MODELS:
        _log.warning(f'Model {model_id} not in VIBE_MODELS allowlist; falling back to API mode')
        effective_mode = 'api'

    if effective_mode == 'vibe':
        vibe_bin = shutil.which('vibe') or os.path.join(os.path.expanduser('~'), '.local', 'bin', 'vibe')
        if not os.path.isfile(vibe_bin) and not shutil.which('vibe'):
            _log.warning('Vibe CLI not found on this host; falling back to API mode')
            effective_mode = 'api'
        else:
            return _call_vibe_cli(model_id, prompt, max_tokens,
                                  project_path=project_path, exec_id=exec_id)

    if effective_mode == 'api':
        # API mode (text-only fallback). Bills to the Pro subscription's
        # included quota via MISTRAL_VIBE_KEY (APIKeyScope.vibe).
        # See CLAUDE.md "Mistral key/provider routing".
        if web_search:
            return _call_mistral_web_search(model_id, prompt, max_tokens, project_path=project_path)
        from mistralai.client import Mistral
        key = MISTRAL_VIBE_KEY
        if not key:
            raise ValueError('MISTRAL_VIBE_KEY not set in .env')
        # 300s read timeout; SDK default falls through to httpx's 60s which
        # is too tight for longer Mistral completions.
        client = Mistral(api_key=key, timeout_ms=300_000)

        @retry(
            stop=stop_after_attempt(getattr(agent_config, 'MISTRAL_API_RETRY_ATTEMPTS', 3)),
            wait=wait_exponential(multiplier=1, min=10, max=30),
            reraise=True,
        )
        def _complete():
            return client.chat.complete(
                model=_mistral_wire_id(model_id),
                messages=[{'role': 'user', 'content': prompt}],
                max_tokens=max_tokens,
            )

        r = _complete()
        raw = r.choices[0].message.content if r.choices else ''
        finish_reason = getattr(r.choices[0], 'finish_reason', None) if r.choices else None
        text = _flatten_content(raw)
        if finish_reason == 'length':
            _log.warning(
                f"Mistral API response truncated at max_tokens={max_tokens} "
                f"(model={model_id}, completion_tokens={r.usage.completion_tokens}). "
                f"Consider raising the caller's max_tokens."
            )
        tok_in  = r.usage.prompt_tokens or 0
        tok_out = r.usage.completion_tokens or 0
        return text, tok_in, tok_out, _cost(model_id, tok_in, tok_out)


def _call_codex_cli(model_id, prompt, max_tokens=4096, *, project_path=None, exec_id=None):
    """Route OpenAI/Codex models through the Codex CLI.

    This is the action-capable OpenAI path for SuperAgent: Codex runs in the
    target project with workspace-write sandboxing, so it can inspect and edit
    files. Auth is handled by the local Codex CLI login (ChatGPT or API key),
    not by OPENAI_API_KEY inside SuperAgent.
    """
    import os, shutil, subprocess, tempfile

    if not shutil.which('codex'):
        raise RuntimeError('Codex CLI not found. Install Codex CLI and run `codex login`.')
    if not project_path:
        raise RuntimeError('project_path is required for Codex CLI calls')
    if not os.path.isdir(project_path):
        raise RuntimeError(f'project_path does not exist or is not a directory: {project_path}')

    with tempfile.NamedTemporaryFile(prefix='superagent-codex-', suffix='.txt', delete=False) as f:
        output_path = f.name

    cmd = [
        'codex',
        '--ask-for-approval', 'never',
        'exec',
        '--cd', project_path,
        '--sandbox', 'workspace-write',
        '--skip-git-repo-check',
        '--ephemeral',
        '--output-last-message', output_path,
        prompt,
    ]
    codex_model = (
        os.getenv('CODEX_CHATGPT_MODEL', CODEX_CHATGPT_DEFAULT_MODEL)
        if model_id == 'codex-chatgpt'
        else model_id
    )
    cmd[8:8] = ['--model', codex_model]
    if CODEX_SKIP_PERMISSIONS:
        cmd.insert(1, '--dangerously-bypass-approvals-and-sandbox')

    # Scrubbed env: Codex uses its own subscription auth from ~/.codex, so no
    # provider key needs forwarding.
    env = agent_config.cli_subprocess_env()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env=env, cwd=project_path,
                            start_new_session=True)
    if exec_id is not None:
        try:
            import agent_db as db
            db.set_execution_pid(exec_id, proc.pid, project_path)
        except Exception:
            pass
    try:
        stdout, stderr = proc.communicate(input='', timeout=1800)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()
        raise
    import subprocess as _sp
    result = _sp.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    try:
        with open(output_path, 'r', encoding='utf-8') as f:
            text = f.read().strip()
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or text
        raise RuntimeError(f'codex CLI error (exit {result.returncode}): {err}')
    if not text:
        text = result.stdout.strip()

    # Codex CLI does not expose token/cost accounting in the non-interactive
    # final message. Treat subscription-auth runs as zero API spend and keep
    # heuristic token counts for dashboard usage telemetry.
    tok_in = max(1, len(prompt) // 4)
    tok_out = max(1, len(text) // 4)
    return text, tok_in, tok_out, 0.0


def _call_openai_api(model_id, prompt, max_tokens=4096):
    """Direct OpenAI API path for PAYG text calls.

    Codex CLI models stay on _call_codex_cli() so action-capable ChatGPT/Codex
    subscription runs never light the PAYG activity indicator.
    """
    if not OPENAI_API_KEY:
        raise ValueError('OPENAI_API_KEY not set in .env')

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    _log.info("OpenAI PAYG call started: model=%s", model_id)
    try:
        r = client.chat.completions.create(
            model=model_id,
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=max_tokens,
        )
        text = r.choices[0].message.content if r.choices else ''
        usage = getattr(r, 'usage', None)
        tok_in = getattr(usage, 'prompt_tokens', 0) or 0
        tok_out = getattr(usage, 'completion_tokens', 0) or 0
        return text or '', tok_in, tok_out, _pricing_cost(model_id, tok_in, tok_out)
    finally:
        _log.info("OpenAI PAYG call ended: model=%s", model_id)


def call_openai(model_id, prompt, max_tokens=4096, *, project_path=None, exec_id=None, **_):
    if model_id == 'codex-chatgpt' or model_id.startswith('codex-'):
        return _call_codex_cli(model_id, prompt, max_tokens,
                               project_path=project_path, exec_id=exec_id)
    return _call_openai_api(model_id, prompt, max_tokens)


# Maps SuperAgent scw-* IDs → actual Scaleway Generative API model IDs
_SCW_MODEL_MAP = {
    'scw-deepseek-v4-flash': 'deepseek-v4-flash-0731',
    'scw-qwen3.8-27b':       'qwen3.8-27b',
    'scw-gpt-oss-120b':      'gpt-oss-120b',
    'scw-llama-3.3-70b':     'llama-3.3-70b-instruct',
    'scw-mistral-small-24b': 'mistral-small-3.2-24b-instruct-2506',
    'scw-gemma-4-26b':       'gemma-4-26b-a4b-it',
    'scw-mistral-medium-128b': 'mistral-medium-3.5-128b',
    'scw-qwen3.6-35b':      'qwen3.6-35b-a3b',
    'scw-qwen3-235b':       'qwen3-235b-a22b-instruct-2507',
    'scw-qwen3.5-397b':     'qwen3.5-397b-a17b',
    'scw-glm-5.2':          'glm-5.2',
}


# Safety cap on the agentic loop. Each write_file/patch_file costs *two* turns
# (the write, then the Phase 7.2 read-back nudge), so a task producing ~7 files
# needs ~14 turns for writes alone plus exploration. 20 starved task #682.
_SCW_MAX_TOOL_TURNS = 40


_SCW_API_URL = 'https://api.scaleway.ai/v1/chat/completions'


# Maps SuperAgent oll-* IDs → actual Ollama Cloud model tags.
# Verify the exact live tags at deploy time:
#   curl -H "Authorization: Bearer $OLLAMA_API_KEY" https://ollama.com/api/tags
# (or browse ollama.com/search?c=cloud). Stale tags surface as a 404 from the endpoint.
_OLL_MODEL_MAP = {
    'oll-glm-5.3':      'glm-5.3',
    'oll-glm-5.3-flash': 'glm-5.3-flash',
    'oll-mistral-large-4': 'mistral-large-4',
    'oll-deepseek-v4-pro': 'deepseek-v4-pro:0813',
    'oll-deepseek-v4.1-flash': 'deepseek-v4.1-flash',
    'oll-kimi-k2.6':    'kimi-k2.6',
    'oll-minimax-m2.7': 'minimax-m2.7',
    'oll-glm-5.2':      'glm-5.2',
    'oll-gpt-oss-120b': 'gpt-oss:120b',
    'oll-gpt-oss-20b':  'gpt-oss:20b',
    'oll-gemma4-31b':   'gemma4:31b',
    'oll-kimi-k2.7-code': 'kimi-k2.7-code',
    'oll-kimi-k3':      'kimi-k3',
    'oll-minimax-m3':   'minimax-m3',
    'oll-mistral-large-675b': 'mistral-large-3:675b',
    'oll-nemotron-3-nano':  'nemotron-3-nano:30b',
    'oll-nemotron-3-super': 'nemotron-3-super',
    'oll-nemotron-3-ultra': 'nemotron-3-ultra',
}


_OLL_MAX_TOOL_TURNS = 40  # safety cap on the agentic loop (see _SCW_MAX_TOOL_TURNS)


# Ollama Cloud OpenAI-compatible endpoint (US, subscription-metered, tool calling supported)
_OLLAMA_API_URL = 'https://ollama.com/v1/chat/completions'

# Regex to strip paired tool-call markup that can false-positive the Phase 7.2
# simulation detector when a model hits max_turns and emits frustrated tool
# markup in its forced final text. The real tool actions are already recorded
# as "→ tool" lines in the transcript; this only cleans the final forced reply.
_STRIP_TOOL_MARKUP_RE = re.compile(
    r'<bash>.*?</bash>'
    r'|<tool_code>.*?</tool_code>'
    r'|<function_call>.*?</function_call>'
    r'|<function=write_file>.*?</function>'
    r'|<antml:invoke>.*?</antml:invoke>'
    r'|<tool_call>.*?</tool_call>',
    re.DOTALL | re.IGNORECASE,
)


def _run_openai_tool_loop(*, provider_label, api_url, api_key, wire_model,
                          model_id, prompt, max_tokens, max_turns, project_path,
                          exec_id=None):
    """Shared OpenAI-compatible function-calling loop used by both Scaleway and
    Ollama Cloud. Uses `requests` directly (not the OpenAI client) to avoid Pydantic
    serialization quirks that make some OpenAI-compatible backends reject multi-turn
    tool-call bodies. Returns (text, tok_in, tok_out, cost_usd).

    `provider_label` is only used for logging/error text. Tool schemas, dispatch, and
    the Phase 7.2 read-back nudge are identical across providers.
    """
    import agent_tools
    import requests as _requests

    use_tools = bool(project_path and os.path.isdir(project_path))
    _log.info(
        "%s API call: superagent_id=%s wire_id=%s tools=%s",
        provider_label, model_id, wire_model, use_tools,
    )

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    def _post(body):
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8')

        @retry(
            stop=stop_after_attempt(getattr(agent_config, 'SCW_HTTP_RETRY_ATTEMPTS', 3)),
            wait=wait_exponential(multiplier=1, min=10, max=30),
            retry=retry_if_exception_type((_requests.exceptions.ReadTimeout,
                                           _requests.exceptions.ConnectionError,
                                           _EmptyResponseError,
                                           _TransientHTTPError)),
            reraise=True,
        )
        def _do_post():
            resp = _requests.post(api_url, headers=headers, data=raw, timeout=300)
            if resp.status_code != 200:
                # 5xx and 429 are transient (LB/upstream hiccup, rate limit,
                # reasoning-model stream timeout). 4xx are permanent and must
                # surface immediately — retrying a 400 payload-validation error
                # wastes three attempts and hides the real cause.
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise _TransientHTTPError(
                        f"Error code: {resp.status_code} - {resp.text or resp.reason}")
                raise Exception(f"Error code: {resp.status_code} - {resp.text or resp.reason}")
            # Scaleway's envoy LB occasionally returns HTTP 200 with an empty
            # body (upstream hiccup / connection drop mid-stream). `resp.json()`
            # then raises JSONDecodeError("Expecting value: line 1 column 1
            # (char 0)") which, before this guard, failed the whole execution
            # (regression: task #10001058). Treat it as a transient transport
            # error so the @retry above re-issues the call.
            try:
                return resp.json()
            except ValueError:
                # `resp.text` may be absent on minimal fakes (tests); fall back
                # to getattr so we never mask the original parse error.
                body = getattr(resp, 'text', '') or ''
                if not body.strip():
                    raise _EmptyResponseError(
                        f"{provider_label} returned 200 with empty body (model={wire_model})")
                raise _EmptyResponseError(
                    f"{provider_label} returned 200 with non-JSON body "
                    f"(model={wire_model}, first 80 bytes={body[:80]!r})")

        return _do_post()

    try:
        # Sanitize prompt: strip null bytes and invalid UTF-8 sequences.
        prompt = prompt.encode('utf-8', 'replace').decode('utf-8')

        messages = [{'role': 'user', 'content': prompt}]
        tools = agent_tools.TOOL_SCHEMAS if use_tools else None

        tok_in_total = tok_out_total = 0
        text = ''
        # Agentic transcript: the model's real "output" is the whole session
        # (narration + the tool actions it took), not just the last message.
        # We accumulate it here so the Exec Log "View" shows what actually
        # happened. Action lines use plain `→ tool` markers (no <bash>/<invoke>
        # tag pairs) so they never trip the Phase 7.2 simulation detector.
        transcript = []

        def _record_step(narration, tool_calls):
            narration = _flatten_content(narration)
            if narration.strip():
                transcript.append(narration.strip())
            lines = []
            for _tc in tool_calls or []:
                _fn = _tc.get('function', {}).get('name', 'tool')
                try:
                    _a = json.loads(_tc.get('function', {}).get('arguments') or '{}')
                except (json.JSONDecodeError, TypeError):
                    _a = {}
                _p = _a.get('path')
                lines.append(f'→ `{_fn}`' + (f' `{_p}`' if _p else ''))
            if lines:
                transcript.append('\n'.join(lines))

        def _emit_progress():
            """Write the running token totals to the execution row (mid-run live
            progress). Best-effort: a DB/SSE hiccup must never break the run."""
            if not exec_id:
                return
            try:
                import agent_db as _db
                _db.update_execution_tokens(exec_id, tok_in_total, tok_out_total,
                                            project_path=project_path)
            except Exception:
                pass

        def _force_final_text(mt=None):
            """One-shot forced synthesis when the loop ends without a usable final
            answer (either max_turns exhausted, or the model returned an empty
            final-text turn after real tool work). Sends a "wrap it up" user
            message (no tools bound so the model can't call more) and strips any
            residual tool-call markup from the reply. Returns '' if the model
            still produced nothing.
            """
            nonlocal tok_in_total, tok_out_total
            messages.append({'role': 'user', 'content': 'Based on all the information gathered above, provide your complete response now.'})
            data = _post({'model': wire_model, 'messages': messages, 'max_tokens': mt if mt is not None else max_tokens})
            usage = data.get('usage', {})
            tok_in_total  += usage.get('prompt_tokens', 0) or 0
            tok_out_total += usage.get('completion_tokens', 0) or 0
            _emit_progress()
            choices = data.get('choices') or []
            t = _flatten_content(choices[0].get('message', {}).get('content') if choices else '').strip()
            if t:
                t = _STRIP_TOOL_MARKUP_RE.sub('', t).strip()
            return t

        for turn in range(max_turns):
            # Cancel check: the /cancel endpoint flips the execution row to
            # 'failed' and kills any child PID, but this loop runs in-process
            # (no child PID to signal). Without this check a cancelled
            # execution keeps burning turns for minutes and the late-returning
            # run used to mark the user-reset task 'done' (regression: task
            # #10001124). Raises ExecutionCancelledError, which run_task
            # handles as a clean stop (and which is deliberately NOT in the
            # HTTP retry lists, so retries don't fight the cancel).
            if _execution_cancelled(exec_id, project_path):
                raise ExecutionCancelledError('execution cancelled during tool loop')

            body = {'model': wire_model, 'messages': messages, 'max_tokens': max_tokens}
            if tools:
                body['tools'] = tools

            data = _post(body)

            usage = data.get('usage', {})
            tok_in_total  += usage.get('prompt_tokens', 0) or 0
            tok_out_total += usage.get('completion_tokens', 0) or 0
            _emit_progress()

            choices = data.get('choices') or []
            if not choices:
                break

            choice = choices[0]
            finish_reason = choice.get('finish_reason')
            msg = choice.get('message', {})

            # Model wants to call tools
            if finish_reason == 'tool_calls' and msg.get('tool_calls'):
                # Only pass back fields the backend needs — strip reasoning/annotations/audio
                # that inflate the body and can confuse strict JSON parsers on later turns.
                asst = {'role': 'assistant', 'tool_calls': msg['tool_calls']}
                if msg.get('content'):
                    asst['content'] = msg['content']
                messages.append(asst)
                _record_step(msg.get('content'), msg['tool_calls'])
                for tc in msg['tool_calls']:
                    fn_name = tc['function']['name']
                    try:
                        args = json.loads(tc['function']['arguments'])
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    result_str = agent_tools.dispatch(fn_name, args, project_path)
                    _log.info('tool result for %s: %s…', fn_name, result_str[:120])
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tc['id'],
                        'content': result_str,
                    })
                    # Phase 7.2 — verify writes/patches by reading back
                    if fn_name in ('write_file', 'patch_file'):
                        ver_path = args.get('path')
                        if ver_path:
                            messages.append({
                                'role': 'user',
                                'content': (
                                    f'You just called {fn_name} on {ver_path}. '
                                    'Now call read_file with the same path to verify '
                                    'the file on disk matches your intent before continuing.'
                                ),
                            })
                continue  # next turn with tool results

            # Final text response
            text = _flatten_content(msg.get('content')).strip()
            if text:
                transcript.append(text)
            # Truncation retry — in-turn re-issue at raised budget (does NOT
            # increment the `for turn in range(40)` counter). Only retries
            # when the final turn was empty/truncated and the model cap
            # allows headroom; the 4 reasoning models capped at 16384 have
            # no headroom when called at their cap (retry is a no-op).
            if finish_reason == 'length' and (not text.strip() or not transcript):
                cap_max = int(get_cap(model_id).get('max_tokens') or max_tokens)
                effective_mt = effective_max_tokens(model_id, cap_max)
                if effective_mt > max_tokens:
                    _log.warning(
                        "%s truncated (length) at max_tokens=%s — retrying at %s",
                        provider_label, max_tokens, effective_mt,
                    )
                    body2 = {**body, 'max_tokens': effective_mt}
                    data2 = _post(body2)
                    u2 = data2.get('usage', {}) or {}
                    tok_in_total += u2.get('prompt_tokens', 0) or 0
                    tok_out_total += u2.get('completion_tokens', 0) or 0
                    _emit_progress()
                    choices2 = data2.get('choices') or []
                    msg2 = choices2[0].get('message', {}) if choices2 else {}
                    text2 = _flatten_content(msg2.get('content')).strip() if msg2 else ''
                    if text2:
                        text = text2
                        transcript.append(text)
                        # adopt retry finish_reason for the marker check below
                        try:
                            finish_reason = choices2[0].get('finish_reason') if choices2 else finish_reason
                        except Exception:
                            pass
            if finish_reason == 'length':
                _log.warning(
                    "%s response truncated at max_tokens=%d model=%s turn=%d",
                    provider_label, max_tokens, wire_model, turn,
                )
            # If the model returned an empty final text but the session already
            # accumulated real tool work, do NOT silently break and ship the raw
            # transcript (which ends mid-flow at a `→ tool` line). Force a proper
            # synthesis so the deliverable reads as complete. Mirrors the
            # max-turns recovery below. Regression: task #12 (a client project).
            if not text and len(transcript) >= 1:
                _log.warning(
                    "%s model %s returned empty final text on turn %d after %d transcript entries — forcing final synthesis",
                    provider_label, wire_model, turn, len(transcript),
                )
                _forced = _force_final_text(mt=effective_mt if 'effective_mt' in locals() else None)
                if _forced:
                    transcript.append(_forced)
                    text = _forced
            if finish_reason == 'length' and text and transcript and text == transcript[-1]:
                text = text + '\n\n[⚠️ Output was truncated at the model max_tokens limit and may be incomplete.]'
                transcript[-1] = text
                _log.warning("%s shipping truncated (but non-empty) deliverable — appending marker", provider_label)
            break

        else:
            _log.warning("%s tool loop hit max turns (%d) for model=%s — forcing final text call", provider_label, max_turns, wire_model)
            _forced = _force_final_text()
            if not _forced:
                raise RuntimeError(
                    f"{provider_label} model {wire_model} exhausted {max_turns} tool turns "
                    "and produced no final text response. "
                    "The model kept calling tools without delivering a final answer. "
                    "Consider using a different model or reducing the number of file reads."
                )
            transcript.append(_forced)
            text = _forced

        # The deliverable is the whole agentic session. When the model narrated
        # and/or took tool actions across turns, return that transcript so the
        # output view is readable; otherwise fall back to the single final text.
        if len(transcript) > 1:
            text = '\n\n'.join(t for t in transcript if t).strip()

        pricing = agent_config.get_pricing(model_id)
        cost = round(
            tok_in_total * pricing['input'] / 1_000_000 +
            tok_out_total * pricing['output'] / 1_000_000,
            6,
        ) if pricing else 0.0

        return text, tok_in_total, tok_out_total, cost

    except Exception as exc:
        _log.error("%s API error: %s", provider_label, exc)
        raise
    finally:
        _log.info("%s API call ended: model=%s", provider_label, wire_model)


def call_scaleway(model_id, prompt, max_tokens=8192, *, project_path=None, exec_id=None, **_):
    """
    Scaleway Generative API — OpenAI-compatible, EU-hosted, pay-per-token.
    Delegates to the shared OpenAI-compatible tool loop.
    """
    if not agent_config.has_real_secret(SCALEWAY_API_KEY):
        raise ValueError('SCALEWAY_API_KEY not set in .env')

    return _run_openai_tool_loop(
        provider_label='Scaleway',
        api_url=_SCW_API_URL,
        api_key=SCALEWAY_API_KEY,
        wire_model=_SCW_MODEL_MAP.get(model_id, model_id),
        model_id=model_id,
        prompt=prompt,
        max_tokens=max_tokens,
        max_turns=_SCW_MAX_TOOL_TURNS,
        project_path=project_path,
        exec_id=exec_id,
    )


def call_ollama(model_id, prompt, max_tokens=8192, *, project_path=None, exec_id=None, **_):
    """
    Ollama Cloud — OpenAI-compatible, US-hosted, flat subscription (metered by
    GPU-time, not per-token). Tool calling supported at /v1/chat/completions, so it
    reuses the same agentic tool loop as Scaleway. Cost is recorded as $0 because
    billing is a subscription (per-token pricing in agent_config is 0 for oll-*).
    """
    if not agent_config.has_real_secret(OLLAMA_API_KEY):
        raise ValueError('OLLAMA_API_KEY not set in .env')

    return _run_openai_tool_loop(
        provider_label='Ollama',
        api_url=_OLLAMA_API_URL,
        api_key=OLLAMA_API_KEY,
        wire_model=_OLL_MODEL_MAP.get(model_id, model_id),
        model_id=model_id,
        prompt=prompt,
        max_tokens=max_tokens,
        max_turns=_OLL_MAX_TOOL_TURNS,
        project_path=project_path,
        exec_id=exec_id,
    )


def call_google(model_id, prompt, max_tokens=4096, *, project_path=None, **_):
    # Phase 2 stub — Google SDK not installed yet
    raise NotImplementedError('Google SDK not installed. Add GOOGLE_API_KEY and run: pip install google-generativeai')


def _call_scw_deploy(model_id, prompt, max_tokens=4096, *, project_path=None, **_):
    import agent_scw_deploy
    return agent_scw_deploy.call_deployment(
        model_id, prompt, max_tokens=max_tokens, project_path=project_path, **_,
    )


PROVIDER_DISPATCH = {
    'anthropic':   call_anthropic,
    'openai':      call_openai,
    'google':      call_google,
    'mistral':     call_mistral,
    'scaleway':    call_scaleway,
    'ollama':      call_ollama,
    'scw_deploy':  _call_scw_deploy,
}


def _project_eu_only(project_path):
    """Resolve the EU-only policy for the project owning `project_path`.

    Per-project flag is authoritative (Phase 0.4). Fails *closed* on a lookup
    error; a path with no matching project row is not EU-only — the
    pre-existing behaviour for chats/scaffolding before a project exists."""
    if not project_path:
        return False
    try:
        import agent_db
        proj = agent_db.get_project_by_path(project_path)
    except Exception:
        _log.warning('EU boundary: could not resolve project for %s; failing closed',
                     project_path, exc_info=True)
        return True
    return bool(proj and proj.get('eu_only'))


def _enforce_eu_boundary(model_id, provider, prompt, project_path, caller):
    """The single gate before any prompt leaves this host.

    Every ungated `or DEFAULT_MODEL` fallback in the codebase funnels through
    route(), so enforcing here makes those call sites safe by construction
    rather than relying on nine separate patches staying in place.

    Raises EUBoundaryError when the model would send data outside the EU.
    Writes an audit line for every crossing of the boundary."""
    eu_only = _project_eu_only(project_path)
    ok, err = agent_config.eu_guard(eu_only, model_id)
    if not ok:
        agent_config.eu_audit(project_path, model=model_id, provider=provider,
                              allowed=False, caller=caller,
                              bytes_out=len(prompt or ''), reason=err)
        _log.error('EU boundary: refused %s (%s) for %s — %s',
                   model_id, provider, project_path or '<no project>', err)
        raise agent_config.EUBoundaryError(err, model_id=model_id)
    if eu_only:
        # Only EU-only projects (and the Vault) log allowed egress — elsewhere it
        # would be noise on every call.
        agent_config.eu_audit(project_path, model=model_id, provider=provider,
                              allowed=True, caller=caller,
                              bytes_out=len(prompt or ''))


def route(model_id, prompt, max_tokens=4096, *, project_path=None,
          exec_id=None, force_anthropic_mode=None, force_mistral_mode=None,
          caller='', policy_path=None, web_search=False):
    """Route to the correct provider. Returns (text, tok_in, tok_out, cost_usd).
    `project_path` (keyword-only) is forwarded to the provider call so that
    subprocess-backed providers (claude CLI) can set cwd for per-project
    .claude/settings.json auto-discovery. Non-subprocess providers ignore it.
    `exec_id` is forwarded to providers that support mid-run progress updates.
    `force_anthropic_mode` is forwarded too; only `call_anthropic` honors it,
    `force_mistral_mode` is forwarded to `call_mistral`, other providers accept
    and ignore via **_. `caller` is a free-text label recorded in the EU egress
    audit log. `policy_path` resolves the EU-only policy when the caller
    deliberately passes `project_path=None` to disable tools (the large-file
    batch path) — the data still belongs to that project.
    `web_search` is forwarded to `call_mistral` only; the Conversations API
    enables Mistral's web_search tool. Other providers absorb it via **_."""
    m = MODELS.get(model_id)

    # Fallback: infer provider from model ID prefix for unregistered models
    if not m:
        if model_id.startswith('claude-'):
            provider = 'anthropic'
        elif model_id.startswith(('gpt-', 'o1', 'o3')):
            provider = 'openai'
        elif model_id.startswith('gemini-'):
            provider = 'google'
        elif model_id.startswith('mistral-') or model_id.startswith('open-mistral'):
            provider = 'mistral'
        elif model_id.startswith('oll-'):
            provider = 'ollama'
        elif model_id.startswith('scw-dep-'):
            provider = 'scw_deploy'
        else:
            raise ValueError(f'Unknown model: {model_id}. Add it to agent_config.MODELS.')
        m = {'provider': provider, 'cost_input': 0, 'cost_output': 0}

    provider = m['provider']

    # Free-tier model whitelist + token metering — resolve owner from project
    # path once; used for both the whitelist check and post-dispatch token
    # recording.  This is the single choke point: every route() call (H2/H3,
    # briefs, scaffold, improve-prompt, batch chunks, chat, task runs) goes
    # through here, so metering here closes all gaps by construction.
    _quota_owner = None
    try:
        import agent_quotas
        _quota_owner = agent_quotas.resolve_owner_for_call(
            project_path or policy_path, exec_id=exec_id)
    except Exception:
        _quota_owner = None
    if _quota_owner is not None:
        # Fail closed: if the allow-list check itself errors we refuse the
        # call rather than silently skipping the free-tier model gate.
        try:
            _model_allowed = agent_quotas.is_allowed_model(model_id, _quota_owner)
        except Exception:
            _model_allowed = False
        if not _model_allowed:
            raise ValueError(
                f'Model {model_id!r} is not available on the free tier. '
                f'Allowed: mistral-*, scw-* (excl. GPU), oll-*.'
            )

    # EU data-residency gate — the last point before bytes leave this host.
    _enforce_eu_boundary(model_id, provider, prompt,
                         policy_path or project_path, caller)

    fn = PROVIDER_DISPATCH.get(provider)
    if not fn:
        raise NotImplementedError(f'Provider {provider} is not supported yet')
    result = fn(model_id, prompt, max_tokens, project_path=project_path,
               exec_id=exec_id,
               force_anthropic_mode=force_anthropic_mode, force_mistral_mode=force_mistral_mode,
               web_search=web_search)

    # Token metering — record after dispatch returns.  Catches every route()
    # call site (task runs, chat, H2/H3/H4, briefs, scaffold, improve-prompt,
    # batch chunks).  Non-free / off-mode skips silently via quotas_applicable.
    if _quota_owner and len(result) >= 3:
        try:
            agent_quotas.record_tokens(_quota_owner, result[1], result[2])
        except Exception:
            pass

    # Budget metering — same single choke point. Recording here counts every
    # paid call (failed, cancelled, retried, briefs, overseer, batch chunks),
    # not just the task/chat runs that used to roll spend at their call sites.
    try:
        _spend = float(result[3]) if result and len(result) >= 4 else 0.0
        if _spend > 0:
            import agent_db as _db
            _proj = _db.get_project_by_path(policy_path or project_path)
            if _proj:
                _db.update_budget_spend(_proj['id'], _spend)
    except Exception:
        pass

    return result
