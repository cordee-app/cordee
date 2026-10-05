# Cordée — Setup

## Quick start

**Prerequisites:** Python 3.11+, Node.js 18+, Git, `sqlite3` (for backups).

```bash
git clone https://github.com/cordee-app/cordee.git && cd cordee
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
(cd frontend && npm install && npm run build)
cp .env.example .env    # add the provider keys you use (see below)
python3 agent_api.py    # → http://localhost:8001
```

The central database (`aingel.db`) is created on first launch. Projects live
in `projects/` beside the code unless you set `AINGEL_PROJECTS_ROOT`.

To have an AI assistant install and verify everything for you, paste
[docs/INSTALL_PROMPT.md](docs/INSTALL_PROMPT.md) into Claude Code, Vibe or Codex.

## On first launch

1. Create a project from the dashboard (or open an existing project folder).
2. Add tasks on the board and pick a model per task.
3. Confirm the tasks you want to run, then **Run** a column or a single task.
4. Open the Memory panel → **Perms** tab on a project to review the permission
   files (seeded automatically on first view).

## Provider CLIs

The default routing modes use the providers' CLIs for tool support:

- **Anthropic** (`ANTHROPIC_MODE=claude-code`): `npm i -g @anthropic-ai/claude-code`,
  then run `claude` once to log in. With `ANTHROPIC_MODE=api` the SDK and
  `ANTHROPIC_API_KEY` are used instead.
- **Mistral** (`MISTRAL_MODE=vibe`): install the Vibe CLI (see Mistral's
  documentation), then run `vibe` to log in. With `MISTRAL_MODE=api` the SDK is
  used (text only, no tools).
- **Scaleway** (`scw-*` models, required for `eu_only` projects): no CLI, set
  `SCALEWAY_API_KEY` (a secret key from the Scaleway IAM console).
- **Ollama** (`oll-*` models): a local Ollama, or Ollama Cloud with `OLLAMA_API_KEY`.

The service user must be able to find `claude` / `vibe` on its `PATH`.

## Environment variables

All variables go in `.env` (template: [.env.example](.env.example)).

| Variable | Default | Description |
|----------|---------|-------------|
| `AINGEL_PROJECTS_ROOT` | `projects/` beside the code | Folder holding one sub-folder per project |
| `AINGEL_TRASH_ROOT` | `trash/` beside the code | Soft-delete trash, outside the projects folder |
| `ANTHROPIC_API_KEY` | | Anthropic models in `api` mode |
| `ANTHROPIC_MODE` | `claude-code` | `claude-code` (CLI) or `api` (SDK) |
| `MISTRAL_VIBE_KEY` | | Pro/Vibe-scope key. Powers direct-API mode and the Vibe `mistral-vibe` provider. Optional: the Vibe CLI can log in through the browser instead; runs work without the key, but the admin readiness check (`/api/config/readiness`) then reports `vibe: false` (cosmetic only). |
| `MISTRAL_ORG_KEY` | | PAYG/Org-scope key. Powers the Vibe `mistral-org` provider (Mistral Large). |
| `MISTRAL_MODE` | `vibe` | `vibe` (CLI) or `api` (SDK) |
| `VIBE_MODELS` | `mistral-large-latest,mistral-glm-5-3` | Mistral models allowed to use the Vibe CLI |
| `VIBE_MAX_PRICE` | `15.00` | Maximum cost in USD per Vibe task |
| `SCALEWAY_API_KEY` | | Scaleway Generative APIs (`scw-*` models) |
| `OPENAI_API_KEY` | | OpenAI models |
| `OLLAMA_API_KEY` | | Ollama Cloud (`oll-*` models) |
| `GOOGLE_API_KEY` | | Google models (hidden in the UI) |
| `SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, `SCW_ORGANIZATION_ID`, `SCW_PROJECT_ID` | | Scaleway encrypted sessions for `eu_only` projects |
| `CLAUDE_CODE_SKIP_PERMISSIONS` | `false` | Bypass Claude permission checks (local debugging only) |
| `VIBE_SKIP_PERMISSIONS` | `false` | Bypass Vibe permission checks (local debugging only) |
| `AINGEL_INSTANCE_NAME` | | `Vault` unlocks vault-only features (Scaleway sessions, RAG corpora) |
| `AINGEL_TZ` | `Europe/Paris` | Timezone for schedules and budget periods |
| `AINGEL_AUTH` | `off` | `off` (single user, no login) or `oidc` (multi-user login) |
| `AINGEL_SESSION_SECRET` | | Session signing key; required when `AINGEL_AUTH=oidc` |
| `AINGEL_OIDC_ISSUER`, `AINGEL_OIDC_CLIENT_ID`, `AINGEL_OIDC_CLIENT_SECRET` | | OpenID Connect provider (e.g. Authentik, Keycloak) |
| `AINGEL_PUBLIC_URL` | | Public origin behind a TLS-terminating proxy, e.g. `https://cordee.example` |
| `AINGEL_FREE_MAX_TOKENS` | `100000` | Free-tier monthly token cap per user (in + out) |
| `AINGEL_DAV_TOKEN` | | WebDAV password. Without it, WebDAV refuses every request. |
| `AINGEL_DAV_HOST` | any `dav.*` host | Dedicated WebDAV host serving `/<slug>/` without `/dav` |
| `AINGEL_DAV_TRUST_HEADERS` | off | Trust proxy identity headers (only behind a proxy that strips client copies) |
| `AINGEL_BIND_HOST` | `127.0.0.1` | Listen address. Keep loopback behind a proxy or tunnel. |
| `AINGEL_WAITRESS_THREADS`, `AINGEL_WAITRESS_CONNECTIONS` | `64`, `256` | Server pool size |
| `AINGEL_MAX_CONTENT_LENGTH` | 128 MiB | Max upload / WebDAV PUT size in bytes |
| `AINGEL_BACKUP_DIR` | `/var/backups/cordee` | Central-DB snapshots (`ops/examples/backup.sh`) |
| `AINGEL_BACKUP_RCLONE_REMOTE` | | Optional off-host backup copy (`rclone` remote:path) |

