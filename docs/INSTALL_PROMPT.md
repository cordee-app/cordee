# AI-Assisted Install — the one-paste bootstrap prompt

Paste the block below into any agentic AI that can run shell commands on your
machine (Claude CLI, Vibe, Codex CLI, …). The AI will probe your system, ask you
two questions, install Cordée, and verify it end-to-end with a real task run.

## Getting the prompt

| Situation | How to get it |
|-----------|---------------|
| Download | `curl -s https://raw.githubusercontent.com/cordee-app/cordee/main/docs/INSTALL_PROMPT.md` |
| Already cloned | Copy the block below from this file |

| Stage | What the AI does |
|-------|------------------|
| Probe | Detects OS, WSL, Python/Node/Git, existing CLIs, free port — silently |
| Ask   | Asks once: install profile + which API keys you have |
| Install | Clones the repo, builds the frontend, writes `.env`, installs only what your profile needs |
| Verify | Starts the server, checks health, guides one cheap test task |

> Nothing is invented: the AI follows the decision tree below and defers
> provider-specific details (WebDAV tokens, Mistral Vibe auth) to the docs it
> clones. It never asks for secrets in chat beyond what you choose to paste.

*Last verified: 2026-09-08 — live run on a fresh Windows+WSL laptop (install →
verify → first task executed).*

---

## The prompt (copy everything in the block below)

````markdown
You are installing Cordée — an AI-agnostic project orchestrator (Flask backend,
React frontend, SQLite) — on this machine, adapting every step to what you find.

Repo: https://github.com/cordee-app/cordee.git — clone the DEFAULT branch.
App port: 8001. Health endpoint: GET /api/config (returns JSON with instance name).

RULES OF ENGAGEMENT
- Run shell commands yourself, show their output, and narrate briefly what you
  are doing. If you CANNOT run commands (e.g. you are a web chat), switch to
  step-by-step guided mode: emit each command for the user to run and interpret
  the pasted results.
- Never fabricate API keys. Never echo key values back. Ask the user to paste
  keys only where you write them into `.env`.
- Pause and ask before anything destructive or privileged (sudo, installing
  system packages, killing processes on port 8001).

HARD INVARIANTS — never violate these
1. WINDOWS: the backend MUST run inside WSL2 Ubuntu. Do NOT attempt a native
   Windows install (untested; agentic CLI routes break on Windows' ~32 KiB
   argv limit). macOS/Linux: native install is correct.
