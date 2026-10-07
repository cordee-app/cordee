import os
import threading
from datetime import datetime, timezone

# Load .env if present (no external dependency needed)
_env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                if k not in os.environ:
                    os.environ[k] = v.strip().strip('"\'')

PROJECTS_ROOT = os.environ.get(
    'AINGEL_PROJECTS_ROOT',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'projects')
)
CENTRAL_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'aingel.db')
DB_PATH = CENTRAL_DB_PATH  # backward-compat alias
API_PORT = 8001

# Per-project soft-delete trash lives OUTSIDE the project tree so that agent
# bash (`find`, globs, `ls -la`) cannot discover or copy deleted files back.
# Layout: <TRASH_ROOT>/<project_id>/files/<timestamp>/<rel>. Mode 700, owned
# by the service user. The default sits beside the code, outside PROJECTS_ROOT.
TRASH_ROOT = os.environ.get(
    'AINGEL_TRASH_ROOT',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trash')
)

# Max accepted request body (bytes) for WebDAV PUT and the main API, configurable
# so it is not duplicated across apps (C4).
MAX_CONTENT_LENGTH = int(os.environ.get('AINGEL_MAX_CONTENT_LENGTH', 128 * 1024 * 1024))

# Instance identity — set AINGEL_INSTANCE_NAME in .env ('Vault' on the
# Scaleway vault host). Default '' = a generic/local install; is_vault gates
# vault-only features (SCW sessions, /opt/RAG corpora) and must be False for
# fresh installs. Never hardcode: a fresh clone would otherwise report
# is_vault=true and unlock vault-only behaviour everywhere.
INSTANCE_NAME = os.environ.get('AINGEL_INSTANCE_NAME', '')
IS_VAULT = INSTANCE_NAME.strip().lower() == 'vault'

# Display timezone for chat transcripts and human-readable output.
# Server always stores UTC; this only affects display formatting.
DISPLAY_TZ = os.environ.get('AINGEL_TZ', 'Europe/Paris')

# The Vault is the Scaleway-hosted AIngel instance that holds confidential
# projects only (see Plans/Scaleway batches and session/Scaleway session).
# Every project on it is EU-only by construction, regardless of its eu_only flag.
IS_VAULT = INSTANCE_NAME.strip().lower() == 'vault'

# AI-agnostic project definition filename (replaces the Claude-specific CLAUDE.md)
DEFINITION_FILE = 'READMEFIRST.md'
_LEGACY_DEF_FILE = 'CLAUDE.md'

# Canonical definition files for a project (includes legacy alias for allowlists / raw serving)
DEF_FILES_ALLOWLIST = (DEFINITION_FILE, _LEGACY_DEF_FILE, 'GUIDE.md', 'Skills.md')


# Environment variables an agent CLI subprocess genuinely needs to start and
# find its own config. Everything else — AINGEL_* (session secret, DAV token),
# provider API keys, SCW_* credentials, DB paths — is dropped so an agent's
# Bash tool cannot read the server's secrets out of its own environment.
_CLI_ENV_ALLOWLIST = (
    'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'TERM',
    'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES', 'LC_NUMERIC', 'LC_TIME',
    'TMPDIR', 'TMP', 'TEMP',
    'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR',
    'CLAUDE_CONFIG_DIR', 'CODEX_HOME',
    'NODE_ENV', 'NODE_OPTIONS', 'NODE_PATH', 'NPM_CONFIG_PREFIX',
    'PYTHONPATH', 'PYTHONIOENCODING', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
    'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE',
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy',
    'TZ', 'HOSTNAME', 'PWD', 'OLDPWD',
)


def cli_subprocess_env(extra=None):
    """Minimal, scrubbed environment for agent CLI subprocesses.

    ``extra`` adds the one provider key a given CLI needs (e.g.
    ``MISTRAL_API_KEY`` for the Vibe CLI). Values that are None are skipped.
    """
    env = {k: os.environ[k] for k in _CLI_ENV_ALLOWLIST if k in os.environ}
    if 'HOME' not in env:
        env['HOME'] = os.path.expanduser('~')
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def resolve_def_filename(project_path):
    """Return the definition filename present in this project. Prefers READMEFIRST.md; falls back to CLAUDE.md."""
    if not project_path:
        return DEFINITION_FILE
    for name in (DEFINITION_FILE, _LEGACY_DEF_FILE):
        if os.path.exists(os.path.join(project_path, name)):
            return name
    return DEFINITION_FILE


