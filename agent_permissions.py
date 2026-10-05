"""
SuperAgent permissions — manages Claude Code .claude/settings.json files.

Two scopes:
  • Global ~/.claude/settings.json  — A+B+C defaults, marked block preserved
    around any user-managed entries elsewhere in the file.
  • Per-project <project>/.claude/settings.json — DB-driven; toggles + custom
    rules surface through the webapp.

Source of truth for per-project state is the project_permissions DB table;
the file is regenerated from DB rows on every mutation.
"""
import json
import os
from pathlib import Path
import logging

try:
    import toml
    TOML_AVAILABLE = True
except ImportError:
    TOML_AVAILABLE = False
    # _log might not be available during early import
    try:
        _log.warning("toml package not available. Install with: pip install toml")
    except NameError:
        print("WARNING: toml package not available. Install with: pip install toml")

import agent_db as db

_log = logging.getLogger(__name__)

# Sentinel keys used inside the global file's permissions.allow list so we can
# find and replace our managed block without touching the user's own entries.
GLOBAL_BEGIN = '__SUPERAGENT_BEGIN__'
GLOBAL_END   = '__SUPERAGENT_END__'

# Eight permission groups. Each value is the list of Claude Code permission
# rules that belong to the group. Group D starts empty and is populated by the
# user per-project (build/test commands specific to that project's stack).
GROUPS: dict[str, dict] = {
    'A_read': {
        'label': 'Read-only file inspection',
        'default_enabled': True,
        'rules': ['Read(*)', 'Glob(*)', 'Grep(*)'],
    },
    'B_write': {
        'label': 'Project file mutation',
        'default_enabled': True,
        'rules': ['Edit(*)', 'Write(*)', 'NotebookEdit(*)'],
    },
    'C_bash_safe': {
        'label': 'Safe Bash (read-only)',
        'default_enabled': True,
        'rules': [
            'Bash(ls:*)', 'Bash(pwd)', 'Bash(cat:*)',
            'Bash(find:*)', 'Bash(grep:*)', 'Bash(which:*)',
            'Bash(echo:*)', 'Bash(stat:*)', 'Bash(head:*)', 'Bash(tail:*)',
            'Bash(git status)', 'Bash(git diff:*)',
            'Bash(git log:*)', 'Bash(git show:*)', 'Bash(git branch)',
        ],
    },
    'D_bash_proj': {
        'label': 'Project Bash commands (build / test / run)',
        # Keep this enabled by default for code-oriented projects so Claude Code
        # can actually run the usual inspect/build/test commands instead of
        # failing on the first Bash request. Destructive/system commands stay
        # blocked by group E.
        'default_enabled': True,
        'rules': [
            'Bash(cp:*)',
            'Bash(mv:*)',
            'Bash(mkdir:*)',
            'Bash(touch:*)',
            'Bash(python3:*)',
            'Bash(python:*)',
            'Bash(pip3:*)',
            'Bash(pip:*)',
            'Bash(node:*)',
            'Bash(npm:*)',
            'Bash(npx:*)',
            'Bash(pytest:*)',
            'Bash(uv:*)',
            'Bash(sqlite3:*)',
            'Bash(awk:*)',
            'Bash(sed:*)',
            'Bash(sort:*)',
            'Bash(wc:*)',
            'Bash(diff:*)',
            'Bash(xargs:*)',
            'Bash(timeout:*)',
            'Bash(tee:*)',
            'Bash(git add:*)',
            'Bash(git commit:*)',
            'Bash(git status)',
            'Bash(git diff:*)',
            'Bash(git log:*)',
            'Bash(git show:*)',
            'Bash(git branch)',
        ],
    },
    'E_destructive': {
        'label': 'Destructive / system Bash (DENY)',
        'default_enabled': False,
        'rules': [
            'Bash(rm:*)', 'Bash(sudo:*)', 'Bash(chmod:*)',
            'Bash(kill:*)', 'Bash(systemctl:*)',
            'Bash(git push:*)', 'Bash(git reset --hard:*)',
            'Bash(git branch -D:*)',
        ],
    },
    'F_network': {
        'label': 'Network egress',
        'default_enabled': False,
        'rules': ['WebFetch(*)', 'WebSearch(*)', 'Bash(curl:*)', 'Bash(wget:*)'],
    },
    'G_agent': {
        'label': 'Recursive Claude / agent spawning',
        'default_enabled': False,
        'rules': ['Agent(*)', 'Task(*)'],
    },
    'H_mcp': {
        'label': 'MCP tools',
        'default_enabled': False,
        'rules': ['mcp__*'],
    },
}

# Which groups represent default-allow vs default-deny semantics — used when
# rendering the file's allow vs deny lists.
DENY_GROUPS = {'E_destructive'}


