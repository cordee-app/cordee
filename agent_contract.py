"""Task-contract checker — deterministic must-haves, no LLM.

This module is the seed of the task-contract checker planned as TODO item 15
Phase 3.3 (Plans/Cordee_Action_Plan_2026-10-14.md row 3.3; pipeline review
lines 210/219/252). The dependencies-done rule shipped first as a standalone
feature after the RP-096 incident (2026-10-17: a task created with a pending
dependency was silently skipped by the slot scheduler twice — no execution
row, no notification — while every other run path would have run it anyway).
The remaining must-have rules (output targets, capabilities, the
contract_json schema) will be added here later — Phase 3 extends check(),
it never replaces it.
"""

import agent_db as db


# The four fields every unmet-dep entry carries across the frozen API
# contract — run_task refusal, deps-endpoint 409, SSE event, PATCH warning,
# H2 dep_status. Trimmed here so a full task row (description, cost, …)
# can never leak into an API payload.
_UNMET_FIELDS = ('id', 'display_code', 'title', 'status')


def check(task, project_path=None):
    """Must-have checks for a task that is about to run. Returns
    ``{'ok': bool, 'unmet': [...]}`` — ``ok`` is False iff at least one
    must-have is violated, ``unmet`` lists the offending dependencies (one
    entry per dep, trimmed to ``_UNMET_FIELDS``).

    Current rule — dependencies done: a task may not run while any
    dependency's status is not 'done'. "Green" is status == 'done',
    mode-agnostic: 'skip', 'failed', 'paused' and the future
    'awaiting_answer' are all not green, because the rule is != 'done'
    (user decision, 2026-10-17). Deterministic on purpose — no LLM decides
    it (precedent: agent_api._run_seq's pre-skip).

    ``project_path`` is accepted for the later contract rules that need the
    filesystem (output-target existence); the deps-done rule already
    resolves cross-project dependencies through the central task_registry.

    Fail-open by design: ANY internal error → ``{'ok': True, 'unmet': []}``.
    A dependency whose project folder was deleted raises
    sqlite3.OperationalError inside agent_db.get_dependencies, and that
    must never hard-block run_task on a task with no other problem.
    """
    try:
        deps = db.get_dependencies(task['id']) if task else []
        unmet = [d for d in (deps or []) if d.get('status') != 'done']
        return {'ok': not unmet,
                'unmet': [{k: d.get(k) for k in _UNMET_FIELDS} for d in unmet]}
    except Exception:
        return {'ok': True, 'unmet': []}