def get_def_files_for_project(project_path):
    """Return (def_filename, 'GUIDE.md', 'Skills.md') using the actual file present in this project."""
    return (resolve_def_filename(project_path), 'GUIDE.md', 'Skills.md')


def _read_secret_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ''


def resolve_secret(value: str) -> str:
    """Resolve supported secret references without printing secret material."""
    if not value:
        return ''
    v = value.strip()
    lower = v.lower()
    if lower.startswith('<contents of ') and v.endswith('>'):
        return _read_secret_file(v[len('<contents of '):-1].strip())
    if os.path.isabs(v) and os.path.isfile(v):
        return _read_secret_file(v)
    return v

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
OPENAI_API_KEY    = os.getenv('OPENAI_API_KEY', '')
GOOGLE_API_KEY    = os.getenv('GOOGLE_API_KEY', '')
MISTRAL_VIBE_KEY  = os.getenv('MISTRAL_VIBE_KEY', '')
MISTRAL_ORG_KEY   = os.getenv('MISTRAL_ORG_KEY', '')
SCALEWAY_API_KEY  = resolve_secret(os.getenv('SCALEWAY_API_KEY', ''))
OLLAMA_API_KEY    = resolve_secret(os.getenv('OLLAMA_API_KEY', ''))


def has_real_secret(value: str) -> bool:
    """Return True when an env secret is present and not a setup placeholder."""
    if not value:
        return False
    v = value.strip()
    return not (v.startswith('<') and v.endswith('>')) and 'contents of' not in v.lower()


# Boolean environment variable parsing utility
def parse_bool_env(var: str, default: bool = False) -> bool:
    """Parse env vars like '1', 'true', 'yes', 'on' (case-insensitive) into bool."""
    val = os.getenv(var, '').lower()
    return val in ('1', 'true', 'yes', 'on') if val else default

# 'api' = direct API calls (billed to API credits)
# 'claude-code' = route through the claude CLI (billed to Pro subscription)
ANTHROPIC_MODE = os.getenv('ANTHROPIC_MODE', 'claude-code')
CLAUDE_CODE_SKIP_PERMISSIONS = parse_bool_env('CLAUDE_CODE_SKIP_PERMISSIONS')

# 'api' = direct Mistral API calls (text-only, billed to API credits)
# 'vibe' = route through the Vibe CLI (full tool support, billed to Le Chat Pro subscription)
MISTRAL_MODE = os.getenv('MISTRAL_MODE', 'vibe')
VIBE_SKIP_PERMISSIONS = parse_bool_env('VIBE_SKIP_PERMISSIONS')
VIBE_MODELS = [m.strip() for m in os.getenv('VIBE_MODELS', 'mistral-large-latest,mistral-large-4,mistral-medium-latest,mistral-small-latest,codestral-latest,mistral-glm-5-3').split(',') if m.strip()]
CODEX_SKIP_PERMISSIONS = parse_bool_env('CODEX_SKIP_PERMISSIONS')

# Max USD to spend per Vibe CLI call (0 = no limit)
VIBE_MAX_PRICE = float(os.getenv('VIBE_MAX_PRICE', '15.00'))
if VIBE_MAX_PRICE < 0:
    import logging
    _log = logging.getLogger(__name__)
    _log.warning(f"VIBE_MAX_PRICE cannot be negative. Falling back to 15.00.")
    VIBE_MAX_PRICE = 15.00

# Wall-clock cap (seconds) on one Vibe CLI run. Long research-and-write tasks
# (task 10001187: ~12 min of reading before drafting) blew through the old
# hard-coded 900s; a timeout is not retried, so this must cover the whole task.
try:
    VIBE_CLI_TIMEOUT_SECS = max(60, int(os.getenv('VIBE_CLI_TIMEOUT_SECS', '2700')))
except ValueError:
    VIBE_CLI_TIMEOUT_SECS = 2700