# Map permission groups to Vibe tool names. Vibe's gating is per-tool, not per-pattern.
#
# IMPORTANT: these must match Vibe's *actual* registered tool names, which are
# the snake_case of the builtin tool class (vibe/core/tools/builtins/*.py):
#   Edit -> `edit`, WebSearch -> `web_search`, WebFetch -> `web_fetch`, etc.
# Vibe filters the toolset by fnmatch against `enabled_tools`, so a name with the
# wrong spelling/underscores silently drops the tool from the model's toolset.
# Regression: task #10000698 (a client project) — the previous values `webfetch`/
# `websearch`/`search_replace` did not match Vibe's `web_fetch`/`web_search`/
# `edit`, so the model ran with only [bash, write_file, edit, read_file, grep]
# and reported "no web browsing capabilities" despite F_network being enabled.
_GROUP_TO_VIBE_TOOLS = {
    'A_read':       ['read_file', 'grep'],
    'B_write':      ['write_file', 'edit'],
    'C_bash_safe':  ['bash'],                  # all-or-nothing
    'D_bash_proj':  ['bash'],                  # same tool as C
    'E_destructive': [],                       # handled via system-prompt guardrail
    'F_network':    ['web_fetch', 'web_search'],
    'G_agent':      ['task'],
    'H_mcp':        ['mcp_*'],
}


# ── Per-project ──────────────────────────────────────────────────────────────

def _project_settings_path(project_path: str) -> Path:
    return Path(project_path) / '.claude' / 'settings.json'


def _vibe_config_path(project_path: str) -> Path:
    return Path(project_path) / '.vibe' / 'config.toml'


def _seed_rules_for_db() -> dict[str, list[str]]:
    """Static GROUPS dict flattened to {group_key: [rule, ...]} for DB seeding.
    Groups with default_enabled=True will be seeded as enabled=1; deny-by-default
    groups still get seeded so the user can flip them on, but are written to
    `deny` not `allow` when enabled."""
    return {gk: list(g['rules']) for gk, g in GROUPS.items() if g['rules']}


def _seed_defaults_for_project(project_id: int) -> None:
    """Idempotently insert all default rules with auto_added=1. Then for every
    group whose default_enabled=False, mark its rows enabled=0 (only the FIRST
    time — never clobber user toggles on subsequent calls)."""
    existing = db.get_project_permissions(project_id)
    db.seed_project_permissions(project_id, _seed_rules_for_db())
    # First-seed: groups that start disabled get their rows toggled off.
    if not existing:
        for gk, group in GROUPS.items():
            if not group['default_enabled']:
                for rule in group['rules']:
                    db.toggle_permission(project_id, rule, False)


def _build_settings_dict(project_id: int) -> dict:
    """Compose the JSON body of <project>/.claude/settings.json from DB state."""
    rows = db.get_project_permissions(project_id)
    allow: list[str] = []
    deny:  list[str] = []
    for group_key, items in rows.items():
        target = deny if group_key in DENY_GROUPS else allow
        for item in items:
            if item['enabled']:
                target.append(item['rule'])
    return {'permissions': {'allow': allow, 'deny': deny}}


def _build_vibe_config(project_id: int) -> dict:
    """Compose the TOML body of <project>/.vibe/config.toml from DB state."""
    rows = db.get_project_permissions(project_id)
    config = {
        'tools': {},
        'enabled_tools': [],
        'disabled_tools': [],
    }
    
    # Group D (project-specific bash) and C (safe bash) both map to the 'bash' tool
    bash_enabled = False
    for group_key in ['C_bash_safe', 'D_bash_proj']:
        for item in rows.get(group_key, []):
            if item['enabled']:
                bash_enabled = True
                break
    
    # Apply group-to-tool mapping. Bash + MCP are handled separately below
    # so we don't write the same tool twice.
    _SPECIAL_GROUPS = {'C_bash_safe', 'D_bash_proj', 'H_mcp'}
    for group_key, tools in _GROUP_TO_VIBE_TOOLS.items():
        if not tools or group_key in _SPECIAL_GROUPS:
            continue
        group_enabled = any(item['enabled'] for item in rows.get(group_key, []))
        for tool in tools:
            if group_enabled:
                config['enabled_tools'].append(tool)
                config['tools'][tool] = {'permission': 'always'}
            else:
                config['disabled_tools'].append(tool)

    # Bash tool (all-or-nothing across groups C+D)
    if bash_enabled:
        config['tools']['bash'] = {'permission': 'ask'}  # Option 1: ask for every bash command
        config['enabled_tools'].append('bash')
    else:
        config['disabled_tools'].append('bash')

    # MCP tools (group H)
    if any(item['enabled'] for item in rows.get('H_mcp', [])):
        config['enabled_tools'].append('mcp_*')
    else:
        config['disabled_tools'].append('mcp_*')

    return config


