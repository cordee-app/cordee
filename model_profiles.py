"""Counselor knowledge base — model tiers, strengths and task-type detection.

The subjective, drift-prone inputs to the model recommender (which model is
"frontier", what each model is good at, and the keywords that identify a task
type) live in ``model_profiles.json`` alongside this module so they can be
refreshed without touching code. Objective capabilities (context window, vision,
tools, cost, EU/provider) are NOT stored here — they are read live from
``agent_config.MODELS`` and ``model_caps.json`` by the recommender.

Design goal: stay current with minimal maintenance. A model that is registered
in ``agent_config.MODELS`` but absent from the JSON's ``models`` map still gets
ranked, via ``provider_defaults`` and then a neutral fallback. See the JSON
``_meta`` block for the review cadence.
"""
import json
import os
from typing import Dict, Any, List

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_profiles.json")


def _load() -> Dict[str, Any]:
    """Load the JSON knowledge base once; fall back to empty on error so the
    importer never crashes (callers then use built-in defaults)."""
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_DATA = _load()

META: Dict[str, Any] = _DATA.get("_meta", {})
TIER_WEIGHT: Dict[str, int] = _DATA.get(
    "tier_weight", {"frontier": 0, "strong": 1, "mid": 2, "light": 3}
)
TASK_TYPES: Dict[str, Any] = _DATA.get("task_types", {})
_PROVIDER_DEFAULTS: Dict[str, Any] = _DATA.get("provider_defaults", {})
_MODELS: Dict[str, Any] = _DATA.get("models", {})

_NEUTRAL = {"tier": "mid", "strengths": []}


def profile_for(model_id: str, provider: str = "") -> Dict[str, Any]:
    """Return ``{tier, strengths}`` for a model.

    Resolution order: explicit per-model entry → provider default → neutral.
    This is what lets a newly-registered model rank without a JSON edit.
    """
    if model_id in _MODELS:
        return _MODELS[model_id]
    if provider and provider in _PROVIDER_DEFAULTS:
        return _PROVIDER_DEFAULTS[provider]
    return _NEUTRAL


def tier_weight(tier: str) -> int:
    """Numeric weight for a tier name (lower = more capable). Unknown → mid."""
    return TIER_WEIGHT.get(tier, TIER_WEIGHT.get("mid", 2))


def task_config(task_type: str) -> Dict[str, Any]:
    """Return the config block for a task type, falling back to 'general'."""
    return TASK_TYPES.get(task_type) or TASK_TYPES.get("general", {})


def detect_task_type(text: str) -> str:
    """Pick the task type whose keywords best match ``text`` (title+description).
    Ties and no-match fall back to 'general'."""
    text = (text or "").lower()
    best, best_n = "general", 0
    for name, cfg in TASK_TYPES.items():
        if name == "general":
            continue
        n = sum(1 for kw in cfg.get("keywords", []) if kw in text)
        if n > best_n:
            best_n, best = n, name
    return best


def detect_languages(text: str) -> List[str]:
    """Return the set of ISO-639-1 language codes detected in ``text``.

    Used by the Counselor to decide whether a task needs a multilingual model.
    Falls back to ``['en']`` on any failure so a detection error can never crash
    the recommender. langdetect is unreliable on very short text, so callers
    should treat "non-English OR more than one language" as the meaningful signal
    rather than trusting a single short-prompt classification. Low-probability
    matches are dropped to avoid one-word false positives (e.g. a stray accented
    word reading as a second language).
    """
    text = (text or "").strip()
    if not text:
        return ["en"]
    try:
        from langdetect import detect_langs
        probs = detect_langs(text)
        # Keep only languages with substantial probability. langdetect often
        # returns a long tail of low-confidence guesses (e.g. a short English
        # title reading as 14% Danish); a 0.3 floor drops those one-word false
        # positives while keeping genuinely mixed prompts.
        kept = [p.lang for p in probs if getattr(p, 'prob', 0.0) >= 0.3]
        if kept:
            return kept
        # Nothing cleared the floor (very short text) — trust the top guess.
        return [probs[0].lang] if probs else ["en"]
    except Exception:
        return ["en"]