# Cost = USD per 1M tokens
MODELS = {
    'claude-sonnet-5-5': {
        'label': 'Claude Sonnet 5.5', 'provider': 'anthropic',
        'cost_input': 2.0, 'cost_output': 10.0, 'default': True,
        'context_window': 1000000,
    },
    'claude-sonnet-4-6': {
        # Legacy (still API-available). Kept for existing task rows; no longer default.
        'label': 'Claude Sonnet 4.6', 'provider': 'anthropic',
        'cost_input': 3.0, 'cost_output': 15.0,
    },
    'claude-opus-5-5': {
        'label': 'Claude Opus 5.5', 'provider': 'anthropic',
        'cost_input': 4.0, 'cost_output': 20.0,
        'context_window': 1000000,
    },
    'claude-opus-4-7': {
        # Legacy (still API-available). Price corrected 2026-10: $5/$25 (the
        # $15/$75 that was here was Opus 4.1's retired rate).
        'label': 'Claude Opus 4.7', 'provider': 'anthropic',
        'cost_input': 5.0, 'cost_output': 25.0,
    },
    'claude-fable-5-1': {
        'label': 'Claude Fable 5.1', 'provider': 'anthropic',
        'cost_input': 10.0, 'cost_output': 50.0,
        'context_window': 1000000, 'best_for': 'frontier reasoning, long-horizon agentic',
    },
    'claude-haiku-4-5-20251001': {
        # Price corrected 2026-10: $1/$5 (the $0.80/$4 that was here was Haiku 3.5's rate).
        'label': 'Claude Haiku 4.5', 'provider': 'anthropic',
        'cost_input': 1.0, 'cost_output': 5.0,
    },
    'mistral-large-latest': {
        # Covered by the Mistral Pro subscription since 2026-08 — billed as a flat
        # subscription, not per token, so priced at 0 like the oll-* models. Both
        # AIngel API paths already use MISTRAL_VIBE_KEY (the Pro key), so this is a
        # pricing correction, not a routing change. Leaving it at the old $2/$6
        # meant the Counselor, which ranks cost-weighted, would never recommend a
        # model the subscription already pays for — the same failure as the 4x
        # scw-qwen3.5-397b overprice, in the opposite direction.
        'label': 'Mistral Large', 'provider': 'mistral',
        'cost_input': 0.0, 'cost_output': 0.0,
    },
    # Aligned to PRICING. All Mistral `-latest` models route through the Vibe CLI
    # and are covered by the Le Chat Pro subscription (flat, not per-token), so
    # priced at 0 like the oll-* models. tests/test_model_metadata.py fails if
    # MODELS and PRICING diverge.
    'mistral-small-latest': {
        'label': 'Mistral Small', 'provider': 'mistral',
        'cost_input': 0.0, 'cost_output': 0.0,
    },
    'mistral-medium-latest': {
        'label': 'Mistral Medium', 'provider': 'mistral',
        'cost_input': 0.0, 'cost_output': 0.0,
    },
    'codestral-latest': {
        'label': 'Codestral', 'provider': 'mistral',
        'cost_input': 0.0, 'cost_output': 0.0,
    },
    'mistral-large-4': {
        # Mistral Large 4 (v26.10), released Oct 2026 — Mistral's new flagship,
        # open-weight multimodal, 524k context. Covered by the Le Chat Pro
        # subscription via the Vibe CLI (flat, not per-token), so priced at 0 like
        # the other mistral-*-latest models. EU-operated (Mistral, Paris) —
        # is_eu_model() returns True for the mistral- prefix. Wire ID equals the
        # SuperAgent ID on both the Vibe and direct-API paths.
        'label': 'Mistral Large 4', 'provider': 'mistral',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 524288,
        'best_for': 'reasoning, coding, multimodal, long context',
    },
    'mistral-glm-5-3': {
        # Z.ai GLM-5.3, hosted unmodified by Mistral (wire ID `zai-glm-5-3`).
        # Covered by the Mistral Pro subscription (flat, not per-token) like the
        # other mistral-*-latest models, so priced at 0. EU-operated (Mistral,
        # Paris) — is_eu_model() returns True for the mistral- prefix. The
        # `mistral-` prefix also makes it slot-2 (Mistral Pro) eligible and
        # EU-visible in the frontend with zero extra changes. Wire ID is mapped
        # in agent_router._MISTRAL_WIRE_ID_MAP (Vibe + API paths).
        'label': 'GLM-5.3 (Mistral Pro)', 'provider': 'mistral',
        'cost_input': 0.0, 'cost_output': 0.0,
        'context_window': 1000000,
        'best_for': 'code, agentic, long context',
    },
    'mistral-ocr-latest': {
        # Mistral OCR — Document AI / OCR-4.1. Billed per-page via Mistral API credits,
        # not per-token. Covered by Pro subscription $30 included credits (~7500 pages
        # at $4/1k OCR, ~6000 at $5/1k Document AI). Priced at 0 in AIngel like other
        # mistral-latest (subscription), so the Counselor does not penalize it on cost.
        # EU-operated (Mistral, Paris) — is_eu_model() returns True for mistral-*.
        'label': 'Mistral OCR', 'provider': 'mistral',
        'cost_input': 0.0, 'cost_output': 0.0,
        'context_window': 128000,
        'best_for': 'ocr, document parsing, Polish diacritics',
    },
    'codex-chatgpt': {
        'label': 'Codex ChatGPT Default', 'provider': 'openai',
        'cost_input': 0.0, 'cost_output': 0.0,
    },
    # Scaleway Generative API (EU, pay-per-token, text-only, OpenAI-compatible)
    # Prices: EUR converted to USD at EUR_TO_USD (see constant above). Actual
    # Scaleway model IDs stored in agent_router._SCW_MODEL_MAP; SuperAgent uses
    # the scw-* prefix as stable IDs.
    'scw-deepseek-v4-flash': {
        # deepseek-v4-flash-0731: agentic/logical-reasoning model (Jul 2026).
        # Scaleway price EUR 0.40 / 0.80 per 1M tokens; ×EUR_TO_USD. Added
        # 2026-10 after qwen3-coder-30b's EOL.
        'label': 'SCW DeepSeek-V4 Flash', 'provider': 'scaleway',
        'cost_input': 0.432, 'cost_output': 0.864, 'context_window': 256000,
        'best_for': 'code, agentic, reasoning',
    },
    'scw-qwen3.8-27b': {
        # qwen3.8-27b: vision-language dense model (builds on 3.6-27B, improved
        # coding + office productivity, text and visual). Scaleway price
        # EUR 0.60 / 3.30 per 1M tokens; ×EUR_TO_USD.
        'label': 'SCW Qwen3.8 27B', 'provider': 'scaleway',
        'cost_input': 0.648, 'cost_output': 3.564, 'context_window': 256000,
        'best_for': 'coding, vision, analysis',
    },
    'scw-gpt-oss-120b': {
        'label': 'SCW GPT-OSS 120B', 'provider': 'scaleway',
        'cost_input': 0.162, 'cost_output': 0.648, 'context_window': 128000,
        'best_for': 'research, analysis',
    },
    'scw-llama-3.3-70b': {
        'label': 'SCW Llama 3.3 70B', 'provider': 'scaleway',
        'cost_input': 0.972, 'cost_output': 0.972, 'context_window': 128000,
        'best_for': 'general tasks',
    },
    'scw-mistral-small-24b': {
        'label': 'SCW Mistral-Small 24B', 'provider': 'scaleway',
        'cost_input': 0.162, 'cost_output': 0.378, 'context_window': 32000,
        'best_for': 'fast, cheap tasks',
    },
    'scw-gemma-4-26b': {
        'label': 'SCW Gemma-4 26B', 'provider': 'scaleway',
        'cost_input': 0.270, 'cost_output': 0.540, 'context_window': 128000,
        'best_for': 'writing, summarisation',
    },
    'scw-mistral-medium-128b': {
        'label': 'SCW Mistral-Medium 128B', 'provider': 'scaleway',
        'cost_input': 1.620, 'cost_output': 8.100, 'context_window': 128000,
        'best_for': 'complex reasoning, vision',
    },
    'scw-qwen3-235b': {
        'label': 'SCW Qwen3 235B', 'provider': 'scaleway',
        'cost_input': 0.810, 'cost_output': 2.430, 'context_window': 131072,
        'best_for': 'coding, analysis',
    },
    'scw-qwen3.6-35b': {
        'label': 'SCW Qwen3.6 35B', 'provider': 'scaleway',
        'cost_input': 0.270, 'cost_output': 1.620, 'context_window': 256000,
        'best_for': 'code, analysis, long context',
    },
    'scw-qwen3.5-397b': {
        'label': 'SCW Qwen3.5 397B', 'provider': 'scaleway',
        # Scaleway lists EUR 0.60 / 3.60 per 1M tokens; ×EUR_TO_USD, as every
        # other scw-* entry here. Was 2.50/10.00, ~4x over, which made the
        # Counselor rank the EU catalogue as far more expensive than it is —
        # the one place where a wrong price actively steers EU-only projects
        # away from Scaleway.
        'cost_input': 0.648, 'cost_output': 3.888, 'context_window': 250000,
        'best_for': 'complex reasoning, large projects',
    },
    'scw-glm-5.2': {
        # Scaleway repriced GLM-5.2 to EUR 1.80 / 5.50 per 1M (2026-08);
        # ×EUR_TO_USD.
        'label': 'SCW GLM-5.2', 'provider': 'scaleway',
        'cost_input': 1.944, 'cost_output': 5.940, 'context_window': 128000,
        'best_for': 'general tasks, multilingual',
    },
    # Ollama Cloud (US, flat subscription metered by GPU-time — NOT per-token, NOT EU).
    # Accessed via the OpenAI-compatible endpoint at https://ollama.com/v1 with a
    # function-calling tool loop, exactly like Scaleway. Cost recorded as $0 (like
    # codex-chatgpt) because billing is a subscription, not per-token. Budget is
    # governed by the dedicated slot-5 work session window (5h, matching Ollama's reset).
    # Actual cloud tags stored in agent_router._OLL_MODEL_MAP; oll-* are stable IDs.
    'oll-glm-5.3': {
        'label': 'OLL GLM-5.3', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 1000000,
        'best_for': 'coding, agentic, long context (subscription)',
    },
    'oll-glm-5.3-flash': {
        'label': 'OLL GLM-5.3 Flash', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 1000000,
        'best_for': 'fast coding, vision, agentic (subscription)',
    },
    'oll-mistral-large-4': {
        'label': 'OLL Mistral-Large 4', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 1000000,
        'best_for': 'reasoning, coding, multimodal (subscription)',
    },
    'oll-deepseek-v4-pro': {
        'label': 'OLL DeepSeek-V4 Pro', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 1000000,
        'best_for': 'reasoning, coding, agentic (subscription)',
    },
    'oll-deepseek-v4.1-flash': {
        'label': 'OLL DeepSeek-V4.1 Flash', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 1000000,
        'best_for': 'fast coding, vision, reasoning (subscription)',
    },
    'oll-kimi-k2.6': {
        'label': 'OLL Kimi K2.6', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 256000,
        'best_for': 'coding, agentic, vision (subscription)',
    },
    'oll-minimax-m2.7': {
        'label': 'OLL MiniMax M2.7', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 200000,
        'best_for': 'coding, reasoning, analysis (subscription)',
    },
    'oll-glm-5.2': {
        'label': 'OLL GLM-5.2', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 128000,
        'best_for': 'general tasks, multilingual (subscription)',
    },
    'oll-gpt-oss-120b': {
        'label': 'OLL GPT-OSS 120B', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 128000,
        'best_for': 'research, analysis, long context (subscription)',
    },
    'oll-gpt-oss-20b': {
        'label': 'OLL GPT-OSS 20B', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'fast, cheap tasks (subscription)',
    },
    'oll-gemma4-31b': {
        'label': 'OLL Gemma-4 31B', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'writing, multilingual (subscription)',
    },
    'oll-kimi-k2.7-code': {
        'label': 'OLL Kimi K2.7 Code', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'coding, agentic (subscription)',
    },
    'oll-kimi-k3': {
        'label': 'OLL Kimi K3', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'coding, reasoning (subscription)',
    },
    'oll-minimax-m3': {
        'label': 'OLL MiniMax M3', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'analysis, multilingual (subscription)',
    },
    'oll-mistral-large-675b': {
        'label': 'OLL Mistral-Large 675B', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'reasoning, complex tasks (subscription)',
    },
    'oll-nemotron-3-nano': {
        'label': 'OLL Nemotron-3 Nano 30B', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'fast coding (subscription)',
    },
    'oll-nemotron-3-super': {
        'label': 'OLL Nemotron-3 Super', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'coding, analysis (subscription)',
    },
    'oll-nemotron-3-ultra': {
        'label': 'OLL Nemotron-3 Ultra', 'provider': 'ollama',
        'cost_input': 0.0, 'cost_output': 0.0, 'context_window': 131072,
        'best_for': 'reasoning, complex tasks (subscription)',
    },
}