def write_vibe_config(project_id: int, project_path: str) -> dict:
    """Write <project>/.vibe/config.toml from DB state. Returns path and config."""
    if not TOML_AVAILABLE:
        _log_msg = "Cannot write Vibe config: toml package not available"
        try:
            _log.warning(_log_msg)
        except NameError:
            print(f"WARNING: {_log_msg}")
        vibe_path = _vibe_config_path(project_path)
        return {'path': str(vibe_path), 'config': {}}
    
    config = _build_vibe_config(project_id)
    vibe_path = _vibe_config_path(project_path)
    vibe_path.parent.mkdir(parents=True, exist_ok=True)

    # Nest tool settings under a single `tools` table so the TOML writer
    # emits proper `[tools.<name>]` sections instead of literal "tools.<name>" keys.
    toml_config: dict = {}
    if config['tools']:
        toml_config['tools'] = config['tools']
    if config['enabled_tools']:
        toml_config['enabled_tools'] = sorted(set(config['enabled_tools']))
    if config['disabled_tools']:
        toml_config['disabled_tools'] = sorted(set(config['disabled_tools']))

    vibe_path.write_text(toml.dumps(toml_config), encoding='utf-8')
    return {'path': str(vibe_path), 'config': config}


def refresh_permissions(project_id: int, project_path: str) -> dict:
    """Seed defaults if needed, then rewrite both:
    - <project>/.claude/settings.json (Claude)
    - <project>/.vibe/config.toml    (Vibe)
    Returns dict with both paths and their contents."""
    if not project_path:
        raise ValueError('project_path is required')
    _seed_defaults_for_project(project_id)
    
    # Claude settings
    claude_settings = _build_settings_dict(project_id)
    claude_path = _project_settings_path(project_path)
    claude_path.parent.mkdir(parents=True, exist_ok=True)
    claude_path.write_text(json.dumps(claude_settings, indent=2) + '\n', encoding='utf-8')
    
    # Vibe config
    vibe_config = _build_vibe_config(project_id)
    vibe_result = write_vibe_config(project_id, project_path)
    
    return {
        'claude': {'path': str(claude_path), 'settings': claude_settings},
        'vibe':    {'path': vibe_result['path'], 'config': vibe_config},
    }


def get_permissions_view(project_id: int) -> dict:
    """Return DB state shaped for the dashboard: {group_key: {label, rules: [...]}}."""
    rows = db.get_project_permissions(project_id)
    view = {}
    for gk, group in GROUPS.items():
        view[gk] = {
            'label':    group['label'],
            'is_deny':  gk in DENY_GROUPS,
            'rules':    rows.get(gk, []),  # [{rule, enabled, auto_added, added_at}]
        }
    return view


def get_deny_patterns_for_project(project_path: str) -> list[str]:
    """Return the list of deny patterns for the project (group E). Used by
    the system-prompt guardrail in _call_vibe_cli."""
    # Find project_id from project_path
    project = db.get_project_by_path(project_path)
    if not project:
        return []
    project_id = project['id']
    
    # Get enabled rules from group E
    rows = db.get_project_permissions(project_id)
    deny_patterns = []
    for item in rows.get('E_destructive', []):
        if item['enabled']:
            # Extract the command pattern from rules like 'Bash(rm:*)'
            rule = item['rule']
            if rule.startswith('Bash(') and rule.endswith(')'):
                cmd_pattern = rule[5:-1]
                deny_patterns.append(cmd_pattern)
    
    return deny_patterns


