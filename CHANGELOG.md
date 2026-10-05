# Changelog

All notable changes to this project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]
### Changed
- Rebranded AIngel → Cordée (UI, docs, theme). Internal identifiers unchanged.
- Relicensed under AGPL-3.0 with a commercial-licence option and a contributor licence agreement (`CLA.md`).

### Added
- Initial public release of Cordée — an AI-guided project management system with EU data sovereignty.
- **Plan, run, and close projects** with AI assistance at every stage (task drafting, execution, review, memory).
- **Per-project LLM selection** — run different projects on different models (Claude, Mistral, Scaleway-hosted open models) depending on scope, budget, and sensitivity.
- **EU-only project guard** (`eu_only` flag): hard-enforced at the API boundary — non-EU models (Claude/US, OpenAI/US, Ollama Cloud/US) are excluded entirely for `eu_only` projects.
- **Scaleway integration** for EU-hosted inference and KMS-encrypted session storage with crypto-shredding on close.
- Two-DB architecture (`aingel.db` central + `project.db` per-project) with registry tables for global ID resolution.
- Execution types: `standard`, `software`, `research`, and `deployment`.
- Git-validated execution for code changes (per-task branches, approve-to-merge, revert support).
- Memory system: project memory + phase memory, with a user-gated accumulation flow (approve / reject / skip / fork).
- Kanban-style Task Board, AI-powered chat interface, execution log, and per-project settings modal.
- Guide Autopilot: pre-run completeness check (H2), post-run analysis (H3), rolling project overview (H4).

### Changed
- Migrated from a single `aingel.db` to a two-DB architecture (Phase 2).
- **Docs — deployment documented generically**: `SETUP.md` covers install, every environment variable and running as a service; systemd templates, a WAL-safe backup script and a restore runbook ship in `ops/examples/`.
- **Security hardening (audit P0–P2)**:
  - `PATCH /api/chats/<id>` and `PATCH /api/tasks/<id>` now accept only an allow-list of client-editable fields — `file_path` / `project_path` can no longer be set by a client (arbitrary file read/append/delete and cross-project writes).
  - Cross-tenant id resolution: every `project_id` / `task_id` / `exec_id` / `chat_id` in a request is resolved and a mismatch is rejected (403); `/api/execute` requires a `task_id` and no longer falls back to "first confirmed task anywhere".
  - WebDAV: removed the `X-Forwarded-For` loopback bypass, constant-time token comparison, standalone unit runs as an unprivileged user (not root); the app binds `127.0.0.1` (no longer `0.0.0.0`).
  - Quota metering always records actual usage (the limit is enforced only in the pre-flight check); free-tier model allow-list fails closed.
  - Task runs are serialized per project with an atomic pending/confirmed→running claim; every run stops its heartbeat and leaves a terminal execution state via `try/finally`.
  - Budget spend is recorded centrally in `route()` (covers failed/brief/overseer/batch calls); fixed the `budget_monthly` vs `monthly_budget` key mismatch in the overseer; added a pre-run budget gate.
  - Agent CLI subprocesses (claude/vibe/codex, Bash tool) get a scrubbed environment; `web_fetch` blocks loopback/private targets and validates redirects.
  - `/files/<subpath>` and `/files/<pid>/<path>` force active content (HTML/SVG/XML/JS) to download with `nosniff` + a sandboxing CSP (PDFs stay inline).
  - WebDAV has no loopback bypass at all (agent tools run on the host) and fails closed when `AINGEL_DAV_TOKEN` is unset.
  - The project lock is taken before the task is claimed, and the orphan sweep resets tasks stuck `running` with no running execution.
  - Waitress pool sized for long-lived SSE connections (`AINGEL_WAITRESS_THREADS`, default 64).
  - Project-DB schema migrations apply each ALTER independently with logging and run once per path per process; the v1→v2 central rebuild now keeps every current column, index and dependent row while dropping the stale FK.
  - Off-host DB backup hook (rclone; the backup unit now loads `.env`) + restore runbook; central snapshots kept outside the code checkout (mode 700/600).
  - CI workflow (`.github/workflows/ci.yml`) running the unittest suite and the frontend typecheck.
- **Removed the legacy `/old/` dashboard** (`agent_dashboard.{html,css,js}`) — duplicated the React UI, untested, and injectable.

### Fixed
- Orphaned executions are marked as `failed` on startup.
- Memory injection for `research` execution type.