DEFAULT_MODEL = 'claude-sonnet-5-5'

# ─────────────────────────────────────────────────────────────────────────────
# EU data-residency boundary
#
# DEFAULT_MODEL is Anthropic (US). Every ungated `or DEFAULT_MODEL` fallback in
# the codebase is therefore a potential egress out of an EU-only project. These
# helpers live here — not in agent_api — so agent_router, agent_executor and
# agent_overseer can all reach them, and so the boundary can be enforced at the
# single choke point in agent_router.route() rather than at every call site.
# ─────────────────────────────────────────────────────────────────────────────

# Fallback models for EU-only projects. Both are Scaleway (fr-par, zero data
# retention). EU_BRIEF_MODEL is used for the short, cheap summarisation calls
# (Autopilot briefs) where the general default would be overkill.
EU_DEFAULT_MODEL = 'scw-qwen3.6-35b'
EU_BRIEF_MODEL   = 'scw-mistral-small-24b'


class EUBoundaryError(RuntimeError):
    """Raised when a call would send project data to a non-EU provider.

    Carries `model_id` so callers can report which model was refused."""

    def __init__(self, message, model_id=''):
        super().__init__(message)
        self.model_id = model_id


def is_eu_model(model_id):
    """Return True if model_id is EU-compliant (Scaleway or Mistral, which are
    EU-operated). Claude (Anthropic/US), Codex (OpenAI/US) and Ollama Cloud (US)
    are excluded.

    The scw-dep-<id> prefix (Phase 3 dedicated GPU deployments) also passes
    because it begins with 'scw-', and dedicated deployments are EU-hosted on
    Scaleway inference — so no separate branch is needed."""
    m = model_id or ''
    return (m.startswith('scw-') or m.startswith('mistral-')
            or m.startswith('open-mistral') or m.startswith('codestral-')
            or m.startswith('devstral-'))


