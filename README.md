<p align="center">
  <img src="docs/brand/banner.png" alt="Cordée: AI-guided project management with a guide on the rope" width="100%">
</p>

<p align="center">
  <b>Every AI run is checked before it starts and reviewed when it finishes.</b><br>
  Project management for consultants and small teams who work with AI and can't afford to trust it blindly.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-AGPL--3.0-c67139?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/python-3.11+-7a8a5e?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/react-19-7a8a5e?style=flat-square" alt="React 19">
  <img src="https://img.shields.io/badge/data%20residency-EU-56633f?style=flat-square" alt="EU data residency">
</p>

---

## Why Cordée

A *cordée* is a rope team: climbers tied together behind a guide who checks every anchor before anyone moves. Cordée applies that idea to AI work.

- **A Guide on every task.** Before a run, the Guide checks that the brief is complete and the inputs exist. After the run, it reviews the output against the brief. It also keeps a running overview of the whole project. Problems get flagged before they reach your client.
- **The right model for each project.** Claude, Mistral, OpenAI, Scaleway or Ollama, chosen per project and per task, not once per installation.
- **EU-only, enforced.** Mark a project `eu_only` and only EU-operated models (Scaleway, Mistral) can run it. The API rejects anything else, so the rule can't be bypassed from the UI.
- **Nothing gets promoted without you.** Phase memory moves into project memory only after you approve it.
- **Spend you can see.** Every run is logged with its tokens and cost. Monthly budgets pause work before you overspend.

![Board](docs/screenshots/task-board.png)

## Features

| | |
|---|---|
| **Task board** | Kanban board across phases: pending → confirmed → running → done / failed. AI-assisted drafting, scoping and dependency mapping. |
| **Guide** | Pre-run completeness check, post-run review and a rolling project overview. Runs can be held, retried or reworked. |
| **Execution log** | Every run with status, model, tokens, cost and full output. |
| **Chat** | Context-aware conversation grounded in project and phase memory. |
| **Memory** | Project memory plus per-phase memory, promoted only with your approval. |
| **Execution types** | `standard`, `software` (Git forced on), `research` (full memory), `deployment` (planned). |
| **Files & WebDAV** | Built-in file manager, and project folders can be mounted over WebDAV. |
| **Multi-user** | OIDC login, roles per project (viewer, member, admin, owner) and free-tier quotas. |

<table>
  <tr>
    <td><img src="docs/screenshots/execution-log.png" alt="Execution log"></td>
    <td><img src="docs/screenshots/chat-panel.png" alt="Chat"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/memory-panel.png" alt="Memory"></td>
    <td><img src="docs/screenshots/settings-modal.png" alt="Project settings"></td>
  </tr>
</table>

## Quick start

**Prerequisites:** Python 3.11+, Node.js 18+, Git

```bash
git clone https://github.com/cordee-app/cordee.git && cd cordee
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
(cd frontend && npm install && npm run build)
cp .env.example .env    # add your provider keys
python3 agent_api.py    # → http://localhost:8001
```

The database is created automatically on first launch.

**Provider CLIs** (used by the default routing modes):
- Anthropic (`ANTHROPIC_MODE=claude-code`): `npm i -g @anthropic-ai/claude-code`
- Mistral (`MISTRAL_MODE=vibe`): install the Vibe CLI, then run `vibe` to log in

**Key environment variables**

```env
AINGEL_PROJECTS_ROOT=/path/to/projects
SCALEWAY_API_KEY=...        # required for EU-only projects
ANTHROPIC_API_KEY=...
MISTRAL_VIBE_KEY=...
```

For systemd, the sandbox and the full variable reference, see [SETUP.md](SETUP.md). To have an AI assistant install and verify everything for you, paste [docs/INSTALL_PROMPT.md](docs/INSTALL_PROMPT.md) into Claude Code, Vibe or Codex.

## Architecture

```mermaid
flowchart LR
  UI["React 19 + Vite<br/>board · guide · log · chat"] -->|REST / SSE| API["agent_api.py<br/>Python"]
  API --> G{{"Guide<br/>pre-run check · post-run review · overview"}}
  G --> R["Router<br/>per-project model + EU guard"]
  R --> C["Claude"] & M["Mistral"] & S["Scaleway (EU)"] & O["OpenAI"] & L["Ollama"]
  API --> DB[("central DB<br/>projects · users · registry")]
  API --> PDB[("project DB (one per project)<br/>tasks · runs · chats · memory")]
  API --> FS["project files<br/>Git · WebDAV"]
```

- **Two databases:** a central DB holds projects, users and the registry. Each project has its own DB with tasks, runs, chats and skills.
- **EU guard:** task creation, model changes and the model recommender all reject non-EU models on `eu_only` projects.
- **Guide hooks:** H2 (pre-run check), H3 (post-run review) and H4 (overview) wrap every execution.

See [READMEFIRST.md](READMEFIRST.md) for the full design.

## Roadmap

- [x] Guide pre-run check, post-run review and overview
- [x] EU-only projects with server-side enforcement
- [x] OIDC login, project roles, free-tier quotas
- [x] WebDAV project mounts
- [ ] Local llama.cpp provider for offline work
- [ ] `deployment` execution type (Scaleway batch pipelines)
- [ ] RAG over project corpora
- [ ] Hosted EU instance

## Development

```bash
python3 -m unittest discover -s tests -p "test_*.py"
cd frontend && npm run test          # Playwright
```

## License

Cordée is dual-licensed:

- **[AGPL-3.0](LICENSE)**: free to use, modify and self-host. If you offer a modified version to users over a network, you must share its source.
- **Commercial license**: no copyleft obligations, with support available. See [docs/COMMERCIAL.md](docs/COMMERCIAL.md).

Contributions are accepted under the [CLA](CLA.md). See [NOTICE](NOTICE) for copyright and third-party notices.

---

<p align="center"><sub>Built for people who work with AI and stay accountable for the result.</sub></p>