def test_command(project_id: int, command: str) -> dict:
    """Evaluate a bash command against the project's permission rules.
    
    Returns a dict with:
      - allowed: bool
      - matched_rule: str | None  — the rule that blocked/allowed the command
      - matched_group: str | None — the group key of the matched rule
      - reason: str               — human-readable explanation
    """
    rows = db.get_project_permissions(project_id)
    command_lower = command.strip().lower()
    
    # 1. Check deny groups first (E_destructive)
    for item in rows.get('E_destructive', []):
        if not item['enabled']:
            continue
        rule = item['rule']
        if _rule_matches_command(rule, command_lower):
            return {
                'allowed': False,
                'matched_rule': rule,
                'matched_group': 'E_destructive',
                'reason': f'Blocked by deny rule: {rule}',
            }
    
    # 2. Check allow groups (A_read, B_write, C_bash_safe, D_bash_proj)
    allow_groups = ['A_read', 'B_write', 'C_bash_safe', 'D_bash_proj']
    for gk in allow_groups:
        for item in rows.get(gk, []):
            if not item['enabled']:
                continue
            rule = item['rule']
            if _rule_matches_command(rule, command_lower):
                return {
                    'allowed': True,
                    'matched_rule': rule,
                    'matched_group': gk,
                    'reason': f'Allowed by rule: {rule}',
                }
    
    # 3. Check remaining allow groups (F_network, G_agent, H_mcp)
    for gk in ['F_network', 'G_agent', 'H_mcp']:
        for item in rows.get(gk, []):
            if not item['enabled']:
                continue
            rule = item['rule']
            if _rule_matches_command(rule, command_lower):
                return {
                    'allowed': True,
                    'matched_rule': rule,
                    'matched_group': gk,
                    'reason': f'Allowed by rule: {rule}',
                }
    
    # 4. No matching rule found — blocked by default
    return {
        'allowed': False,
        'matched_rule': None,
        'matched_group': None,
        'reason': 'No matching permission rule — blocked by default',
    }


def _rule_matches_command(rule: str, command_lower: str) -> bool:
    """Check if a permission rule matches a command.
    
    Rules are in the format:
      - Bash(cmd:pattern) — matches bash commands
      - Read(*), Edit(*), Write(*) — generic file operations
      - Glob(*), Grep(*) — search operations
      - WebFetch(*), WebSearch(*) — network operations
      - Agent(*), Task(*) — agent spawning
      - mcp__* — MCP tools
    
    For Bash rules, we extract the command pattern and check if the command
    starts with that pattern (with wildcard support).
    """
    if not rule or not command_lower:
        return False
    
    # Bash rules: Bash(cmd:pattern)
    if rule.startswith('Bash(') and rule.endswith(')'):
        inner = rule[5:-1]  # e.g. "rm:*" or "ls:*"
        if ':' not in inner:
            return False
        cmd_part, pattern = inner.split(':', 1)
        cmd_part = cmd_part.strip().lower()
        pattern = pattern.strip()
        
        # Extract the base command from the input
        input_cmd = command_lower.split()[0] if command_lower.split() else ''
        
        # Check if the command matches
        if pattern == '*':
            return command_lower.startswith(cmd_part + ' ')
        elif pattern.endswith('*'):
            prefix = pattern[:-1].lower()
            return command_lower.startswith(cmd_part + ' ' + prefix)
        else:
            return command_lower == (cmd_part + ' ' + pattern.lower()).strip()
    
    # Generic rules with wildcard: Read(*), Edit(*), Write(*), Glob(*), Grep(*)
    if rule.endswith('(*)'):
        base = rule[:-3]  # e.g. "Read"
        # These match any file operation, not bash commands
        return False
    
    # WebFetch(*), WebSearch(*)
    if rule.startswith('Web') and rule.endswith('(*)'):
        return False
    
    # Agent(*), Task(*)
    if rule in ('Agent(*)', 'Task(*)'):
        return False
    
    # mcp__* rules
    if rule.startswith('mcp__'):
        return False
    
    return False


# ── Global ~/.claude/settings.json ───────────────────────────────────────────

def _global_settings_path() -> Path:
    return Path(os.path.expanduser('~/.claude/settings.json'))


def _global_managed_rules() -> list[str]:
    """A+B+C rules that get written into the global file as universal defaults."""
    rules: list[str] = []
    for gk in ('A_read', 'B_write', 'C_bash_safe'):
        rules.extend(GROUPS[gk]['rules'])
    return rules


def _strip_managed_block(existing_allow: list) -> list:
    """Remove anything between the BEGIN/END sentinels (inclusive). Preserves
    every other entry the user has in the file."""
    out, skipping = [], False
    for entry in existing_allow:
        if entry == GLOBAL_BEGIN:
            skipping = True
            continue
        if entry == GLOBAL_END:
            skipping = False
            continue
        if not skipping:
            out.append(entry)
    return out


def provision_global_defaults() -> dict:
    """Merge A+B+C into ~/.claude/settings.json under sentinel markers. Idempotent:
    re-running replaces only our block, leaves the rest of the file alone."""
    path = _global_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding='utf-8') or '{}')
        except json.JSONDecodeError:
            existing = {}
    else:
        existing = {}

    perms = existing.setdefault('permissions', {})
    user_allow = perms.get('allow') or []
    cleaned = _strip_managed_block(user_allow)
    new_allow = cleaned + [GLOBAL_BEGIN] + _global_managed_rules() + [GLOBAL_END]
    perms['allow'] = new_allow
    perms.setdefault('deny', perms.get('deny') or [])

    path.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
    return {'path': str(path), 'managed_rules': _global_managed_rules()}
