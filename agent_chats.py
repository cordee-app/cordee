"""
Chat persistence — Phase 4.
Manages Artifacts/chats/ files. One file per chat, append-only.
A chat lives at one of three scopes:
  - project (project_id only)
  - phase   (project_id + phase_name)
  - task    (project_id + task_id, phase_name optional)
"""
import os
import re
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import agent_config

CHATS_DIR = os.path.join('Artifacts', 'chats')
CHAT_CONTEXT_CHARS = 12000


def format_display_ts():
    """Format current time as '15:09 CEST (13:09 UTC)' for chat transcripts.
    Falls back to bare UTC if the configured timezone is invalid."""
    try:
        local_zone = ZoneInfo(agent_config.DISPLAY_TZ)
    except Exception:
        local_zone = timezone.utc
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(local_zone)
    tzname = now_local.strftime('%Z') or 'UTC'
    return f'{now_local.strftime("%H:%M")} {tzname} ({now_utc.strftime("%H:%M")} UTC)'


def _chats_dir(project_path):
    d = os.path.join(project_path, CHATS_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def _slug(text, max_len=24):
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return s[:max_len] or 'chat'


def chat_scope_label(chat):
    """Return 'project' | 'phase' | 'task'."""
    if chat.get('task_id'):
        return 'task'
    if (chat.get('phase_name') or '').strip():
        return 'phase'
    return 'project'


def chat_file_path(project_path, chat):
    """Build the on-disk path for a chat file."""
    scope = chat_scope_label(chat)
    if scope == 'task':
        prefix = f'task-{chat["task_id"]}'
    elif scope == 'phase':
        m = re.search(r'\d+', chat['phase_name'])
        prefix = f'phase-{m.group()}' if m else f'phase-{_slug(chat["phase_name"])}'
    else:
        prefix = 'project'
    return os.path.join(_chats_dir(project_path), f'{prefix}-{_slug(chat["name"])}-{chat["id"]}.chat.md')


def create_chat_file(project_path, chat):
    path = chat_file_path(project_path, chat)
    if os.path.exists(path):
        return path
    scope = chat_scope_label(chat)
    ts = format_display_ts()
    header = (
        f'# Chat: {chat["name"]}\n\n'
        f'**Scope:** {scope}  \n'
        f'**Project:** {chat.get("project_name", "")}  \n'
    )
    if scope in ('phase', 'task') and chat.get('phase_name'):
        header += f'**Phase:** {chat["phase_name"]}  \n'
    if scope == 'task' and chat.get('task_title'):
        header += f'**Task:** {chat["task_title"]}  \n'
    header += f'**Started:** {ts}  \n\n---\n'
    with open(path, 'w', encoding='utf-8') as f:
        f.write(header)
    return path


def append_user_message(chat_path, content):
    if not chat_path:
        return
    ts = format_display_ts()
    entry = f'\n### [{ts}] 👤 User\n\n{content.strip()}\n'
    with open(chat_path, 'a', encoding='utf-8') as f:
        f.write(entry)


def append_assistant_message(chat_path, content, model, tokens_in, tokens_out, cost, exec_id):
    if not chat_path:
        return
    ts = format_display_ts()
    entry = (
        f'\n### [{ts}] 🤖 Assistant (exec #{exec_id})\n\n'
        f'**Model:** {model} · **Tokens:** {tokens_in}↑ {tokens_out}↓ · **Cost:** ${cost:.4f}\n\n'
        f'{content.strip()}\n\n---\n'
    )
    with open(chat_path, 'a', encoding='utf-8') as f:
        f.write(entry)


def append_system_note(chat_path, note):
    if not chat_path:
        return
    ts = format_display_ts()
    entry = f'\n_[{ts}] {note}_\n'
    with open(chat_path, 'a', encoding='utf-8') as f:
        f.write(entry)


def _summarize_permission_denials(denials):
    parts = []
    for denial in (denials or [])[:3]:
        tool = denial.get('tool_name') or 'unknown tool'
        tool_input = denial.get('tool_input') or {}
        questions = tool_input.get('questions') if isinstance(tool_input, dict) else None
        if questions:
            qs = [
                q.get('question')
                for q in questions[:3]
                if isinstance(q, dict) and q.get('question')
            ]
            if qs:
                parts.append(f'{tool}: ' + ' | '.join(qs))
                continue
        parts.append(tool)
    if denials and len(denials) > 3:
        parts.append(f'+{len(denials) - 3} more')
    return '; '.join(parts)


def _load_result_envelope(text):
    """Return a parsed CLI result envelope if `text` contains one."""
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


def normalize_cli_result_text(text):
    """Replace malformed CLI JSON envelopes with compact notes for UI/context."""
    if not text or '"type"' not in text:
        return text
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith('{') or '"type"' not in stripped:
            lines.append(line)
            continue
        data = _load_result_envelope(stripped)
        if not data:
            lines.append(line)
            continue
        result = (data.get('result') or '').strip()
        denials = data.get('permission_denials') or []
        if result or not denials:
            lines.append(line)
            continue
        detail = _summarize_permission_denials(denials)
        note = 'Provider returned no assistant text because a tool request was denied.'
        if detail:
            note += f' Denied request: {detail}.'
        lines.append(f'_{note}_')
    return '\n'.join(lines)


def search_chat_files(query, limit=20):
    """Search across all .chat.md files for a case-insensitive substring.
    Returns a list of dicts with chat_id, chat_name, project_name, snippet.
    """
    import os
    import agent_db as db
    import re
    results = []
    for chat in db.get_chats():
        file_path = chat.get("file_path")
        if not file_path or not os.path.exists(file_path):
            continue
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            continue
        lower_content = content.lower()
        lower_q = query.lower()
        idx = lower_content.find(lower_q)
        if idx == -1:
            continue
        start = max(0, idx - 80)
        end = min(len(content), idx + len(query) + 80)
        snippet = content[start:end]
        snippet = re.sub(re.escape(query), f"<mark>{query}</mark>", snippet, flags=re.IGNORECASE)
        results.append({
            "chat_id": chat["id"],
            "chat_name": chat.get("name") or f"Chat #{chat['id']}",
            "project_name": chat.get("project_name") or "",
            "snippet": snippet,
        })
        if len(results) >= limit:
            break
    return results


def _normalize_cli_result_envelopes(text):
    return normalize_cli_result_text(text)


def read_chat_file(chat_path, cap=CHAT_CONTEXT_CHARS):
    """Return chat history text, capped to `cap` chars (truncating from the start)."""
    if not chat_path or not os.path.exists(chat_path):
        return ''
    try:
        with open(chat_path, encoding='utf-8', errors='ignore') as f:
            text = f.read().strip()
    except Exception:
        return ''
    if not text:
        return ''
    text = normalize_cli_result_text(text)
    if len(text) > cap:
        text = '[Earlier chat history truncated]\n\n' + text[-cap:]
    return text


def parse_messages(chat_path):
    """Return raw chat content (markdown) for UI display."""
    if not chat_path or not os.path.exists(chat_path):
        return ''
    try:
        with open(chat_path, encoding='utf-8', errors='ignore') as f:
            return normalize_cli_result_text(f.read())
    except Exception:
        return ''
