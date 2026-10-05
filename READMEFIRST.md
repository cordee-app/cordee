# READMEFIRST.md — Cordée (formerly AIngel)

> Architecture reference for contributors and AI coding agents. Install and
> operations: `SETUP.md`. Internal identifiers keep the historical names
> *AIngel* / *SuperAgent* (see `SETUP.md` → Historical identifiers).
> **Host-specific deployment:** if a `VAULT.md` exists at the repo root, it
> describes this host (paths, domains, units) and takes precedence over the
> generic defaults below.
> Specialist AI Team Architecture (Roles, Dependencies, Master Spec, Counselor AI) — **complete, promoted 2026-06-10**.
> Phase 2 (per-project database split) — **complete**.
> Phase 3 (project definition as execution model — `aingel.json`, execution types) — **complete**.
> Phase 5 (AIngel persona per project — named AI, `role.default_model`, `aingel_brief` auto-generation) — **complete**.

## Project Identity

- **Name:** Cordée, formerly AIngel (AI-agnostic project orchestrator)
- **Purpose:** Multi-AI project orchestrator. Web dashboard to manage, queue, and execute work across all projects using Claude, Mistral, Scaleway, and OpenAI. Cost tracking, model selection, and budget enforcement per project. **DB is the source of truth** for all task state; the WebApp is the interface.
- **Location:** the git checkout (central DB `aingel.db` lives beside the code)
- **Projects root:** env `AINGEL_PROJECTS_ROOT` (default `projects/` beside the code)
- **Port:** 8001 (bound to `127.0.0.1` via `AINGEL_BIND_HOST`; served by `waitress` when installed)
- **Start:** `python3 agent_api.py` (or the systemd unit from `ops/examples/cordee.service`)
- **DB:** `aingel.db` (central registry) + `project.db` per project (see Two-DB Architecture)

## Architecture — AIngel Core

```
AIngel/
├── agent_api.py          ← Flask server (port 8001) + REST endpoints
├── agent_router.py       ← Provider dispatch (Anthropic, Mistral, OpenAI/Codex CLI; Google hidden)
├── agent_executor.py     ← Task runner + prompt builder + memory
├── agent_importer.py     ← Initial import from GUIDE.md; DB is authoritative after that
├── agent_phases.py       ← Phase parser (queries DB; falls back to GUIDE.md for unimported projects)
├── agent_db.py           ← SQLite: two-DB architecture (aingel.db central + project.db per project)
├── agent_chats.py        ← Chat file I/O (create, append, read transcripts)
├── agent_memory.py       ← Artifacts/ + *.memory.md read/append/remove, GUIDE.md ✅-flip
├── agent_skills.py       ← Skills auto-detection (scans project, writes Skills.md, mirrors to DB)
├── agent_permissions.py  ← Per-project + global Claude Code permission management
├── agent_git.py          ← Git-validated execution helper (per-task branch/commit/merge/revert)
├── agent_overseer.py     ← Phase 7 AIngel Autopilot hooks (H2 pre-run check, H3 post-run analysis, H4 overview)
├── agent_config.py       ← Model registry, pricing, API keys, READMEFIRST.md resolver
├── agent_tools.py        ← Tool definitions (file I/O) used by Scaleway function-calling loop
│                            Tools: list_files, list_working_docs, read_file (start_line/end_line),
│                                   write_file, patch_file (targeted old→new replacement), get_project_memory
│                            read_file() returns base64 + binary:true flag for .pdf/.db/.xlsx/.docx etc.
├── agent_mcp.py          ← FastMCP server exposing project file tools over stdio/SSE
├── prompt_builder.py     ← File auto-catch + chunk-and-aggregate batch mode (see section below)
├── model_caps.py         ← Per-model capability lookup; loads model_caps.json
├── model_caps.json       ← max_tokens / context_window / tools per model (source of truth)
├── agent_webdav.py        ← WebDAV provider at /dav/<slug>/ (Lane C): PROPFIND/GET/PUT/MKCOL/DELETE/MOVE/COPY + aingel:tags deadprops, DAV-token auth, agent_files checks
├── frontend/             ← Primary React + TypeScript + Vite frontend (served at /)
│   └── src/components/   ← 20+ React components (Sidebar, TaskBoard, ChatPanel, FilesBrowser (WebDAV button), …)
├── ops/examples/         ← systemd templates (app, standalone WebDAV on 127.0.0.1:8002, daily DB backup) + backup.sh + RESTORE.md
├── ops/stop_guard.py     ← ExecStop hook: fails executions still running at shutdown
├── aingel.db             ← Central SQLite registry (projects, roles, work_sessions, task_dependencies, registry tables)
├── migrate_to_per_project_db.py ← One-time migration script (Phase 2; already run)
├── .mcp.json             ← MCP server registration for Claude Code (project-level)
├── .gitignore            ← Excludes .env, aingel.db*, logs, backups, Artifacts/
├── .env                  ← API keys + AINGEL_PROJECTS_ROOT (git-ignored)
└── SETUP.md              ← Quick start guide
```

## Two-DB Architecture (Phase 2)

| DB | Location | Tables |
|----|----------|--------|
| `aingel.db` (central) | `aingel.db` beside the code | `projects`, `roles` (global + project-scoped), `work_sessions`, `project_type_templates`, `gpu_sessions`, `task_dependencies`, `task_registry`, `exec_registry`, `chat_registry` |
| `project.db` (per-project) | `<AINGEL_PROJECTS_ROOT>/<name>/project.db` | `tasks`, `executions`, `chats`, `sessions` (legacy), `project_skills`, `project_permissions` |

**Registry tables** (`task_registry`, `exec_registry`, `chat_registry`) in `aingel.db` hold globally-unique integer IDs that map to a project path, enabling blind lookups like `GET /api/tasks/<id>` without knowing the project.

**Registry ≠ row:** a registry table stores only `id → project_path`, never the task/execution/chat's own columns (title, status, timestamps, etc). Querying `aingel.db` directly for `SELECT * FROM tasks WHERE id=N` will find nothing even for a perfectly valid id — the row only exists in that project's `project.db`. Always resolve `project_path` from the registry first, then query the right `project.db`.

**Cross-project queries** (`get_cost_summary`, `get_executions`, `reset_orphaned_executions`) iterate `_all_project_paths()` and aggregate from each `project.db` in Python.

**Roles:** all roles (both global `project_id IS NULL` and project-scoped) live in `aingel.db`. The per-project `project.db` does not have a `roles` table. `apply_role_template()` clones template roles into `aingel.db` with the target `project_id` set.

### Task ID vs Execution ID

Two independent counters, easy to confuse in the Execution Log:

- **`tasks.id`** — globally unique, allocated once via `task_registry` autoincrement in `aingel.db`, then written into the project's `project.db`. One per task, for its lifetime.
- **`executions.id`** — locally autoincrement within each project's own `project.db`. One per run/attempt; `executions.task_id` is a foreign key back to `tasks.id`.

