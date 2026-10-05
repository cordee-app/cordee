# Security Policy

## Why this matters

Cordée's defining feature is **EU data sovereignty** — the guarantee that confidential projects never send data to non-European servers. Security is therefore not a side concern; it is the core promise of the product. This document explains how to report vulnerabilities and the practices we follow to keep that promise.

---

## Supported Versions

Cordée is under active development and has not yet been versioned. The `dev/aingel-productisation` branch is the working branch; `main` is intended for stable releases.

| Branch | Supported |
|---------|-----------|
| `dev/aingel-productisation` | :white_check_mark: |
| `main` | :white_check_mark: |
| `< 0.1` | :x: |

---

## Reporting a Vulnerability

If you discover a security vulnerability in Cordée — **especially one that could breach the EU data-residency guarantee** — please report it to us as soon as possible. We take security issues seriously and will respond promptly to fix verified vulnerabilities.

### How to Report
1. **Email**: Send details to [security@cordee-app.io](mailto:security@cordee-app.io), or use GitHub's private vulnerability reporting on the repository.
2. **Subject**: Include `[SECURITY]` in the subject line.
3. **Details**: Provide a clear description of the vulnerability, including:
   - Steps to reproduce.
   - Affected versions / branches.
   - Potential impact (in particular: could it leak data to a non-EU model or server?).
   - Any suggested fixes.

### What to Expect
- **Acknowledgment**: We will acknowledge your report within 48 hours.
- **Investigation**: We will investigate the issue and may contact you for further details.
- **Fix**: If the vulnerability is verified, we will work on a fix and release it as soon as possible.
- **Disclosure**: We will coordinate with you to disclose the vulnerability responsibly.

---

## Security Best Practices

### For Users
- Keep your Cordée installation up to date with the latest version.
- For confidential projects, mark them `eu_only` and run Cordée on Scaleway's EU infrastructure.
- Regularly back up your `aingel.db` and `project.db` files (and store the backups securely).
- Use environment variables for all API keys — never hardcode them in your project files.

### For Developers
- Follow secure coding practices (input validation, sanitization, no string-built SQL).
- Never hardcode secrets in the codebase — use `.env` (already gitignored).
- **Never weaken the EU guard** (`_eu_guard()`, `_is_eu_model()`). If you must touch it, document why and flag it for review.
- **No telemetry.** Do not add analytics, error reporting, or usage tracking that sends data outside the user's own infrastructure.
- Regularly audit dependencies for vulnerabilities.

---

## Known Security Considerations
- **EU-only guard**: `eu_only` projects are restricted to Scaleway (`scw-*`) and Mistral models at the API boundary. The guard covers task creation, model changes, and the model recommender. Any change to this surface area is security-sensitive.
- **Database files**: `aingel.db` and `project.db` contain project data. Ensure they are stored securely and backed up regularly. Never copy or overwrite them on another machine.
- **Git integration**: Tasks in `software` execution type use git branches. Ensure your repository is private if working with sensitive code. `.gitignore` excludes `.env`, `*.db`, and `Artifacts/` from commits.
- **`research` execution type**: Injects all phase memories into the prompt. Avoid using this mode for sensitive data on non-EU models — or simply mark the project `eu_only` so non-EU models cannot be selected in the first place.
- **Scaleway session storage**: Uses KMS-encrypted buckets with crypto-shredding on close. Ensure your Scaleway credentials (`SCW_ACCESS_KEY`, `SCW_SECRET_KEY`) are stored securely.