def eu_only_for(proj):
    """Resolve the effective EU-only policy for a project.

    Accepts a project dict, a truthy/falsy flag, or None. Per-project flag is
    authoritative on every instance, including the Vault (instance identity
    remains Vault for naming/audit, but data-residency is decided by the
    project's eu_only — see Phase 0.4 decision 2026-08-31)."""
    if isinstance(proj, dict):
        return bool(proj.get('eu_only'))
    return bool(proj)


def eu_guard(proj, model_id):
    """Enforce a project's EU-only data-residency policy.

    EU-only projects may only run Mistral and Scaleway models."""
    if not eu_only_for(proj):
        return True, None
    if is_eu_model(model_id):
        return True, None
    label = MODELS.get(model_id or '', {}).get('label', model_id)
    return False, (f"'{label}' is not EU-compliant. This project is EU-only — "
                   f"choose a Mistral or Scaleway (scw-*) model.")


def default_model_for(proj=None, *, cheap=False):
    """Resolve the fallback model, honouring the EU boundary.

    Every `or DEFAULT_MODEL` fallback should go through this so an EU-only
    project silently lands on a Scaleway model instead of Claude."""
    if eu_only_for(proj):
        return EU_BRIEF_MODEL if cheap else EU_DEFAULT_MODEL
    return DEFAULT_MODEL