A task can have multiple executions (retries, resumes, re-runs), so the "Task #" and the execution's own id on the same Execution Log row are unrelated numbers — that's expected, not a bug. Rendered in the dashboard's ExecutionLog component.

## Three-Layer Project Structure (per project)

```
<AINGEL_PROJECTS_ROOT>/<ProjectName>/
├─ DEFINITION LAYER
│  ├─ READMEFIRST.md  ← Architecture, patterns, constraints (AI-agnostic; new projects)
│  │   (legacy: CLAUDE.md — kept if already present; code resolves via resolve_def_filename())
│  ├─ GUIDE.md        ← Phase roadmap (initial import only; ✅ auto-updated on approval)
│  └─ Skills.md       ← Capabilities (auto-detected, user-editable)
├─ My Docs/           ← User reference materials (pdf, docx, txt, images, uploads/)
│   (also accepted: Working Docs, Working Documents, working-docs, docs)
├─ ARTIFACTS/
│  ├─ project.memory.md, phase-N.memory.md
│  ├─ chats/<scope>-<slug>-<id>.chat.md
│  └─ outputs/<task-id>-<slug>/    ← ALL task deliverables + exec-<id>-output.md logs
├─ project.db         ← Per-project SQLite (tasks, executions, chats, skills, permissions)
└─ [source code, tests, etc.]
```

**Source of truth:** SQLite DB (`project.db`). GUIDE.md is for initial import only. TASKS.md is retired.

**Output location rule (2026-09-28):** a task's output files never go in the project root or in Working Docs (the user's reference material). They go in `Artifacts/outputs/<task-id>-<slug>/` (`agent_executor.task_output_dir`; an existing `<id>-*` folder is reused, so renaming a task doesn't split its outputs). Files there are attributed to task `<id>` in Files even when no commit claims them (`agent_filecat.task_id_from_output_folder`), and the Exec Log lists files changed in that folder during the run. `scripts/migrate_outputs_by_task.py` moved older flat/title-named outputs into numbered folders (2026-09-28). Tasks read inputs from Working Docs *and* `Artifacts/outputs/`. Enforced by: the improver prompt (never gives outputs a folder), agentic prompt rule 9, `agent_tools.write_file` (new files → the run's `output_dir_scope`), batch-mode extraction, and a post-run sweep (`_relocate_new_root_files`) that moves new non-ignored root files into the output folder (skipped for `software` projects). Deliverables there are git-versioned (`agent_git.ARTIFACTS_IGNORE_RULES`: `Artifacts/*` ignored except `outputs/`, exec logs ignored). Users see outputs read-only on Y: and in Files; write/delete stays limited to Working Docs.

**Definition file resolution:** `agent_config.resolve_def_filename(project_path)` prefers `READMEFIRST.md`, falls back to `CLAUDE.md`. The frontend resolves the def file name via the `def_filename` field returned by `/api/projects`.

## Project Archive & Restore

A project can be exported to a single `.aingel.zip`, removed, and later re-imported. Implementation: `agent_archive.py` (build/restore/reaper) + central-row helpers in `agent_db.py` + three routes in `agent_api.py`.

**Archive zip layout**

```
<slug>-<YYYYMMDD-HHMMSS>.aingel.zip
├─ manifest.json     ← projects row, roles, project_hf_models, project_members,
│                       task_dependencies, task/exec/chat registries, scw_* spend
│                       rows, format + version + original path/name/slug
└─ project/          ← the project tree
   ├─ project.db     ← WAL-safe snapshot (SQLite online backup API — NOT a copy)
   ├─ Artifacts/, Working Documents/, .git/ (full history), READMEFIRST.md, ...
Excluded: .uploads/, project.db-wal/-shm, project.db.bak-*, legacy .trash/
```

**Endpoints**

| Method | Path | Notes |
|--------|------|-------|
| POST | `/api/projects/<pid>/archive` | Owner. Non-destructive build (syncs SCW bucket → Working Documents first). Returns `{filename, size, download_url, import_limit[, sync_warning]}`; 409 on running/queued work. |
| GET | `/api/projects/<pid>/archive/<filename>` | Owner. Serves the built zip `as_attachment`, but only from **that pid's** subdirectory. |
| POST | `/api/projects/import` | multipart `file`. `check_project_create` quota first; returns `{ok, project}`. |

Built zips live in `<PROJECTS_ROOT>/.archives/<pid>/<slug>-<ts>-<rand4>.aingel.zip` — the per-pid subdirectory is what stops one project owner from downloading another project's archive by guessing its filename (the GET route authorizes on `pid` and resolves only inside that pid's dir). The directory is outside every project tree so quota storage walks don't count it, and entries are reaped after 7 days by `_reap_archives()`, next to `_reap_trash()`.

**Invariants baked into restore**

- **WAL:** export snapshots `project.db` via `Connection.backup()`. A plain copy would miss committed pages still in `-wal`.
- **Global IDs:** registry ids (and every task/exec/chat PK) are re-inserted **verbatim** with explicit `INSERT ... (id, ...)`; SQLite bumps `sqlite_sequence` to the new max. No new ids are invented.
- **v1 is same-instance only:** a *fatal* collision (project name/slug/folder/id, roles, HF models, deployments, session costs, registries) is a hard 409 before a single write (`ArchiveConflictError`). Two row classes are **skippable** instead of fatal: `task_dependencies` and `scw_deployment_calls`, because a dependency edge or a shared-window call can legitimately appear in two projects' archives — they are re-inserted with `INSERT OR IGNORE` and skips are logged, so archiving and restoring two linked projects both succeed. Cross-instance moves remain the job of `scripts/import_vault_into_laptop.py`.
- **Path/name validation:** restore enforces the same `^[A-Za-z0-9 _\-]+$` project-name regex as `create_project` and a realpath containment check under `PROJECTS_ROOT`; the sanitized name/slug are written back into the manifest so the folder, conflict checks and DB row all agree.
- **Paths repointed:** `projects.path` + the 3 registry `project_path` columns + `chats.file_path` (which also self-heals by basename).
- **SCW sessions:** `scw_*` columns are cleared at export and restore (the existing DELETE route crypto-shreds the bucket/KMS, irreversibly). A restore starts session-less; open a fresh session after.
- **Restore safety:** entries are validated **before** the manifest is read (so a small zip cannot force a large manifest allocation; the manifest is capped at 1 MiB). Zip-slip, absolute paths, symlink/device/fifo entries, tree conflicts, entry-count and uncompressed-size caps are all rejected before extraction (no `extractall`). The extracted `project.db` is probed for SQLite validity before anything is moved or committed, and the central insert is tracked so a failed restore never leaves half-committed rows. Restores are serialized by a lock and use `os.rename` (no clobbering). Symlinks and special files in the **source** tree are skipped at export too.
- **Removal:** archive then remove reuses the existing `DELETE /api/projects/<pid>` route (guards, SCW shred, GPU windows, trash, audit) unchanged. The frontend uses a **two-step** flow — build + download in step 1, then a separate "Remove project" confirmation in step 2 — so a cancelled/failed save can never delete a project whose zip was not written; if the zip exceeds the import limit the modal warns it cannot be restored through the app until acknowledged.