2. AINGEL_PROJECTS_ROOT must be a real filesystem path on the machine that runs
   the backend — NEVER a /mnt/* drive mount, network share, or WebDAV mount
   (SQLite over such mounts corrupts databases).
3. Requirements: Python >= 3.11, Node >= 18, Git. Use a Python venv — never
   pip-install into the system interpreter.
4. Install provider CLIs ONLY for providers whose keys the user actually has.
5. On Windows there are TWO contexts: backend + CLIs live inside WSL; the
   optional rclone drive mount is native Windows (WinFsp). Never mix them.

PHASE A — PROBE (run silently, no questions yet)
- OS and version (uname / ver). If Windows: `wsl -l -v` — is a distro
  installed? Does `wsl` exist at all?
- Inside the Linux context (WSL if Windows, native otherwise):
  python3 --version, node --version, npm --version, git --version.
- Which CLIs already exist: claude, vibe, codex, rclone.
- Is port 8001 already listening? If yes, ask before touching it.
- If a directory `cordee` already exists in the intended install location,
  report it and ask whether to reuse or clone fresh.

PHASE B — ASK THE USER (once, all questions together)
1. Profile — which of these do you want?
   a) Core local: Cordée runs on this machine, projects on local disk.
   b) + Online: same, plus Scaleway buckets as the cloud mirror for project
      files (requires a Scaleway account + API key).
   c) + Server mount: additionally mount the project files of an existing
      Cordée server as a drive letter / directory (requires that server's
      WebDAV URL + token from its operator).
2. Which API keys do you have today? Anthropic / Mistral (Vibe + Org) /
   Scaleway / OpenAI (Codex login) / Ollama / none yet.

PHASE C — INSTALL (follow the tree)

C1. Windows without WSL:
    - Explain in one sentence why WSL2 is required (see invariants).
    - Run `wsl --install -d Ubuntu` (elevated). WARN: one reboot is required.
    - After reboot, verify `wsl -l -v` shows Ubuntu, create the user, then
      continue INSIDE WSL with C2. If virtualization is disabled in BIOS/UEFI
      or install fails repeatedly, STOP and report — do not improvise a
      native Windows fallback.

C2. Common path (inside WSL on Windows; natively on Linux/macOS):
    - git clone https://github.com/cordee-app/cordee.git && cd cordee
    - python3 -m venv venv && source venv/bin/activate
    - pip install -r requirements.txt
    - cd frontend && npm install && npx tsc -b --noEmit && npm run build && cd ..
    - cp .env.example .env
    - Set AINGEL_PROJECTS_ROOT to a real Linux path (e.g. ~/Projects), mkdir -p it.
    - Write ONLY the key lines matching Phase B answers. Every variable exists
      in .env.example (AINGEL_PROJECTS_ROOT commented out: uncomment it);
      edit the lines in place:
        Anthropic key  -> ANTHROPIC_API_KEY (keep ANTHROPIC_MODE=claude-code)
        Mistral keys   -> MISTRAL_VIBE_KEY, MISTRAL_ORG_KEY (keep MISTRAL_MODE=vibe)
        Scaleway key   -> SCALEWAY_API_KEY
                         Best practice: create the key inside a dedicated
                         Scaleway Project (e.g. "Cordee-<host>"), not the
                         default project — lets you tighten IAM scope later
                         without regenerating org-wide keys.
        OpenAI/Codex   -> none in .env; Codex CLI authenticates via `codex login`
    - CLIs, only for providers with keys:
        Anthropic: npm install -g @anthropic-ai/claude-code
        Mistral:   install the Vibe CLI per Mistral's docs, then run `vibe`
                   once. Two auth paths: (a) OAuth browser login — no key in
                   .env needed for the CLI route; (b) API key — write it to
                   MISTRAL_VIBE_KEY (keeps direct-API features on). Say which
                   one you set up. If Vibe is unavailable for this OS, set
                   MISTRAL_MODE=api (text-only, loses tool use) and say so.
        Codex:     install per OpenAI's docs, then `codex login`.

C3. Profile extras:
    b) Online: confirm the Scaleway key via GET /api/config/readiness
       (it reports `scaleway: true/false` — /api/models lists all models
       regardless of key validity). Do NOT set up an rclone bisync of project
       files against another server (it can resurrect deleted files); a live
       mount as in (c) is the supported file path.
    c) Server mount: this is NATIVE-WINDOWS rclone + WinFsp (or native rclone on
       macOS/Linux). Follow docs/WEBDAV_SETUP.md in the cloned repo exactly —
       it needs the DAV token from the server's operator. Do not invent tokens.

PHASE D — VERIFY (all gates must pass before declaring success)
1. python3 agent_api.py  (leave running; open a second terminal for checks)
2. GET /api/config returns JSON containing an instance name.
3. GET /api/models returns a non-empty model list.
4. Open http://localhost:8001 in a browser (on Windows, the Windows browser
   reaches the WSL server via localhost — WSL2 forwards it).
5. Guide the user to create a throwaway project (any type) and ONE task, then
   run it on the cheapest model for the provider keys they have:
     Scaleway: scw-mistral-small-24b | Mistral: mistral-small-latest |
     Anthropic: claude-haiku-4-5-20251001. With no provider key, skip this gate
     and say clearly that no task can run until at least one key exists.
6. Confirm the execution completes (Execution Log in the UI, or GET /api/executions).
7. Offer to delete the throwaway project. Print a final summary: what was
   installed where, which providers are active, and the exact command to start
   Cordée next time (python3 agent_api.py from the repo directory, inside WSL
   on Windows).

FAILURE PLAYBOOK
- Port 8001 busy -> find the listener, ask the user, stop it or set another port.
- python3 < 3.11 (e.g. Ubuntu 22.04 ships 3.10) -> install a newer interpreter
  (Ubuntu 24.04+ ships 3.12; otherwise `sudo add-apt-repository ppa:deadsnakes/ppa`
  && `sudo apt install python3.11 python3.11-venv`), then recreate the venv with
  that version. Report which you chose.
- `python3 -m venv` fails mentioning ensurepip/venv -> `sudo apt install
  python3-venv` (or `python3.11-venv`), then retry.
- frontend build fails with TypeScript errors -> Node too old; require >= 18, retry.
- CLI "command not found" right after install -> new shell needed; restart the
  shell and re-check before declaring failure.
- WSL install loops or fails -> virtualization disabled in firmware or policy
  blocked; stop and report, do not fall back to native Windows.
- Task run fails with `"<KEY>_API_KEY not set in .env"` in the result (the API
  returns HTTP 200 with `status: 'failed'`) -> the matching provider key is
  missing/invalid; show which variable to fix (never print the key).
- Mistral run truncates mid-output -> MISTRAL_MODE=api caps output; this is a
  known limitation, suggest a Scaleway or Anthropic model for heavy tasks.
- Anything else: show the failing command's full output and ask before retrying.

Finally: point the user to README.md (what Cordée does) and SETUP.md (systemd,
environment reference) in the cloned repo, and remind them .env contains
secrets and must never be committed or shared.

RAG NOTE (set expectations): /api/config may show "rag_corpora" — those are
host-local RAG libraries that exist only on a server prepared for them
(AINGEL_INSTANCE_NAME=Vault). On a normal install the list is empty; RAG
search is not part of a standard install. Do not try to configure it.
````

---

## Notes

* The prompt is intentionally self-sufficient for the core path; provider-token
  specifics (WebDAV token, Vibe CLI auth) stay in their own
  docs (`docs/WEBDAV_SETUP.md`, `SETUP.md`) so they never
  drift into two copies.
* After editing the prompt block, bump the *Last verified* line above and
  paste-test the block once in a fresh AI session.