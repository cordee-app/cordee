# Contributing to Cordée

Cordée is an **AI-guided project management system with EU data sovereignty**. We welcome contributions that advance that mission — whether bug fixes, new features, documentation, or feedback.

---

## 📌 Code of Conduct
By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md). Please read it to understand the expectations for behavior in our community.

---

## 🚀 How to Contribute

### 1. Reporting Issues
If you encounter a bug or have a feature request, please open an issue on GitHub. Include the following details:
- A clear and descriptive title.
- Steps to reproduce the issue (if applicable).
- Expected and actual behavior.
- Screenshots or logs (if relevant).
- Your environment (OS, Python version, model provider used).

### 2. Setting Up the Development Environment
Follow the [Getting Started](README.md#getting-started) guide in the `README.md` to set up the project locally.

### 3. Making Changes
1. **Fork the repository** and create a new branch for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** and ensure they follow the project's coding style.

3. **Test your changes** locally:
   ```bash
   python3 -m unittest test_agent_router.py -v
   ```

4. **Commit your changes** with a descriptive message:
   ```bash
   git commit -m "feat: add your feature description"
   ```

5. **Push your changes** to your fork:
   ```bash
   git push origin feature/your-feature-name
   ```

6. **Open a Pull Request (PR)** on GitHub. Include:
   - A clear title and description of your changes.
   - Reference any related issues (e.g., "Fixes #123").
   - Screenshots or GIFs for UI changes.

---

## 📝 Coding Style

### Backend (Python)
- Follow [PEP 8](https://peps.python.org/pep-0008/) guidelines.
- Use type hints for function signatures.
- Write docstrings for all public functions and classes.
- Keep functions small and focused.

### Frontend (React/TypeScript)
- Use TypeScript for type safety.
- Keep components small and reusable.
- Write tests for new components.

### Git Commits
- Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for commit messages:
  - `feat:` for new features.
  - `fix:` for bug fixes.
  - `docs:` for documentation changes.
  - `style:` for formatting changes.
  - `refactor:` for code refactoring.
  - `test:` for test-related changes.

---

## 🇪🇺 EU Compliance — Important for Contributors
Cordée's defining feature is **EU data sovereignty**. When contributing, keep these invariants in mind:

- **Never weaken the EU guard.** `eu_guard()` and `is_eu_model()` in `agent_config.py` are the single source of truth for which models `eu_only` projects may use (`agent_api._is_eu_model` is a thin alias). Do not add bypasses or feature-flags that circumvent them.
- **EU-only models are Scaleway (`scw-*`) and Mistral.** If you add a new provider, it must be explicitly classified as EU or non-EU.
- **Scaleway session storage uses KMS-encrypted buckets.** Do not introduce plaintext fallbacks for EU-only projects.
- **No telemetry.** Cordée does not phone home. Do not add analytics, error reporting, or usage tracking that sends data to any server outside the user's own infrastructure.

If your change touches anything in the EU-compliance path, mention it explicitly in your PR description so it gets careful review.

---

## ⚖️ Licensing & CLA
Cordée is licensed under [AGPL-3.0](LICENSE) and is also offered under a
[commercial license](docs/COMMERCIAL.md). Before your first pull request can
be merged, you need to accept the [Contributor License Agreement](CLA.md).
On your first PR, the CLA Assistant bot posts a comment with a link to the
CLA. To accept, reply on the PR with exactly:

> I have read the CLA Document and I hereby sign the CLA

You only sign once; it then covers all your future contributions. If the
check doesn't update, comment `recheck`.
Under the CLA you keep the copyright in your contribution. You license it
under AGPL-3.0 and grant the maintainer the right to include it in commercial
licenses.

Add yourself to [CONTRIBUTORS.md](CONTRIBUTORS.md) in your first PR.

---

## 🔍 Review Process
1. Maintainers will review your PR and may request changes.
2. Address feedback by making additional commits to your branch.
3. Once approved, your PR will be merged into `main`.

---

## 🎯 Areas for Contribution
Here are some ways you can contribute:

### 1. Bug Fixes
- Check the [Issues](https://github.com/cordee-app/cordee/issues) tab for open bugs.

### 2. New Features
- Propose new features by opening an issue first to discuss feasibility.
- Focus on execution types, memory management, project lifecycle, or UI improvements.

### 3. Documentation
- Improve existing documentation (e.g., `README.md`, `ARCHITECTURE.md`).
- Add tutorials or examples for advanced use cases.

### 4. Testing
- Write unit tests for untested code.
- Improve test coverage for critical components.

### 5. UI/UX
- Enhance the React frontend (e.g., dark mode, responsive design).
- Improve accessibility (e.g., keyboard navigation, screen reader support).

---

## 📬 Questions?
If you have questions or need help, open an issue or reach out to the maintainers.

Thank you for contributing to Cordée! 🚀