**Known limits:** cross-instance restore refused (use the import scripts); out-of-tree soft-delete trash (`TRASH_ROOT/<project_id>`) is not in the zip; SCW bucket sync caps at 1000 objects (`list_objects_v2`); import upload is capped by `MAX_CONTENT_LENGTH` (surfaced as `import_limit` so the UI can warn); archives are unencrypted — treat them as confidential. Tests: `test_agent_archive.py`.

## Chat Architecture

Chats are the primary conversation unit, scoped to project / phase / task.

| Scope | File location |
|-------|---------------|
| project | `Artifacts/chats/project-<slug>-<id>.chat.md` |
| phase | `Artifacts/chats/phase-<slug>-<id>.chat.md` |
| task | `Artifacts/chats/task-<slug>-<id>.chat.md` |

**No context is auto-loaded.** Each chat has an explicit `attachments` list (definition / memory / task / working_doc). Prompt = attached items (30k char cap each) + rolling chat history (12,000 char cap).

**Memory accumulation:** EXECUTION OUTPUT → CHAT HISTORY → PHASE MEMORY → PROJECT MEMORY. User approves each step. DB is source of truth; GUIDE.md updated on approval (progress display only).

**Key invariant:** `get_executions` filters `WHERE chat_id IS NULL` — chat executions are reviewed in the chat UI, not the execution log. Never set `chat_id` on task-run execution rows.

**Sessions→chats (retired 2026-06-10):** `sessions` table and `session_id` columns remain in schema for legacy rows only. New code uses `chat_id` exclusively.

## Implementation Phases

- **Phase 1** ✅ Web UI + task import + model selection + real executor
- **Phase 2** ✅ Real AI execution (Anthropic + Mistral + OpenAI/Codex CLI + Scaleway working; Google hidden)
- **Phase 3** ✅ Memory/Chat architecture — DB+API+UI complete
- **Specialist AI Team** ✅ Roles, Dependencies, Master Spec, Counselor AI — complete, promoted 2026-06-10
- **AIngel Phase 1** ✅ Relocate & restructure — READMEFIRST.md, systemd — complete 2026-06-24
- **AIngel Phase 2** ✅ Per-project DB split — `aingel.db` + `project.db`, registry tables, cross-project aggregation — complete
- **AIngel Phase 3** ✅ Project definition as execution model — `aingel.json` per project, `execution_type` column, executor branching (software/research/deployment) — complete 2026-07-14
- **AIngel Phase 4** ⬛ GitHub-ready (de-personalise, open source) — **next**
- **AIngel Phase 5** ✅ AIngel persona per project — named AI (`aingel_name` injection), `role.default_model` fallback, `aingel_brief` auto-generation (Haiku, daemon thread), inline brief in approval row, role edit form — complete 2026-07-14
- **AIngel Phase 7** ✅ AIngel Autopilot — 4 medium-AI oversight hooks (H1 improve nudge, H2 pre-run brief + completeness check, H3 post-run analysis, H4 rolling overview), per-project toggle (`aingel_autopilot`), advisory/strict gate modes — complete 2026-07-17
- **AIngel Phase 6** ⬛ Scaleway deployment & batch pipelines

## Task Lifecycle

```
pending → confirmed → running → done
                  ↘ skip
                  ↘ failed → (reset) → pending
```

Feedback loop: Approve (→ memory) / Reject + Feedback (→ new task) / Skip / Fork (→ new chat). For git-enabled projects, Approve = merge branch, Reject = discard branch, Revert = `git revert`.

## Available Models

| Model | AIngel ID | Provider | Input $/M | Output $/M | Slot |
|-------|-----------|----------|-----------|------------|------|
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | Anthropic | $3.00 | $15.00 | 1/3 |
| Claude Haiku 4.5 | `claude-haiku-4-5-20251001` | Anthropic | $0.80 | $4.00 | 1/3 |
| Claude Opus 4.7 | `claude-opus-4-7` | Anthropic | $15.00 | $75.00 | 1/3 |
| Mistral Large | `mistral-large-latest` | Mistral | $2.00 | $6.00 | 3 |
| Mistral Small | `mistral-small-latest` | Mistral | $0.10 | $0.30 | 2 |
| Mistral Medium | `mistral-medium-latest` | Mistral | $0.40 | $2.00 | 2 |
| GLM-5.3 (Mistral Pro) | `mistral-glm-5-3` | Mistral | $0.00 | $0.00 | 2 |
| Codex ChatGPT | `codex-chatgpt` | OpenAI | $0.00 | $0.00 | 3 |
| SCW Qwen3-Coder 30B | `scw-qwen3-coder-30b` | Scaleway | $0.22 | $0.86 | 4 |
| SCW GPT-OSS 120B | `scw-gpt-oss-120b` | Scaleway | $0.16 | $0.65 | 4 |
| SCW Llama 3.3 70B | `scw-llama-3.3-70b` | Scaleway | $0.97 | $0.97 | 4 |
| SCW Mistral-Small 24B | `scw-mistral-small-24b` | Scaleway | $0.16 | $0.38 | 4 |
| SCW Gemma-4 26B | `scw-gemma-4-26b` | Scaleway | $0.27 | $0.54 | 4 |
| SCW Mistral-Medium 128B | `scw-mistral-medium-128b` | Scaleway | $1.62 | $8.10 | 4 |
| SCW Qwen3 235B | `scw-qwen3-235b` | Scaleway | $0.81 | $2.43 | 4 |
| SCW Qwen3.6 35B | `scw-qwen3.6-35b` | Scaleway | $0.27 | $1.62 | 4 |
| SCW Qwen3.5 397B | `scw-qwen3.5-397b` | Scaleway | $0.65 | $3.89 | 4 |
| SCW GLM-5.2 | `scw-glm-5.2` | Scaleway | $1.94 | $5.94 | 4 |

Scaleway prices in USD (EUR × 1.08). For provider routing details see `agent_router.py` (`route()` and the EU guard).

## Work Sessions & Kanban Columns

`tasks.work_session_slot` is a **provider-bound Kanban column**:

| Slot | Column | Accepts |
|------|--------|---------|
| NULL | Unassigned | any model |
| 1 | Claude Pro | Claude models only |
| 2 | Mistral Pro | Mistral models only (not Large — PAYG) |
| 3 | PAYG | any non-Scaleway model |
| 4 | EU Scaleway | `scw-*` models only; function-calling loop, pay-per-token |

