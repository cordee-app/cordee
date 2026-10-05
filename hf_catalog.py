"""Hugging Face catalogue — discovery, validation and local persistence.

Phase 1 of the Hugging Face Scout. Only stdlib + ``requests`` (already a
project dependency) are used; the heavy ``huggingface_hub``/``transformers``
stack is deferred to Phase 3/4. Nothing here executes a model — it only
searches, validates and records candidates for later adoption.

The persisted catalogue (``hf_models.json``) mirrors ``model_profiles.json`` /
``model_caps.json``: subjective drift-prone signals live in JSON so they can be
refreshed without a code change. Objective capabilities (context window, tools,
cost) are NOT stored here — they are read live by the Counselor.
"""
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

HF_API_BASE = 'https://huggingface.co/api/models'
_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hf_models.json')

# Best-effort family → existing AIngel provider id. A HF repo whose id /
# base_model matches one of these families can be served by an already-running
# engine today. Everything else returns '' (needs self-hosting). Deliberately
# conservative: a specialised fine-tune of an unknown base must not be silently
# served by the wrong engine.
_FAMILY_MAP = {
    'qwen3.5-397b': 'scw-qwen3.5-397b',
    'qwen3.6': 'scw-qwen3.6-35b',
    'qwen3-coder': 'scw-qwen3-coder-30b',
    'glm-5.2': 'scw-glm-5.2',
    'gpt-oss': 'scw-gpt-oss-120b',
    'mistral-small': 'scw-mistral-small-24b',
    'gemma-4': 'scw-gemma-4-26b',
    'llama-3.3': 'scw-llama-3.3-70b',
}

# Licences broadly acceptable for commercial use. ``openrail`` variants are
# permissive in practice (no non-commercial clause); ``-nc-`` and unlicensed
# repos are not.
_PERMISSIVE_LICENSES = {
    'apache-2.0', 'mit', 'bsd-2-clause', 'bsd-3-clause', 'cc0-1.0',
    'cc-by-4.0', 'cc-by-sa-4.0', 'afl-3.0', 'openrail', 'openrail++',
    'bigcode-openrail-m', 'llama3.1', 'llama3.2', 'llama3.3', 'llama3',
    'gemma', 'qwen', 'deepseek', 'mistralai-license',
}
_NONCOMMERCIAL_HINT = ('nc', 'non-commercial', 'noncommercial', 'cc-by-nc')

# Pipeline tags that can act as a task/agent completion model. Embeddings,
# fill-mask, NER and classification models cannot produce agent output even if
# hosted, so they are flagged "not a task model".
GENERATION_PIPELINES = {'text-generation', 'text2text-generation', 'any-to-any'}


def is_task_model_pipeline(pipeline_tag: str) -> bool:
    """True when a pipeline tag can drive task execution (a generation model)."""
    return (pipeline_tag or '').strip().lower() in GENERATION_PIPELINES


# Base-model families that are unmistakably text-generation LLMs. Used to
# infer task-model status when ``pipeline_tag`` is empty — which is the norm for
# GGUF/raw quant repos that the HF API does not classify.
_LLM_FAMILY_HINTS = (
    'llama', 'mistral', 'mixtral', 'qwen', 'bielik', 'gpt', 'gemma', 'phi',
    'falcon', 'zephyr', 'bloom', 't5', 'mt5', 'flan', 'opt', 'gemini',
    'deepseek', 'orca', 'vicuna', 'alpaca', 'solar', 'dbrx', 'olmo', 'glm',
    'baichuan', 'internlm', 'nemotron', 'granite', 'starcoder',
)


def _looks_generation(repo: Dict[str, Any]) -> bool:
    """Infer text-generation when ``pipeline_tag`` is empty."""
    repo = repo or {}
    tags = [str(t).lower() for t in (repo.get('tags') or [])]
    if 'gguf' in tags or 'text-generation' in tags:
        return True
    if any(t in ('instruct', 'chat', 'conversational') for t in tags):
        return True
    hay = ' '.join([
        _repo_id(repo),
        str(repo.get('base_model') or ''),
        str(_card_data(repo).get('base_model') or ''),
    ]).lower()
    return any(h in hay for h in _LLM_FAMILY_HINTS)


def _task_model(entry: Dict[str, Any]) -> bool:
    """True when an entry can act as a task/agent model.

    Prefers the ``validation.task_model`` verdict; otherwise infers from
    ``pipeline_tag``, then from family/GGUF hints when the tag is empty."""
    entry = entry or {}
    val = entry.get('validation') or {}
    if 'task_model' in val:
        return bool(val['task_model'])
    tag = (entry.get('pipeline_tag') or '').strip()
    if tag:
        return is_task_model_pipeline(tag)
    return _looks_generation(entry)