### Historical identifiers

Cordée was developed under the working names *AIngel* and *SuperAgent*. A few
internal identifiers keep those names because renaming them would break
existing installs: the `AINGEL_*` environment variables, the central database
file `aingel.db`, the per-project `aingel.json`, Python module and component
names, and the `aingel:` WebDAV property prefix. They are the current,
supported names.

## Databases

Two kinds of SQLite database, both in WAL mode:

| File | Purpose |
|------|---------|
| `aingel.db` (beside the code) | Central registry: projects, users, roles, work sessions, dependencies, ID registries |
| `<projects root>/<project>/project.db` | Per project: tasks, executions, chats, skills, permissions |

**Never copy or overwrite a live DB file directly.** A raw `cp` can miss
recent commits held in the `-wal` file. Back up with `sqlite3 .backup`, as
`ops/examples/backup.sh` does, and restore with the steps in
[ops/examples/RESTORE.md](ops/examples/RESTORE.md).

## Running as a service

[`ops/examples/`](ops/examples/) has systemd templates. They assume the code in
`/opt/cordee` and a `cordee` service user; adjust both before installing.

| File | Purpose |
|------|---------|
| `cordee.service` | Main app on `127.0.0.1:8001`. `ExecStop` runs `ops/stop_guard.py`, which marks executions still running at shutdown as failed. |
| `cordee-webdav.service` | Optional standalone WebDAV origin on `127.0.0.1:8002` |
| `cordee-db-backup.service` / `.timer` | Daily database backup at 04:30 |
| `backup.sh` | The backup script (WAL-safe, integrity-checked, with retention) |
| `RESTORE.md` | Restore runbook |

```bash
sudo cp ops/examples/cordee.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now cordee
sudo journalctl -u cordee -n 30
```

Put the app behind a reverse proxy or tunnel that terminates TLS (Caddy,
nginx, cloudflared…), keep `AINGEL_BIND_HOST=127.0.0.1`, and set
`AINGEL_PUBLIC_URL` to the public origin.

### After updating the code

```bash
git pull
pip install -r requirements.txt                          # if requirements changed
(cd frontend && npx tsc -b --noEmit && npm run build)    # if frontend/ changed
sudo systemctl restart cordee
```

## WebDAV file access

Mount any project's `Working Documents` in your OS file manager. The main app
serves WebDAV at `/dav/<slug>/`; the in-app **Files → WebDAV** panel shows the
exact URL for your host. Authentication uses `AINGEL_DAV_TOKEN` as the
password (any username). The token opens every project, so treat it as an
operator credential.

Client setup (Windows, macOS, Linux, rclone), the optional dedicated
`dav.<your-domain>` host and troubleshooting are in
[docs/WEBDAV_SETUP.md](docs/WEBDAV_SETUP.md).

## Key files

| File | Purpose |
|------|---------|
| `agent_api.py` | Flask server and REST endpoints |
| `agent_router.py` | Provider dispatch (Anthropic, Mistral, Scaleway, OpenAI, Ollama) and the EU guard |
| `agent_executor.py` | Task and chat runner |
| `agent_overseer.py` | The Guide: pre-run check, post-run review, overview |
| `agent_db.py` | Two-DB architecture (central `aingel.db` + `project.db` per project) |
| `agent_permissions.py` | Permission system for the Claude and Vibe CLIs |
| `agent_webdav.py` | WebDAV provider |
| `frontend/` | React + TypeScript + Vite UI (served at `/`) |
| `.claude/settings.json` | Claude permission config |

## Limitations

- **Vibe bash:** all-or-nothing permission (no per-command rules like Claude).
- **Model gating:** small and medium Mistral models use API mode because of
  limited tool-following.
- **Session cleanup:** the Vibe CLI logs sessions to `~/.vibe/sessions/` with no
  automatic cleanup. Clean it manually if the disk fills.
