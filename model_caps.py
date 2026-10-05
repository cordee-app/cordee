"""Utility to expose per-model capabilities for SuperAgent.

The JSON file ``model_caps.json`` (placed alongside this module) contains the
following keys for each model identifier:

* ``max_tokens``     – maximum **output** tokens per call (the API ``max_tokens``
  parameter). e.g. Scaleway serverless models cap output at 32768.
* ``context_window`` – total input+output window in tokens. Used to decide
  whether a prompt fits one synchronous call or must be chunked.
* ``tools``          – whether the model supports function-calling / tool usage.
* ``reasoning``      – whether the model emits internal reasoning that is billed
  against ``max_tokens`` *before* any answer is produced. See below.

The ``get_cap`` function returns a dict with those fields for a given model id,
falling back to a sensible default if the model is unknown.

Why ``reasoning`` exists
------------------------
Every field above describes what a model *costs* or *can do*. None described how
it *behaves*, and that gap had teeth: Autopilot H2 and H3 ask for JSON with an
800/900-token budget, and on ``scw-qwen3.6-35b`` the reasoning consumed the whole
budget before a single character of answer — ``content: None``,
``finish_reason: length``. The hooks silently produced nothing while every
external signal, the EU audit log included, reported success.

Measured on Scaleway 2026-08-03, same prompt, 12 samples per budget:

===============  ==================  ==========================
``max_tokens``   empty responses     completion_tokens (median)
===============  ==================  ==========================
800              **9 / 12**          800  (truncated)
1600             0 / 12              818
3200             0 / 12              946
===============  ==================  ==========================

Two things follow, and both shaped the design:

1. The failure is **probabilistic**, not deterministic — 3 of 12 calls at 800
   succeeded. A single sample per budget proves nothing, so a static
   ``min_output_tokens`` per model cannot be measured cheaply or trusted.
2. The model needed ~1 200 tokens; it is not "filling the budget". A generous
   floor plus a retry on truncation is therefore both cheaper and more robust
   than trying to pin an exact number per model — and it covers models not yet
   in this file at all.

So ``reasoning`` is a **boolean**, deliberately: it is read directly off the
presence of a ``reasoning`` field in the API response and is unaffected by
sampling luck, unlike a token floor. Callers use it to pick a starting budget;
``agent_overseer`` then retries on a truncated reply regardless of the flag.
"""
import json
import os
from typing import Dict, Any

# Path is resolved relative to this file – works both in LIVE and SANDBOX.
_CAPS_PATH = os.path.join(os.path.dirname(__file__), "model_caps.json")

def _load_caps() -> Dict[str, Dict[str, Any]]:
    """Load the JSON caps file once; fallback to empty dict on error."""
    try:
        with open(_CAPS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # If the file is missing or malformed we still want the importer to
        # continue; callers will receive the default values.
        return {}

# Cache the caps at import time – the file is tiny and never changes at runtime.
_CAPS = _load_caps()

def get_cap(model_id: str) -> Dict[str, Any]:
    """Return a capability dict for ``model_id``.

    Missing keys are filled with conservative defaults:
        max_tokens:     8192
        context_window: 128000
        tools:          False
        reasoning:      False
    """
    default = {
        "max_tokens": 8192,
        "context_window": 128000,
        "tools": False,
        "reasoning": False,
    }
    merged = {**default, **_CAPS.get(model_id, {})}

    # Dedicated GPU deployments (scw-dep-*) serve an arbitrary model whose real
    # context window (e.g. a 4096-token custom fine-tune) must override the 128k
    # default, otherwise prompt/chunk/max_tokens sizing overflows the endpoint
    # and vLLM answers 400. The context is recorded on the deployment row when
    # the window is opened.
    if isinstance(model_id, str) and model_id.startswith('scw-dep-'):
        try:
            import agent_db
            conn = agent_db.get_db()
            row = conn.execute(
                'SELECT max_context_size FROM scw_deployments WHERE scw_deployment_id=?',
                (model_id[len('scw-dep-'):],)).fetchone()
            conn.close()
            ctx = (row['max_context_size'] if row else None) or None
            if ctx:
                merged['context_window'] = int(ctx)
                # Leave room for the input: output budget is capped well under
                # the window, and never exceeds the 8192 default ceiling.
                merged['max_tokens'] = max(512, min(8192, int(ctx) - 1024))
        except Exception:
            pass
    return merged


# Smallest output budget that reliably leaves room for an answer after a
# reasoning model has finished thinking. Derived from the measurements in the
# module docstring: 1600 was clean 12/12 where 800 failed 9/12, and observed
# completion_tokens peaked at ~1170.
REASONING_MIN_OUTPUT_TOKENS = 2048


def effective_max_tokens(model_id: str, requested: int) -> int:
    """Raise a too-small output budget to something a reasoning model can answer in.

    Callers that ask for a short structured reply (the Autopilot hooks want a few
    hundred tokens of JSON) are budgeting for the *answer* and have no idea the
    model will spend the allowance thinking first. Never lowers the request, and
    never exceeds what the model actually accepts.
    """
    cap = get_cap(model_id)
    want = int(requested or 0)
    if cap.get("reasoning"):
        want = max(want, REASONING_MIN_OUTPUT_TOKENS)
    ceiling = int(cap.get("max_tokens") or 0)
    return min(want, ceiling) if ceiling else want


# Alias used throughout the codebase.
model_capabilities = get_cap