# ── Egress audit log ─────────────────────────────────────────────────────────
# One line per outbound model call that crosses (or is refused at) the EU
# boundary, so compliance is demonstrable rather than asserted. Content is never
# logged — only a byte count. ALLOW lines are written for EU-only projects and
# on the Vault; DENY lines are always written, everywhere.

AUDIT_LOG_NAME = 'eu-audit.log'
_AUDIT_LOCK = threading.Lock()
_CENTRAL_AUDIT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'Artifacts')


def eu_audit(project_path, *, model, provider, allowed, caller='', bytes_out=0, reason=''):
    """Append one egress record to <project_path>/Artifacts/eu-audit.log.

    Falls back to the AIngel-local Artifacts/ dir when there is no project
    path (chats and scaffolding calls made before a project exists). Never
    raises — an audit failure must not take down a run."""
    try:
        base = os.path.join(project_path, 'Artifacts') if project_path else _CENTRAL_AUDIT_DIR
        line = '\t'.join((
            datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            INSTANCE_NAME,
            'ALLOW' if allowed else 'DENY',
            model or '-',
            provider or '-',
            str(int(bytes_out or 0)),
            caller or '-',
            (reason or '').replace('\t', ' ').replace('\n', ' '),
        ))
        with _AUDIT_LOCK:
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, AUDIT_LOG_NAME), 'a') as fh:
                fh.write(line + '\n')
    except Exception:
        import logging
        logging.getLogger(__name__).warning('eu_audit: could not write audit line', exc_info=True)