def limitations_for(entry: Dict[str, Any]) -> List[str]:
    """Human-readable limitations for an adopted HF model.

    Explains, in the Counselor output, why a candidate can or cannot be
    served/executed today. An empty list means fully servable."""
    entry = entry or {}
    out: List[str] = []
    mapping = (entry.get('provider_mapping') or '').strip()
    if not mapping:
        out.append('needs self-hosting')
    if not _task_model(entry):
        tag = (entry.get('pipeline_tag') or '').strip()
        out.append(f'not a task model ({tag})' if tag else 'pipeline unknown')
    lic = (entry.get('license') or '').strip()
    if lic and not license_is_permissive(lic):
        out.append(f'restrictive licence ({lic})')
    return out


def candidate_servable(entry: Dict[str, Any]) -> bool:
    """A candidate can actually run today when it has a serving path and is a
    generation model. Licence issues are surfaced, not silently blocked here."""
    entry = entry or {}
    mapping = (entry.get('provider_mapping') or '').strip()
    return bool(mapping) and _task_model(entry)


def _card_data(repo: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise ``cardData`` (dict or the list-of-{key,value} shape the API
    sometimes returns) into a plain dict."""
    cd = repo.get('cardData') or {}
    if isinstance(cd, dict):
        return cd
    if isinstance(cd, list):
        out: Dict[str, Any] = {}
        for item in cd:
            if isinstance(item, dict):
                k = item.get('key') or item.get('name')
                v = item.get('value')
                if k:
                    out[k] = v
        return out
    return {}


def _repo_id(repo: Dict[str, Any]) -> str:
    return (repo.get('id') or repo.get('modelId') or repo.get('repo_id') or '').strip()


def _license(repo: Dict[str, Any]) -> str:
    lic = repo.get('license') or _card_data(repo).get('license') or ''
    if isinstance(lic, dict):
        lic = lic.get('name') or lic.get('value') or ''
    return (lic or '').strip().lower()


def _languages(repo: Dict[str, Any]) -> List[str]:
    lang = _card_data(repo).get('language') or repo.get('language') or []
    if isinstance(lang, str):
        lang = [lang]
    return [str(x).strip().lower() for x in lang if str(x).strip()]


def license_is_permissive(license_tag: str) -> bool:
    """True when a licence tag is broadly safe for commercial use."""
    tag = (license_tag or '').lower()
    if not tag:
        return False
    if any(hint in tag for hint in _NONCOMMERCIAL_HINT):
        return False
    if tag in _PERMISSIVE_LICENSES:
        return True
    # e.g. 'apache-2.0 with some addendum' or 'openrail-m' variants.
    return any(tag.startswith(k) for k in ('apache-2.0', 'mit', 'openrail', 'cc-by-4'))


def resolve_provider_mapping(repo: Dict[str, Any]) -> str:
    """Return an existing AIngel provider id for this repo, or ''.

    Conservative on purpose: only *base* models (no declared ``base_model``)
    whose id matches a known hosted family are mapped. A fine-tune carries a
    ``base_model`` pointing at its parent and has *different weights* than the
    hosted engine, so it must not be silently substituted — it resolves to ''
    ("needs self-hosting") until a real serving path exists (Phase 3/4)."""
    if not repo:
        return ''
    if (str(repo.get('base_model') or '').strip()
            or str(_card_data(repo).get('base_model') or '').strip()):
        return ''
    hay = _repo_id(repo).lower()
    for family, mid in _FAMILY_MAP.items():
        if family in hay:
            return mid
    return ''


def validate_candidate(repo: Dict[str, Any]) -> Dict[str, Any]:
    """Judge a HF candidate without running it (plan §2.2 checklist).

    Returns ``{score (0.0–1.0), reasons, servable, task_model}``. The score
    captures the fine-tuning-readiness signals (licence, community, transparency,
    serving path) so Phase 4 can prioritise what to fine-tune first; ``task_model``
    tells the caller whether the model can actually generate task output."""
    repo = repo or {}
    reasons: List[str] = []
    score = 0.0

    lic = _license(repo)
    if license_is_permissive(lic):
        score += 0.30
        reasons.append(f'permissive licence ({lic})')
    else:
        reasons.append(f'licence {lic or "unspecified"} needs review')

    downloads = int(repo.get('downloads') or 0)
    likes = int(repo.get('likes') or 0)
    if downloads >= 10000:
        score += 0.10
        reasons.append(f'{downloads:,} downloads/mo')
    elif downloads >= 1000:
        score += 0.05
        reasons.append(f'{downloads:,} downloads/mo')
    else:
        reasons.append('low downloads')
    if likes >= 100:
        score += 0.10
        reasons.append(f'{likes:,} likes')
    else:
        reasons.append('low likes')

    if _card_data(repo):
        score += 0.15
        reasons.append('has model card')
    else:
        reasons.append('no model card')

    mapping = resolve_provider_mapping(repo)
    if mapping:
        score += 0.20
        reasons.append(f'servable as {mapping}')
    else:
        reasons.append('needs self-hosting')

    # Task-model capability. A generation pipeline is rewarded; a non-generation
    # one (fill-mask, NER, embeddings) is NOT — even though it may carry a tag.
    # An empty tag (GGUF/raw repos) is inferred from family/tags rather than
    # penalised, so a real LLM like Bielik is not out-ranked by an NER model.
    tag = (repo.get('pipeline_tag') or '').strip()
    task_model = is_task_model_pipeline(tag)
    if not tag:
        if _looks_generation(repo):
            task_model = True
            score += 0.05
            reasons.append('text-generation (inferred)')
        else:
            reasons.append('pipeline unknown')
    elif task_model:
        score += 0.05
        reasons.append(f'{tag} model')
    else:
        reasons.append(f'not a task model ({tag})')

    tags = [str(t).lower() for t in (repo.get('tags') or [])]
    if any(t in tags for t in ('legal', 'law', 'finance', 'medical', 'clinical', 'biomedical')):
        score += 0.10
        reasons.append('domain-specialised')

    return {
        'score': round(min(score, 1.0), 3),
        'reasons': reasons,
        'servable': bool(mapping) and task_model,
        'task_model': task_model,
    }


def _normalize(repo: Dict[str, Any]) -> Dict[str, Any]:
    validation = validate_candidate(repo)
    return {
        'repo_id': _repo_id(repo),
        'label': _repo_id(repo),
        'pipeline_tag': repo.get('pipeline_tag') or '',
        'tags': list(repo.get('tags') or []),
        'language': _languages(repo),
        'license': _license(repo),
        'downloads': int(repo.get('downloads') or 0),
        'likes': int(repo.get('likes') or 0),
        'provider_mapping': resolve_provider_mapping(repo),
        'validation': validation,
    }


def search_models(q: str = '', pipeline_tag: str = '', language: str = '',
                  limit: int = 20) -> List[Dict[str, Any]]:
    """Search the HF API and return a normalised, validated list.

    ``language`` and domain tags are applied client-side because the list API
    does not filter on them directly."""
    params: Dict[str, Any] = {'limit': int(limit or 20), 'full': 'true'}
    if q:
        params['search'] = q
    if pipeline_tag:
        params['filter'] = pipeline_tag
    resp = requests.get(HF_API_BASE, params=params, timeout=20)
    resp.raise_for_status()
    raw = resp.json()
    if not isinstance(raw, list):
        return []
    out = []
    lang = (language or '').strip().lower()
    for repo in raw:
        norm = _normalize(repo)
        if lang and lang not in norm['language']:
            continue
        out.append(norm)
    return out


def get_model_card(repo_id: str) -> Dict[str, Any]:
    """Fetch a single model's metadata (``?full=true`` for the card)."""
    resp = requests.get(f'{HF_API_BASE}/{repo_id}', params={'full': 'true'}, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ── Persistence (hf_models.json) ──────────────────────────────────────────────


def load_catalog() -> Dict[str, Any]:
    try:
        with open(_CATALOG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.setdefault('_meta', {})
    data.setdefault('models', {})
    return data


def save_catalog(data: Dict[str, Any]) -> None:
    data = data or {}
    data.setdefault('_meta', {})
    data.setdefault('models', {})
    with open(_CATALOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def catalog_entry(repo_id: str) -> Optional[Dict[str, Any]]:
    return load_catalog()['models'].get(repo_id)


def register_model(repo_id: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Add/refresh a catalogue entry. ``entry`` is the normalised search shape
    plus any overrides (label, provider_mapping, validation)."""
    data = load_catalog()
    data['_meta']['last_reviewed'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    data['models'][repo_id] = entry
    save_catalog(data)
    return entry


def refresh_catalog() -> Dict[str, Any]:
    """Re-fetch and re-validate every catalogued model.

    Used after a scoring change so already-adopted models get the updated score
    and ``task_model`` verdict. A single failed repo never raises — its existing
    entry is kept as-is."""
    data = load_catalog()
    for repo_id in list(data['models']):
        try:
            data['models'][repo_id] = _normalize(get_model_card(repo_id))
        except Exception:
            continue
    data['_meta']['last_reviewed'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    save_catalog(data)
    return data['models']