- 5-hour window, 150k token budget per slot. `start_work_session(slot, force)` starts the countdown.
- `work_sessions.tokens_used` accumulates actual `tok_in + tok_out` after every run.
- **Slot-overflow auto-promotion**: tasks moved to slot 1/2 that exceed remaining budget are auto-moved to slot 3.
- `scw-*` models rejected by frontend and backend if placed in slots 1–3.
- Token estimates: `estimate_task_prompt_tokens(task_id)` auto-runs on task creation; default fallback 50k.

## EU-only Projects (data residency)

Projects created with `eu_only=1` may run **only EU-operated models — Mistral and Scaleway (`scw-*`)**. Claude (Anthropic/US), Codex (OpenAI/US) and Ollama Cloud (`oll-*`, US) are excluded.

- `_is_eu_model(model_id)` in `agent_api.py` is the single source of truth (`scw-*` / `mistral-*` / `open-mistral*`).
- Enforced at the API boundary by `_eu_guard(proj, model_id)`: **task creation** (`POST /api/tasks`), **model change** (`PATCH /api/tasks/<id>`) and the **Counselor** (`recommend-model` filters non-EU models out entirely).
- The React model picker (`AddTaskModal`) also hides non-EU models and snaps an illegal selection back to a compliant one — but the backend guard is the real enforcement (API-direct callers are rejected with HTTP 400).

## Model Counselor (recommend-model)

`POST /api/tasks/<id>/recommend-model` ranks the whole catalogue for a task (UI: **🤖 Suggest Model** in the task editor). It is **capability + profile driven**, not a frozen table:

- **Subjective inputs** (which model is `frontier`/`strong`/`mid`/`light`, each model's `strengths` tags, and task-type keywords) live in **`model_profiles.json`**, loaded by `model_profiles.py`. A model **absent** from the JSON still ranks via `provider_defaults` → neutral fallback, so newly-registered models participate with zero edits.
- **Objective inputs** (context window, vision, tools, cost, EU/provider) are read **live** from `agent_config.MODELS` + `model_caps.json` — never duplicated in the profile file.
- Scoring (`_score_model`, lower = better): tier band → strength match (refines within band) → hard gates (vision required, context too small, tools needed for agentic) → cost (fraction of remaining budget, heavy bias when tight). EU-only projects only ever see EU models.
- Response carries `tier`, `strengths`, `eu`, `context_window`, a human `reason`, and `profiles_reviewed` (the JSON's `last_reviewed` date, surfaced in the UI).
- **Staying current:** refresh `model_profiles.json` when the landscape shifts — bump `_meta.last_reviewed` / `review_after` (target: quarterly). Because objective caps and the model list come from live config, most model additions need only their normal `agent_config`/`model_caps` entry; the profile line is optional polish.

## MCP Server (`agent_mcp.py`)

FastMCP server: `list_files`, `list_working_docs`, `read_file` (start_line/end_line), `write_file`, `patch_file`, `get_project_memory`. Registered via `.mcp.json` (not `.claude/settings.json` — that field is rejected). Transport: stdio (default) or SSE (`--transport sse`). Most useful for clients without native file access (Telegram bot, remote agents).

## Prompt Builder (`prompt_builder.py`)

Entry point: `build(task, project_path) → {prompt, mode, max_tokens, batch_payload}`.

**File auto-catch** — scans every Working-Docs folder (all variants: `My Docs`, `Working Docs`, `Working Documents`, `working-docs`, `docs`) plus `Artifacts/outputs/`. A file is injected if:
- Its exact filename appears in the task description, OR
- Its extension-less stem appears as a whole word and is "identifier-like" (has `_`, `-`, digit, or uppercase, ≥ 6 chars) — catches `FULL_TEXT_OCR` matching `FULL_TEXT_OCR.txt`.

Binary files (`.pdf`, `.db`, `.xlsx`, `.docx`, etc.) are noted in the prompt but not inlined.

**Execution modes:**
- `inline` — file fits within 70% of model's context window → injected directly into prompt
- `batch` — file too large → `run_large_file_batch()` in `agent_executor.py` splits it into ~280k-char chunks, processes each (map), then hierarchically collapses results (reduce), and synthesises a final answer

**`model_caps.json`** fields: `max_tokens` (API output cap), `context_window` (total window), `tools` (function-calling support). Scaleway: 32768 / 131072 / true. No `body_cap` or `deployment` fields — those were misleading.

## Development Workflow

1. Develop and test in your checkout: `python -m unittest discover -s . -p 'test_agent_*.py'`.
2. Rebuild the frontend if TSX/TS changed: `cd frontend && npx tsc -b --noEmit && npm run build`.
3. Restart the server (`sudo systemctl restart cordee` with the example unit) and check its logs.

Cordée can manage its own development as a project (git-validated execution: edits → task branch → approve → merge). Commit your own changes first, or the task's checkpoint sweeps them in.

**DB safety:** Never copy or overwrite `aingel.db` or any `project.db` files. They are the source of truth.

## Deployment

See `SETUP.md` → Running as a service. Summary:

- **Web app:** Flask on port 8001, bound to `127.0.0.1` (env `AINGEL_BIND_HOST`), served by `waitress` when installed (fallback: Flask dev server). Put a TLS-terminating reverse proxy or tunnel in front and set `AINGEL_PUBLIC_URL`.
- **systemd:** `ops/examples/cordee.service` (`ExecStop=` runs `ops/stop_guard.py`).
- **WebDAV:** integrated at `/dav/<slug>/`; optional standalone origin on `127.0.0.1:8002` (`ops/examples/cordee-webdav.service`) for a `dav.*` host. Auth via `AINGEL_DAV_TOKEN`.
- **Backups:** `ops/examples/backup.sh` + `cordee-db-backup.timer` (daily 04:30): WAL-safe `sqlite3 .backup`, integrity-checked, retention, optional off-host copy via rclone (`AINGEL_BACKUP_RCLONE_REMOTE`). Restore runbook: `ops/examples/RESTORE.md`.

**Frontend build:** after TSX/TS edits run `cd frontend && npx tsc -b --noEmit && npm run build`.

## Frontend (React + TypeScript + Vite)

Primary UI at `/`, built from `frontend/`. 20+ React components: Sidebar, Header, Dashboard, TaskBoard, TaskCard, ExecutionLog, ChatPanel, ChatsPanel, AddTaskModal (unified create+edit), DependenciesModal, NewProjectModal, SettingsModal, BudgetModal, DependencyGraph, MemoryPanel, AIngelPanel, Notifications, ProviderIndicators, SearchModal, AttachPicker, ScaffoldProgress.

- **Home mode**: Dashboard full-width, no sidebar/exec log. Shows project cards with costs.
- **Project mode** (enter by clicking a project): sidebar with phase accordions + task list, tabs, execution log, slide-in panels for Chats/AIngel/Memory.
- **Color scheme**: soft gray-blue background `#f4f5f7`, accent `#3b6cf4`, base font 14px.
- **Build**: `cd frontend && npx tsc -b --noEmit && npm run build`. Flask serves `dist/` as static files.
- **Scaleway prompt behaviour** (`_build_prompt()` in `agent_executor.py`): `is_scw` flag branches `## Output format`. Scaleway models receive tool-use instructions (call tools, don't paste code blocks). Non-scw models receive the standard prose format.

## Runtime Safety Notes

- `ANTHROPIC_MODE=claude-code` routes through the local `claude` CLI with `--permission-mode acceptEdits`. Bash gated by project's `.claude/settings.json`.
- **systemd PATH**: the service file needs `Environment=PATH=<service user home>/.local/bin:...` or `shutil.which("claude")` / `shutil.which("vibe")` return None silently.
- **`route()` kwargs forwarding**: all provider functions must accept `**_` to absorb `force_*_mode` kwargs they don't use.
- **Stop guard** (`ops/stop_guard.py`, wired as `ExecStop=` in the systemd unit, see `ops/examples/cordee.service`): on every `systemctl stop|restart`, marks all `running` executions `failed` and resets their tasks to `pending` **immediately** (no grace). The unit has `KillMode=control-group`, so a stop kills every agent process; a restart mid-task fails that task and you re-run it. (It was `ExecStopPre=`, not a systemd directive, and never ran before 2026-09-24.)
- **Startup orphan-sweep** (`reset_orphaned_executions()` at boot): backup for crashes, where `ExecStop` does not run. Marks stale `running` executions `failed`, resets tasks to `pending`. **10-minute grace period**: execs started within the last 10 minutes are skipped.
- **Flask `threaded=True`** required: concurrent `/api/estimate` calls at page load block the single thread without it.
- **Self-hosting**: git-validated execution checkpoints uncommitted changes before branching. Commit before letting a task run on a project that is Cordée's own checkout, or edits get swept in.
- **Chat git commits** (`commit_chat()` in `agent_git.py`): chat edits are committed to `main` immediately after each reply, before any task branch stash operation.
- **Queue handoff**: cleared on task success, task reset to pending, or description edit — never inherited stale.
- **`Bash(cd:*)` and `Bash(git -C:*)`** needed in allow list for tasks that cross directories.
- **`call_mistral` warns on `finish_reason='length'`** — Mistral truncates silently at `max_tokens`.
- **DB backups** run daily with the backup timer — see **Deployment**. `aingel.db` and `project.db` files are still the live source of truth; take an extra WAL-safe snapshot before risky operations: `sqlite3 aingel.db ".backup 'aingel.db.bak-$(date +%Y%m%d)-manual'"` (never a raw `cp`: it can miss commits still in the `-wal` file).

## Skills System

`refresh_skills(project_id, project_path, project_name)` scans code files, writes `Skills.md` (preserving user-edited section above `## Auto-Detected Capabilities`), mirrors to `project_skills` DB table in `project.db`.

REST: `GET/POST /api/projects/<pid>/skills`, `POST .../skills/refresh`, `POST/DELETE .../skills/manual`.

## Permissions System

Per-project `.claude/settings.json` + global `~/.claude/settings.json` (A+B+C sentinel block). Same two-store pattern as Skills.

| Group | Default | Semantics |
|-------|---------|-----------|
| `A_read` | ✅ on | allow read |
| `B_write` | ✅ on | allow write |
| `C_bash_safe` | ✅ on | allow safe bash |
| `D_bash_proj` | ✅ on | allow build/test bash |
| `E_destructive` | ⬛ off | **deny** destructive |
| `F_network` | ⬛ off | allow network |
| `G_agent` | ⬛ off | allow agent spawning |
| `H_mcp` | ⬛ off | allow MCP tools |

E is the only deny-group. Per-project Vibe config (`.vibe/config.toml`) generated from the same DB rows. REST: `GET/POST /api/projects/<pid>/permissions`, `.../permissions/refresh`, `.../permissions/toggle`, `.../permissions/custom`.

## Roles System

Tasks carry an optional `role_id` that prepends a specialist system prompt. 8 global roles seeded in `init_db()`. `_build_prompt()` injects `## Role\n{system_prompt}` first when set.

| Name | Default model |
|------|--------------|
| Polish Legal Specialist | claude-opus-4-7 |
| Business Analyst | claude-sonnet-4-6 |
| Backend Engineer | claude-sonnet-4-6 |
| Audio/DSP Engineer | claude-opus-4-7 |
| 3D/Mechanical Engineer | claude-opus-4-7 |
| Market Researcher | mistral-large-latest |
| Documentation Writer | mistral-large-latest |
| DevOps/Sysadmin | claude-sonnet-4-6 |

REST: `GET/POST /api/roles`, `PUT/DELETE /api/roles/<rid>`, `GET/POST /api/projects/<pid>/roles`.

## Project-Type Templates

Project categories live in the `project_type_templates` table in `aingel.db`:

```sql
project_type_templates (id, name, description, role_names TEXT)  -- role_names is a JSON array
```

When a project is created with a type, `apply_role_template()` clones the listed roles into `aingel.db` with the target `project_id` set. The global roles (`project_id IS NULL`) are the templates; the cloned rows (with `project_id` populated) are the live per-project copies, also in `aingel.db`.

Six types are seeded on first boot (`init_db()` — only when table is empty):

| Type | Roles seeded |
|------|-------------|
| Software | Backend Engineer, DevOps/Sysadmin, Documentation Writer |
| Business Plan | Business Analyst, Market Researcher, Documentation Writer |
| Hardware | 3D/Mechanical Engineer, Documentation Writer |
| Research | Market Researcher, Documentation Writer |
| Financial | Business Analyst, Documentation Writer |
| Legal | Polish Legal Specialist, Documentation Writer |

**To add a new category** on a running system (DB already seeded — the seed block is guarded by `IF COUNT(*) == 0`):

```sql
-- Step 1: ensure all needed roles exist as global roles in aingel.db
-- (create via Roles UI or POST /api/roles if not already present)

-- Step 2: insert the template
INSERT INTO project_type_templates (name, description, role_names)
VALUES ('Data Science', 'ML / data analysis project',
        '["Business Analyst", "Documentation Writer"]');
```

The UI dropdown (`GET /api/roles/templates`) is a live query — the new type appears immediately on next page load, no restart needed.

**Constraint:** every name in `role_names` must match an existing global role name exactly (case-sensitive). Mismatches are silently skipped by `apply_role_template()`.

For new installs, also add the type to `_SEED_TEMPLATES` in `agent_db.py:init_db()` so fresh DBs get it automatically.

REST: `GET /api/roles/templates`, `POST /api/projects/<pid>/roles/from-template` (body: `{template_id}`).

## Project Execution Model (`aingel.json`) — Phase 3

Each project has an `aingel.json` file at its root, auto-generated from the DB and updated whenever project settings change. It gives any AI opening the project a machine-readable config snapshot.

**DB is authoritative.** `aingel.json` is generated by `_write_aingel_json()` in `agent_api.py`; do not edit it by hand. It is re-generated on: project create, settings PATCH, git toggle, and service startup (bootstrap for missing files).

### `aingel.json` schema

```json
{
  "name": "Family",
  "type": "Software",
  "execution_type": "standard",
  "eu_only": true,
  "llm_mode": "specific",
  "budget_monthly": 50.0,
  "git_enabled": 1,
  "aingel_name": null,
  "deployment_config": null,
  "aingel_version": "1.0"
}
```

### Execution types

| Type | Runtime behaviour | git |
|------|-------------------|-----|
| `standard` | Current task loop — confirm → run → approve | `git_enabled` column / auto-detect |
| `software` | Force git on unconditionally (bypasses `resolve_enabled()`) | Always on |
| `research` | Full memory injection — no SCW caps, all `phase-*.memory.md` files injected | `git_enabled` / auto |
| `deployment` | **Stubbed** — logs a note, runs as `standard`. Full Scaleway batch pipeline is Phase 6 | `git_enabled` / auto |

### AIngel Autopilot modes

| Mode | Effect |
|------|--------|
| `advisory` (default) | H2/H3 gates show banners but do not block execution. User sees warnings post-hoc. |
| `strict` | H2 `gate=hold` aborts the run before `route()` is called. H3 `severity≠ok` sets `gate_state=hold` on the next task. User must explicitly override or discuss.

`execution_type` is stored in `aingel.db projects` table and injected into the task dict as `task['_execution_type']` before `_build_prompt()` is called. UI: project settings panel and Add Project modal both expose a dropdown.

`aingel_name` (optional persona name, e.g. "Léa") is injected as the first line of the system prompt: `"You are Léa, AIngel for the Family project."` — followed by the role system prompt if a role is assigned (Phase 5, complete 2026-07-14).

## AIngel Autopilot — Phase 7

Per-project, opt-in AI oversight of the full task lifecycle. Uses the project's existing `aingel_model` (Mistral Medium, Sonnet, …) with a strictly limited context (~3-5k input tokens per call — never the full task prompt). Enabled on a project via `projects.aingel_autopilot = 1`; binding mode via `projects.aingel_mode` ∈ `{advisory, strict}`.

### Four hooks

| # | Hook | When | Trigger | Binding |
|---|------|------|---------|---------|
| H1 | Guide nudge | task save | user clicks Save without having clicked "Guide my prompt" and the description is non-trivial | soft dialog (no AI call) |
| H1b | Guide my prompt (existing) | user clicks button | manual | manual |
| H2 | Pre-run brief + completeness check (merged) | inside `run_task()`, after `build_prompt()`, before `route()` | one `route(aingel_model, …)` call | advisory banner OR strict block |
| H3 | Post-run analysis | execution `done` (replaces the legacy Haiku 2-sentence brief) | daemon thread, one `route(aingel_model, …)` call | sets `gate_state` on the task; if severity≠ok and the project has an AIngel chat, posts the finding there |
| H4 | Project overview refresh | after H3 completes | daemon thread | informational — rewrites `Artifacts/aingel-overview.md` |

### H2 merged call — strict JSON schema

Single medium-AI call returning both strategic advice AND a prompt-completeness check (referenced files vs. caught files, binary/model capability mismatches):

```json
{
  "completeness": "complete|partial|incomplete",
  "missing_files": [{"name": "...", "exists": false, "reason": "..."}],
  "binary_unparsable": [{"name": "...", "model_can_parse": false, "reason": "..."}],
  "capability_warnings": ["scw-* model has no file access; /opt/agent/ paths unreachable"],
  "strategic_advice": "1-3 bullets",
  "gate": "run|hold|skip",
  "reason": "one sentence"
}
```

**Gate semantics:**
- `gate=run` → proceed normally
- `gate=hold` (advisory) → run proceeds, banner shown on the task card post-hoc
- `gate=hold` (strict) → run aborted before `route()` is called; execution marked `failed` with gate reason, task reset to `pending`; user sees banner with Override & run / Discuss / Acknowledge buttons
- `gate=skip` (strict) → same as hold (user decides whether to skip)
- `gate=skip` (advisory) → banner shown, run proceeds

### H3 — post-run analysis (replaces the legacy Haiku brief)

Structured JSON stored on `executions.aingel_review_json`:

```json
{
  "severity": "ok|warning|issue",
  "findings": ["3 bash calls blocked by permission system (non-fatal)"],
  "recommendation": "approve|discuss|reject",
  "gate_for_next": "proceed|hold|skip",
  "brief_2s": "2-3 sentence summary — also stored on executions.aingel_brief for the inline UI"
}
```

If `severity != ok` and the project has an AIngel chat (`aingel_chat_id`), the finding is auto-posted to that chat for discussion. The dashboard's "Discuss" button on the gate banner opens the chat at that message.

### H4 — rolling overview

Rewrites `Artifacts/aingel-overview.md` (≤2k words) from: last 5 H3 briefs, phase list + task counts, open risks (from H3 history where severity≠ok), and budget snapshot. Surfaced via the **❆ AIngel Overview** button in the main toolbar (opens a modal). Manual refresh: `POST /api/projects/<pid>/aingel-overview/refresh`.

### Cost model

Per 20-task session on Build Agent AI Assistant (Mistral Medium): ~$0.20 total oversight cost — ~20× cheaper than a dedicated H100 hour AND higher quality (purpose-built instruction-following models vs. self-hosted 70B/120B). All hooks reuse the project's `aingel_model`; no new model choice needed.

### DB schema (Phase 7)

```sql
-- aingel.db projects table
ALTER TABLE projects ADD COLUMN aingel_autopilot INTEGER DEFAULT 0;
ALTER TABLE projects ADD COLUMN aingel_mode      TEXT    DEFAULT 'advisory';

-- project.db tasks table
ALTER TABLE tasks ADD COLUMN original_description TEXT;
ALTER TABLE tasks ADD COLUMN gate_state      TEXT DEFAULT 'open';   -- open|hold|closed
ALTER TABLE tasks ADD COLUMN gate_reason     TEXT;
ALTER TABLE tasks ADD COLUMN gate_source     TEXT;                  -- H2|H3
ALTER TABLE tasks ADD COLUMN gate_decided_at TEXT;
ALTER TABLE tasks ADD COLUMN gate_report_json TEXT;

-- project.db executions table
ALTER TABLE executions ADD COLUMN aingel_review_json TEXT;
```

### REST endpoints (Phase 7)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/tasks/<tid>/gate` | Resolve a gate (body `{action: acknowledge\|override\|discuss}`) |
| `GET`  | `/api/projects/<pid>/aingel-overview` | Return the rolling overview markdown |
| `POST` | `/api/projects/<pid>/aingel-overview/refresh` | Manually trigger H4 |
| `PATCH`| `/api/projects/<pid>` (extended) | Now accepts `aingel_autopilot` + `aingel_mode` |

### Files touched

| File | Role |
|------|------|
| `agent_overseer.py` (new) | `pre_run_check()` (H2), `post_run_analysis()` (H3), `refresh_overview()` (H4) |
| `agent_executor.py` | H2 wired before `route()`; `_generate_brief_async` replaced by H3 + H4 chain; legacy Haiku brief kept as fallback |
| `prompt_builder.py` | Exposes `caught_files` + `noted_binary` in the return dict (consumed by H2) |
| `model_caps.json` | Added `vision` + `file_access` per model (consumed by H2 capability cross-check) |
| `agent_db.py` | Phase 7 migrations + `update_execution_review()` + `overview_file_path()` |
| `agent_api.py` | 3 new endpoints + project PATCH extended + `aingel.json` includes the new fields |
| `frontend/` | Autopilot gate banner, `resolveGate()`, AIngel overview panel, autopilot toggle in project settings |

### Roll-back

Every change is additive. Setting `aingel_autopilot=0` on a project disables all four hooks and reverts to today's behaviour (manual buttons, legacy Haiku brief via the fallback path).

### Phase 7.1 — Visibility & Cross-Task Gate (2026-07-20)

Three fixes to Phase 7 based on real task runs (676/677) where AIngel was silent on success, cross-task dependency outcomes weren't consulted, and H2's audit trail was overwritten by H3.

**AIngel chat cadence:** H3 now posts to the project's `aingel_chat_id` for **every** task run (not just problems). Clean runs get a 1-line `✓ Task #N completed: {brief_2s}`; problems still get the full finding block. Chat 116 becomes a running log of AIngel's voice.

**Cross-task gate:** H2 now consults dependency H3 outcomes before deciding whether to run the next task. For each dependency whose `gate_for_next` is `hold`/`skip` (deterministic computation, not model-inferred):
- `strict` mode → aborts before `route()`, resets task to `pending`, logs blocker reason
- `advisory` mode → appends blockers to `capability_warnings`; the model sees dep outcomes in its prompt and may surface them in its `reason` field shown in the gate banner

**H2 audit trail:** New column `gate_report_h2_json` on `tasks` — H2 writes to this column only, H3 keeps owning `gate_report_json`. Both columns are populated independently. H2 now logs on every fire (not just abort), so `journalctl` shows the full audit trail.

```sql
-- project.db tasks table
ALTER TABLE tasks ADD COLUMN gate_report_h2_json TEXT;
```

New DB helpers:
- `update_task_h2_gate(task_id, h2_json, gate_state, gate_reason, gate_decided_at, project_path)` — writes H2 to its own column
- `get_dependency_h3_outcomes(task_id, project_path)` — returns `[{task_id, title, severity, gate_for_next, findings, brief_2s}]` for all dependency tasks, sourced from their `gate_report_json` (H3-only)

No new settings or endpoints. Behaviour follows the existing per-project `aingel_mode`.

### Phase 7.2 — Mandated Agentic Execution (2026-07-20)

Eliminates the "simulation gap" observed in Task 676, where `claude-sonnet-4-6` (a Claude Code CLI model with real filesystem access) was told it *couldn't* run Bash and responded with markdown command blocks instead of acting.

**Root cause:** `_task_uses_direct_text_route()` keyed off `model_caps.tools` (API-level function-calling support). Claude Code, Codex, and Mistral Vibe route through local CLIs that execute tools internally — they have `tools: false` in the API sense but real filesystem access. The function misclassified them as text-only and injected "You cannot run Bash… Do not emit tool-call markup", *causing* the simulation it was trying to prevent.

**Fix — capability signal:** `_task_uses_direct_text_route()` now keys off `model_caps.file_access` instead of `tools`:

| `file_access` | Meaning | Route |
|---------------|---------|-------|
| `native` | CLI with built-in file/bash access | Claude Code, Codex |
| `bash` | CLI running Bash | Mistral Vibe |
| `tools` | API function-calling | Scaleway |
| missing/other | plain text API, no file access | OpenAI PAYG, Google stub |

Only the last row is treated as text-only. The first three are agentic and get the new rules block.

**Fix — agentic rules block:** `_build_prompt()` now emits a `## Agentic execution rules (mandatory)` block for agentic models with five rules:
1. No simulation — never wrap commands in markdown expecting them to run
2. ReAct loop — think → tool → observation → next action
3. Verify after every write/patch — call `read_file` on the same path immediately after `write_file`/`patch_file`
4. Read before patch — read the target section first so `old_string` matches exactly
5. Report what you did, with paths — not what you planned

**Fix — simulation detector:** `_raise_if_unexecuted_tool_request()` now fires on agentic routes too (not just text-only). If tool-call markup (`<bash>`, `<tool_code>`, `<function_call>`, `antml:invoke`) appears in the final text *and* no CLI result envelope is present, the execution is failed with a Phase 7.2 simulation-gap error. CLI routes (Claude Code, Codex, Vibe) wrap real executions in a `{"type":"result",…}` envelope — when that envelope is present, the markers are treated as quoted content, not simulation.

**Fix — Scaleway read-back nudge:** `call_scaleway()` injects a `user` message after every `write_file`/`patch_file` tool result prompting the model to call `read_file` on the same path to verify the bytes on disk match its intent. This enforces rule #3 at the loop level, not just the prompt level.

**Files changed:**
- `agent_executor.py` — `_task_uses_direct_text_route()` keys off `file_access`; `_build_prompt()` adds agentic rules block; `_raise_if_unexecuted_tool_request()` covers agentic routes; `estimate_task_prompt_tokens()` mirrors the rules block
- `agent_router.py` — `call_scaleway()` post-write read-back nudge
- No DB migration, no new settings, no new endpoints

### Phase 7.2.1 — Containment + File-Read Verification (2026-07-20)

Extends 7.2 with two oversight checks that close the loop on file access: **(a) the project folder is a hard sandbox** and **(b) AIngel verifies that referenced files were actually read**, not just that they *could* be read.

**Containment invariant — project folder is the only scope.** AIngel now enforces this at three layers:

1. **Prompt mandate** (`agent_executor._build_prompt` rule 5): the agentic rules block tells the model "All file access — reads, writes, bash cwd — MUST stay inside the project folder. Never read, list, or write paths outside that folder (no `/home/other`, `/etc`, `/opt`, parent directories, or absolute paths outside the project). If a task seems to require external files, refuse and explain."
2. **H2 pre-run containment check** (`agent_overseer.pre_run_check`): any referenced path that resolves outside `realpath(project_path)` is classified as a containment breach — `gate=skip` is forced regardless of the model's opinion, and a `capability_warning` names the offending path. The check uses `realpath` so symlink traversal and `../` escapes are both caught.
3. **Existing route-level containment (unchanged but now explicit):**
   - Scaleway: `agent_tools._safe_join` rejects any path whose `realpath` escapes the project root.
   - Claude Code: `cwd=project_path`, no `--add-dir` (Claude scopes Read to cwd by default), `--permission-mode acceptEdits` + `.claude/settings.json` deny patterns.
   - Codex: `--cd project_path` + `--sandbox workspace-write` (writes contained; reads are not sandboxed by Codex so the prompt mandate + H2/H3 checks are the read-containment layer).
   - Vibe: `cwd=project_path` + `.vibe/config.toml`.

**H2 — agentic self-serve vs text-only block.** A referenced file not auto-inlined by the prompt builder used to be a hard "missing — model will not see content" regardless of route. Now:
- **Agentic routes** (`file_access` = `native`/`bash`/`tools`): a referenced file *inside the project* is classified as `self_serve` — the model can `read_file` it itself. Not a block; tracked so H3 can verify it was actually read. A referenced file *outside the project* is always a containment breach (`gate=skip`).
- **Text-only routes** (`file_access` missing/none): unchanged — "model will not see content" is a real block.

New H2 return field: `self_serve: [{name, exists, reason}]`. `missing_files` is now reserved for true blocks (outside-project paths or text-only-route gaps).

**H3 — actual-read verification.** `post_run_analysis` now receives `referenced_files`, `caught_files`, `self_serve_files`, and `route_agentic` from the executor. For each referenced file:
- If it was auto-inlined (`caught_files`) → verified.
- If the output text mentions the basename → assumed read (CLI routes echo paths; Scaleway tool-call logs go to `journalctl` but not the final text, so the heuristic is conservative).
- Else, for agentic routes, it goes into `unread_files`.

`unread_files` is surfaced deterministically: even if the AIngel model returns `severity=ok`, the executor-level heuristic promotes the review to `warning`, adds a finding listing the unread files, and sets `gate_for_next=hold`. So a model that skipped reading a referenced file cannot quietly pass review.

New H3 return field: `unread_files: [str]`.

**Files changed:**
- `agent_executor.py` — agentic rules block gains rules 5 (containment) + 6 (read every referenced file); `_generate_brief_async` accepts + forwards `referenced_files`/`caught_files`/`self_serve_files`/`route_agentic`; call site computes them
- `agent_overseer.py` — `pre_run_check` classifies missing vs self-serve by route + containment, returns `self_serve`, forces `gate=skip` on containment breaches; `post_run_analysis` accepts file lists, computes `unread_files`, surfaces them deterministically
- No DB migration, no new settings, no new endpoints

### Phase 7.3 — Oversight Call Isolation + Working-Docs Folder Fix (2026-07-27)

Three fixes from a single incident on task #465 (project 118, a hardware-setup project): H2 silently ran a full agentic session instead of returning a JSON brief, the task's own output was truncated well under its model's real cap, and H2 reported files as missing that actually existed on disk.

**H2/H3/H4 now force a plain API route.** `pre_run_check`, `post_run_analysis`, and `refresh_overview` called `route(aingel_model, prompt, ...)` with no mode override. `call_mistral()` defaults to `MISTRAL_MODE` (env var, default `'vibe'`), so any project with a Mistral `aingel_model` had its lightweight completeness/review calls silently upgraded to a full Mistral Vibe CLI session — real file/bash access, `cwd=project_path`. On task #465 this produced a 1,281-line document instead of the requested JSON brief, which then failed to parse and fell back to a permissive gate. Fixed by passing `force_anthropic_mode='api', force_mistral_mode='api'` on all three overseer `route()` calls — these calls are analysis, not task execution, and must never spawn an agentic CLI.

**Real task execution now honors the per-model output cap.** `agent_executor.py`'s non-batch `route()` call hardcoded the global `MAX_TOKENS` constant (8192) instead of the `max_tokens` value already computed from `model_caps.json` earlier in the same function (`built.get('max_tokens', MAX_TOKENS)` — 32768 for `oll-glm-5.2`, etc). Only the batch path used the correct variable. This silently truncated every non-batch agentic run at 8192 output tokens regardless of the model's real cap — on task #465 the model was cut off mid-turn before it ever wrote its deliverable. (Distinct from — and not fixed by — the `_OLL_MAX_TOOL_TURNS`/`_SCW_MAX_TOOL_TURNS` 20→40 raise from 2026-07-24, which addresses turn-count starvation, not per-turn token truncation.)

**`_WORKING_DOC_FOLDERS` was missing `'My Docs'`.** The Three-Layer Project Structure section (above) has documented `My Docs/` as the default folder name all along, but `agent_tools._WORKING_DOC_FOLDERS` only listed the accepted aliases (`Working Docs`, `Working Documents`, `working-docs`, `docs`) — never the default itself. This silently broke `prompt_builder`'s file auto-catch (and, after the fix above, H2's file-existence check) for every project using the documented default instead of an alias — 9 of 11 projects at time of fix. Added `'My Docs'` to the tuple in `agent_tools.py` (canonical) and its two import-fallback copies in `prompt_builder.py` and `agent_overseer.py`.

**Files changed:**
- `agent_overseer.py` — `pre_run_check`/`post_run_analysis`/`refresh_overview` force `force_anthropic_mode='api', force_mistral_mode='api'`; `_check_path_exists` now scans Working-Docs folders (not just project root) via `agent_tools._WORKING_DOC_FOLDERS`, matching `prompt_builder`'s scan scope
- `agent_executor.py` — non-batch `route()` call uses the computed `max_tokens` instead of the hardcoded `MAX_TOKENS` constant
- `agent_tools.py` — `_WORKING_DOC_FOLDERS` gains `'My Docs'`
- `prompt_builder.py` — import-fallback tuple updated to match
- No DB migration, no new settings, no new endpoints

## Git-Validated Execution

Per-task branches (`task/<id>-<slug>`) for software projects. `projects.git_enabled`: NULL = auto, 1 = on, 0 = off. Auto-detect via `agent_git.looks_like_software()`. When `execution_type = 'software'`, git is forced on regardless of the `git_enabled` column.

Run lifecycle: start branch → AI edits on branch → `commit_task()` auto-commits diff → Approve merges (`git merge --no-ff`), Reject discards, Revert does `git revert`.

**Boundaries:** merge ≠ promote to live. One task at a time per project. `Artifacts/` is gitignored (chat logs dirty the tree otherwise).

Dep picker: `⬡ Deps` button in Add Task modal opens a full-screen overlay (z-index 4500). Deps saved via `POST /api/tasks/<id>/dependencies` after task is created. `_addTaskSubmitting` flag prevents duplicate submissions.