# Extended pricing table — USD per 1M tokens (superset of MODELS, covers all known providers)
#
# NOTE on cache pricing: Anthropic models include 'cache_write' and 'cache_read'
# fields (prompt caching discounts/premiums). The cost calculation functions
# (_cost, _pricing_cost, estimate_cost, estimate_*_prompt_tokens) currently use
# only 'input' and 'output' prices — cache tokens are billed at the full input
# rate. This means costs for Anthropic models with heavy prompt caching are
# OVER-ESTIMATED (conservative upper bound). Tracking cache tokens separately
# would require provider API changes to expose cache_creation_input_tokens and
# cache_read_input_tokens in usage responses.
PRICING = {
    # Anthropic
    'claude-fable-5-1':          {'input': 10.00, 'output': 50.00, 'cache_write': 12.50, 'cache_read': 0.25},
    'claude-opus-5-5':           {'input':  4.00, 'output': 20.00, 'cache_write':  5.00, 'cache_read': 0.20},
    'claude-opus-4-7':           {'input':  5.00, 'output': 25.00, 'cache_write':  6.25, 'cache_read': 0.50},
    'claude-sonnet-5-5':         {'input':  2.00, 'output': 10.00, 'cache_write':  2.50, 'cache_read': 0.20},
    'claude-sonnet-4-6':         {'input':  3.00, 'output': 15.00, 'cache_write':  3.75, 'cache_read': 0.30},
    'claude-sonnet-4-5':         {'input':  3.00, 'output': 15.00, 'cache_write':  3.75, 'cache_read': 0.30},
    'claude-haiku-4-5-20251001': {'input':  1.00, 'output':  5.00, 'cache_write':  1.25, 'cache_read': 0.10},
    # Mistral — all `-latest` models route through the Vibe CLI and are covered
    # by the Le Chat Pro subscription (flat, not per-token), so priced at 0 like
    # the oll-* models. Must stay in step with MODELS. The `-3` legacy aliases
    # keep their published PAYG rates for the fuzzy get_pricing() fallback.
    'mistral-large-latest':      {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-large-4':           {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-large-3':           {'input':  0.50, 'output':  1.50, 'cache_write': 0, 'cache_read': 0},
    'mistral-medium-latest':     {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-medium-3':          {'input':  0.40, 'output':  2.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-small-latest':      {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-small-3':           {'input':  0.10, 'output':  0.30, 'cache_write': 0, 'cache_read': 0},
    'codestral-latest':          {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-glm-5-3':           {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-ocr-latest':        {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-ocr-2503':          {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    'mistral-ocr-2505':          {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    # OpenAI Codex CLI via ChatGPT login. CLI usage is subscription-metered,
    # not API-metered, so SuperAgent records zero API spend for this route.
    'codex-chatgpt':             {'input':  0.00, 'output':  0.00, 'cache_write': 0, 'cache_read': 0},
    # Scaleway Generative API — EUR×EUR_TO_USD → USD/M tokens
    'scw-deepseek-v4-flash':     {'input': 0.432, 'output': 0.864, 'cache_write': 0, 'cache_read': 0},
    'scw-qwen3.8-27b':           {'input': 0.648, 'output': 3.564, 'cache_write': 0, 'cache_read': 0},
    'scw-gpt-oss-120b':          {'input': 0.162, 'output': 0.648, 'cache_write': 0, 'cache_read': 0},
    'scw-llama-3.3-70b':         {'input': 0.972, 'output': 0.972, 'cache_write': 0, 'cache_read': 0},
    'scw-mistral-small-24b':     {'input': 0.162, 'output': 0.378, 'cache_write': 0, 'cache_read': 0},
    'scw-gemma-4-26b':           {'input': 0.270, 'output': 0.540, 'cache_write': 0, 'cache_read': 0},
    'scw-mistral-medium-128b':   {'input': 1.620, 'output': 8.100, 'cache_write': 0, 'cache_read': 0},
    'scw-qwen3-235b':            {'input': 0.810, 'output': 2.430, 'cache_write': 0, 'cache_read': 0},
    'scw-qwen3.6-35b':          {'input': 0.270, 'output': 1.620, 'cache_write': 0, 'cache_read': 0},
    'scw-qwen3.5-397b':         {'input': 0.648, 'output': 3.888, 'cache_write': 0, 'cache_read': 0},
    'scw-glm-5.2':              {'input': 1.944, 'output': 5.940, 'cache_write': 0, 'cache_read': 0},
    # Ollama Cloud — subscription-metered (GPU-time), so per-token cost is 0 (like codex-chatgpt)
    'oll-glm-5.3':              {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-glm-5.3-flash':        {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-mistral-large-4':      {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-deepseek-v4-pro':      {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-deepseek-v4.1-flash':  {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-kimi-k2.6':            {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-minimax-m2.7':         {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-glm-5.2':              {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-gpt-oss-120b':         {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-gpt-oss-20b':          {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-gemma4-31b':           {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-kimi-k2.7-code':       {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-kimi-k3':              {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-minimax-m3':           {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-mistral-large-675b':   {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-nemotron-3-nano':      {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-nemotron-3-super':     {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
    'oll-nemotron-3-ultra':     {'input': 0.0, 'output': 0.0, 'cache_write': 0, 'cache_read': 0},
}


def get_pricing(model_id):
    """Return pricing dict for model_id, or None if unknown. Fuzzy: exact → prefix → keyword."""
    if not model_id:
        return None
    m = model_id.lower()
    if m in PRICING:
        return PRICING[m]
    for key in PRICING:
        if m.startswith(key):
            return PRICING[key]
    if 'fable'     in m: return PRICING['claude-fable-5-1']
    if 'opus'      in m: return PRICING['claude-opus-4-7']
    if 'sonnet'    in m: return PRICING['claude-sonnet-4-6']
    if 'haiku'     in m: return PRICING['claude-haiku-4-5-20251001']
    if 'codestral' in m: return PRICING['codestral-latest']
    # Deliberately NOT the `-latest` Mistral models: those are subscription-covered
    # and priced at 0, which would silently price an unknown paid model at nothing.
    # Under-reporting defeats budget enforcement, so fall back to the PAYG `-3`.
    if 'large'     in m: return PRICING['mistral-large-3']
    if 'medium'    in m: return PRICING['mistral-medium-3']
    if 'small'     in m: return PRICING['mistral-small-3']
    if 'codex'     in m: return PRICING.get('codex-chatgpt')
    return None


PROVIDER_COLORS = {
    'anthropic': '#e8865a',
    'mistral':   '#ff7000',
    'vibe':      '#ff7000',  # Same as mistral for now
    'openai':    '#43853D',
    'scaleway':  '#4F5BD5',
    'ollama':    '#0b8f6a',
}

# Work session configuration
SESSION_DURATION_HOURS = 5
SESSION_TOKEN_BUDGET   = 150000

# EUR → USD conversion factor. Scaleway prices are quoted in EUR; this rate
# converts them to USD for unified cost tracking. Update when the rate shifts
# materially (currently ~1.08 as of 2026-10).
EUR_TO_USD = 1.08

# Estimated input/output token split for cost estimation when actual token
# counts are unknown (task creation, Counselor scoring). 75/25 is a middle
# ground between the previous 70/30 (estimate_cost) and 80/20 (_score_model).
# Used by estimate_cost() and _score_model() so both agree.
ESTIMATE_INPUT_RATIO  = 0.75
ESTIMATE_OUTPUT_RATIO = 0.25


def dynamic_models():
    """Overlay active scw_deployments onto MODELS + PRICING.

    Returns a merged dict {model_id: model_meta} suitable for the models
    picker. Hourly-billed deployments price at 0 per token (cost accrues by
    runtime in scw_deployments.accrued_cost_usd, not via the per-token
    counter). Active = status NOT IN ('deleted','deleting')."""
    merged = dict(MODELS)
    try:
        import agent_db
        conn = agent_db.get_db()
        rows = conn.execute(
            "SELECT scw_deployment_id, model_name, node_type, max_context_size FROM scw_deployments "
            "WHERE status NOT IN ('deleted','deleting')"
        ).fetchall()
        conn.close()
        for r in rows:
            r = dict(r)
            mid = f'scw-dep-{r["scw_deployment_id"]}'
            merged[mid] = {
                'label': f'Dedicated {r["model_name"]} ({r["node_type"]})',
                'provider': 'scw_deploy',
                'cost_input': 0,
                'cost_output': 0,
                'context_window': r.get('max_context_size') or 128000,
            }
    except Exception:
        pass
    return merged
