import os
import json
import time
import subprocess
import urllib.request
import urllib.error
import logging
from datetime import datetime, timezone
from random import choices
from string import ascii_lowercase, digits

import agent_config
import agent_db

_log = logging.getLogger(__name__)

_SCW_SECRET_KEY = os.environ.get('SCW_SECRET_KEY', '')
_SCW_DEFAULT_PROJECT_ID = (
    os.environ.get('SCW_DEFAULT_PROJECT_ID')
    or os.environ.get('SCW_PROJECT_ID', '')
)
# Hugging Face READ token used when importing custom models. Public repos work
# without one; gated/private repos require a token whose account has been granted
# access. Kept in .env so a single token covers every import.
_HF_TOKEN = os.environ.get('HF_TOKEN', '')
_EUR_TO_USD = 1.08
# Official Scaleway Generative APIs Dedicated Deployment prices (fr-par), EUR/h.
# Source: https://www.scaleway.com/en/pricing/model-as-a-service/ (verified 2026-10-05).
_HOURLY_RATES = {
    'L4': 0.93, 'L40S': 1.72, 'H100': 3.40,
    'H100-2': 6.68, 'H100-SXM-2': 7.95,
    'H100-SXM-4': 15.22, 'H100-SXM-8': 30.06,
}
_DEPLOYED_NODE_TYPES = ('L4', 'L40S', 'H100', 'H100-2', 'H100-SXM-2', 'H100-SXM-4', 'H100-SXM-8')
_READY_TIMEOUT = 1800
_POLL_INTERVAL = 20
# Downloading a custom model (e.g. a ~55 GB 27B fp16 repo) can run long; allow
# an hour before giving up. Import itself is free — billing only starts when the
# model is actually deployed.
_IMPORT_TIMEOUT = 3600
_INFER_BASE = 'https://api.scaleway.com/inference/v1/regions'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _scw_headers():
    return {'X-Auth-Token': _SCW_SECRET_KEY, 'Content-Type': 'application/json'}


def _scw_get(url):
    req = urllib.request.Request(url, headers=_scw_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _scw_delete(url):
    req = urllib.request.Request(url, method='DELETE', headers=_scw_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def _scw_post(url, body, timeout=120):
    """POST JSON and return the parsed response. Raises HTTPError on rejection so
    callers can map it with the friendly classifiers; kept separate from
    ``urllib`` so tests can patch a single seam."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method='POST', headers=_scw_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _rand6():
    return ''.join(choices(ascii_lowercase + digits, k=6))


def _hourly_rate(node_type):
    return _HOURLY_RATES.get(node_type, 0.93)


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def list_models(region='fr-par', **_):
    if not _SCW_SECRET_KEY:
        _log.warning('list_models: SCW_SECRET_KEY not set')
        return []
    url = f'{_INFER_BASE}/{region}/models'
    try:
        data = _scw_get(url)
        models = data.get('models', []) if isinstance(data, dict) else data
        result = []
        for m in models:
            # A node type is only usable when at least one of its quantizations
            # is `allowed` — listing every node_type_name regardless was wrong
            # (e.g. qwen3.6-35b:bf16 only allows H100, not L4/L40S).
            node_types = []
            quantizations = {}   # node_type -> sorted allowed bits (highest last)
            stock_status = {}    # node_type -> stock_status (available/out_of_stock/unknown_stock)
            context_size = m.get('max_context_size')
            for ns in m.get('nodes_support', []):
                for n in ns.get('nodes', []):
                    nt = n.get('node_type_name', '')
                    if not nt:
                        continue
                    # Live API names the field `quantization_bits` (not `bits`),
                    # and `max_context_size` is per-quantization on the node.
                    allowed_bits = []
                    for q in (n.get('quantizations') or []):
                        if q.get('allowed') and q.get('quantization_bits') is not None:
                            allowed_bits.append(q['quantization_bits'])
                            if q.get('max_context_size'):
                                context_size = context_size or q['max_context_size']
                    allowed_bits = sorted(allowed_bits)
                    if not allowed_bits:
                        continue
                    if nt not in node_types:
                        node_types.append(nt)
                    quantizations[nt] = allowed_bits
                    if n.get('stock_status'):
                        stock_status[nt] = n['stock_status']
            if not any(nt in node_types for nt in _DEPLOYED_NODE_TYPES):
                continue
            tags = [str(t).lower() for t in (m.get('tags') or [])]
            result.append({
                'id': m.get('id'),
                'name': m.get('name'),
                'tags': tags,
                'custom': 'custom' in tags,
                'status': m.get('status'),
                'node_types': node_types,
                'quantizations': quantizations,
                'stock_status': stock_status,
                'max_context_size': context_size,
                'eula_required': bool(m.get('has_eula', False)),
                'size_bytes': m.get('size_bytes'),
                'parameter_size_bits': m.get('parameter_size_bits'),
                'hourly_eur': {nt: _hourly_rate(nt) for nt in node_types},
            })
        return result
    except Exception as e:
        _log.warning('list_models failed: %s', e)
        return []


def _find_model(model_name, region):
    models = list_models(region=region)
    for m in models:
        if m['name'] == model_name or m['id'] == model_name:
            return m
    for m in models:
        if model_name.lower() in (m['name'] or '').lower():
            return m
    return None


def _find_model_id(model_name, region):
    m = _find_model(model_name, region)
    return m['id'] if m else None


def _pick_quantization(model, node_type):
    """Highest allowed quantization bits for `node_type`, or None. Custom fp32
    models need an explicit `quantization: {bits}` on deployment create; catalogue
    models let Scaleway infer a default, so callers only consult this for custom
    models. Bits are returned highest-first as the safe (most-compatible) default."""
    bits = (model or {}).get('quantizations', {}).get(node_type)
    return max(bits) if bits else None


# ── Custom model import (Hugging Face → Scaleway model library) ───────────────

def _classify_import_error(code, body):
    """Map a Scaleway model import/verify rejection to a friendly, in-app message.

    The 412 ``resource_not_usable`` body carries a ``help_message`` that is the
    single most useful thing a user can read (e.g. GGUF repos: "Maximum model
    context length is not available in config.json"), so it is surfaced verbatim
    when present. ``kind`` lets the UI react (e.g. suggest a Transformers-format
    repo for ``unsupported_format``)."""
    msg = ''
    try:
        parsed = json.loads(body or '{}')
        msg = (parsed.get('help_message') or parsed.get('message') or '').strip()
    except (ValueError, TypeError):
        msg = ''
    b = (body or '').lower()
    if code in (401, 403):
        return ('auth', 'Scaleway rejected the import (check SCW_SECRET_KEY and '
                'API permissions).')
    if code == 404:
        return ('not_found', 'The Hugging Face repository or Scaleway model was '
                'not found.')
    if code == 409:
        return ('conflict', 'A model with that name already exists in your '
                'Scaleway Organization/Project. Choose a different name.')
    if 'does not exist' in b or 'permissions to access' in b or 'gated' in b:
        return ('access', (msg + ' ' if msg else '')
                + 'For gated or private models, set HF_TOKEN in .env to a token '
                  'whose account has been granted access.')
    if 'quantization' in b and 'not available' in b:
        return ('unsupported_quantization', msg or 'This quantization is not '
                'supported for the chosen node type.')
    if any(k in b for k in ('max_position_embeddings', 'config.json',
                            'maximum model context length',
                            'not supported', 'not_usable', 'not usable',
                            'tokeniz')):
        return ('unsupported_format', msg or 'Scaleway cannot import this repo. '
                'It must be a full Transformers repo (config.json + tokenizer '
                'files + weights). GGUF repos are not supported — use the base '
                'or FP8/NVFP4 repo instead.')
    if code in (400, 412, 422):
        return ('invalid', msg or f'Scaleway rejected the import: {(body or "")[:200]}')
    return ('error', msg or f'Scaleway import failed (HTTP {code}): {(body or "")[:200]}')


class ScwImportError(RuntimeError):
    """A model import/verify rejection, carrying a machine-readable ``kind``."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.friendly = message


def _hf_url(repo_id):
    """Normalise a HF repo id or URL to the canonical model-page URL Scaleway
    expects as ``source.url``."""
    repo = (repo_id or '').strip().rstrip('/')
    if repo.startswith('http://') or repo.startswith('https://'):
        return repo
    return f'https://huggingface.co/{repo}'


def verify_hf_model(repo_id, region='fr-par', hf_token=None, **_):
    """Pre-flight an HF repo with Scaleway's own ``verify-model`` endpoint.

    Returns ``{ok, nodes, size_bytes}`` or ``{ok: False, error, error_code}``.
    This is authoritative for compatibility (it is what the console's "Verify
    import" button calls) and needs no import — so a GGUF repo is rejected here
    before anything is created. ``nodes`` mirrors ``list_models`` for the
    prospective model, ready to drive node/quantization pickers."""
    if not _SCW_SECRET_KEY:
        return {'ok': False, 'error': 'SCW_SECRET_KEY not set', 'error_code': 'auth'}
    url = f'{_INFER_BASE}/{region}/verify-model'
    token = hf_token or _HF_TOKEN
    source = {'url': _hf_url(repo_id)}
    if token:
        source['secret'] = token
    try:
        raw = _scw_post(url, {'source': source})
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        kind, friendly = _classify_import_error(e.code, body)
        return {'ok': False, 'error': friendly, 'error_code': kind}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'error_code': 'error'}

    nodes = []
    quantizations = {}
    context_size = None
    for n in raw.get('nodes') or []:
        nt = n.get('node_type_name', '')
        if not nt:
            continue
        allowed_bits = []
        for q in (n.get('quantizations') or []):
            if q.get('allowed') and q.get('quantization_bits') is not None:
                allowed_bits.append(q['quantization_bits'])
                cs = q.get('max_context_size')
                if cs and (context_size is None or cs > context_size):
                    context_size = cs
        if allowed_bits and nt not in nodes:
            nodes.append(nt)
            quantizations[nt] = sorted(allowed_bits)
    return {
        'ok': True,
        'repo_id': (repo_id or '').strip(),
        'nodes': nodes,
        'quantizations': quantizations,
        'max_context_size': context_size,
        'size_bytes': raw.get('size_bytes'),
        'hourly_eur': {nt: _hourly_rate(nt) for nt in nodes},
    }


def import_hf_model(name, repo_id, project_id=None, region='fr-par',
                    hf_token=None, **_):
    """Import an HF repo into the Scaleway model library (``POST /models``).

    Returns the created model object (``status`` is ``preparing``/``downloading``
    while it downloads). Import is free; billing starts only on deployment.
    Raises ``ScwImportError`` with a friendly message on rejection."""
    if not _SCW_SECRET_KEY:
        raise ScwImportError('auth', 'SCW_SECRET_KEY not set')
    url = f'{_INFER_BASE}/{region}/models'
    token = hf_token or _HF_TOKEN
    source = {'url': _hf_url(repo_id)}
    if token:
        source['secret'] = token
    body = {'name': name, 'source': source}
    if project_id:
        body['project_id'] = project_id
    elif _SCW_DEFAULT_PROJECT_ID:
        body['project_id'] = _SCW_DEFAULT_PROJECT_ID
    try:
        return _scw_post(url, body)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors='replace')
        kind, friendly = _classify_import_error(e.code, raw)
        raise ScwImportError(kind, friendly) from e


def get_model(model_id, region='fr-par', **_):
    """Fetch one model from the library (``GET /models/{id}``), or None."""
    url = f'{_INFER_BASE}/{region}/models/{model_id}'
    try:
        return _scw_get(url)
    except Exception as e:
        _log.warning('get_model %s failed: %s', model_id, e)
        return None


def wait_model_ready(model_id, region='fr-par', timeout=_IMPORT_TIMEOUT,
                     status_cb=None, **_):
    """Poll a model until it reaches ``ready``. Returns the model dict, or None
    on timeout/error. ``error_message`` on a failed model is logged and left on
    the returned dict when status is ``error``."""
    url = f'{_INFER_BASE}/{region}/models/{model_id}'
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = _scw_get(url)
            status = data.get('status', '')
            if status_cb:
                try:
                    status_cb(status)
                except Exception:
                    pass
            if status == 'ready':
                return data
            if status == 'error':
                _log.error('model %s import error: %s', model_id,
                           data.get('error_message'))
                return data
        except Exception as e:
            _log.warning('poll model %s failed: %s', model_id, e)
        time.sleep(_POLL_INTERVAL)
    _log.error('model %s did not become ready within %ds', model_id, timeout)
    return None


def delete_model(model_id, region='fr-par', **_):
    """Delete an imported model from the Scaleway library (frees quota)."""
    url = f'{_INFER_BASE}/{region}/models/{model_id}'
    try:
        _scw_delete(url)
        return {'ok': True}
    except Exception as e:
        _log.warning('delete_model %s failed: %s', model_id, e)
        return {'ok': False, 'error': str(e)}


def get_model_eula(model_id, region='fr-par', **_):
    """Fetch a model's EULA content (empty string when none)."""
    url = f'{_INFER_BASE}/{region}/models/{model_id}/eula'
    try:
        data = _scw_get(url)
        return data.get('content', '') if isinstance(data, dict) else ''
    except Exception as e:
        _log.warning('get_model_eula %s failed: %s', model_id, e)
        return ''


def model_exists(model_id, region='fr-par', **_):
    """Tri-state existence check for a library model: True (found), False (404,
    authoritatively gone), or None (couldn't tell — network/other error). Used to
    reconcile stale local import rows after an out-of-band deletion."""
    if not model_id:
        return False
    url = f'{_INFER_BASE}/{region}/models/{model_id}'
    try:
        _scw_get(url)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return None
    except Exception:
        return None


def _extract_endpoint_url(deployment):
    endpoints = deployment.get('endpoints', []) or []
    for ep in endpoints:
        url = ep.get('url')
        if url:
            return url
        pub = ep.get('public_endpoint') or {}
        if pub.get('url'):
            return pub['url']
    endpoint = deployment.get('endpoint') or {}
    return endpoint.get('url')


def _wait_ready(scw_deployment_id, region, status_cb=None):
    url = f'{_INFER_BASE}/{region}/deployments/{scw_deployment_id}'
    deadline = time.time() + _READY_TIMEOUT
    while time.time() < deadline:
        try:
            data = _scw_get(url)
            status = data.get('status', '')
            if status_cb:
                try:
                    status_cb(status)
                except Exception:
                    pass
            if status == 'ready':
                return data
            if status in ('error', 'failed'):
                _log.error('deployment %s entered status %s', scw_deployment_id, status)
                return None
        except Exception as e:
            _log.warning('poll deployment %s failed: %s', scw_deployment_id, e)
        time.sleep(_POLL_INTERVAL)
    _log.error('deployment %s did not become ready within %ds', scw_deployment_id, _READY_TIMEOUT)
    return None


def _warmup(endpoint_url, model_name):
    body = json.dumps({
        'model': model_name,
        'messages': [{'role': 'user', 'content': 'hi'}],
        'max_tokens': 1,
    }).encode()
    req = urllib.request.Request(
        endpoint_url + '/v1/chat/completions',
        data=body,
        method='POST',
        headers={
            'Authorization': f'Bearer {_SCW_SECRET_KEY}',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            json.loads(r.read())
        _log.info('warmup ok for %s', endpoint_url)
    except Exception as e:
        _log.warning('warmup request failed (may still work on retry): %s', e)


class ScwDeployError(RuntimeError):
    """A deployment-create rejection, with a machine-readable ``kind`` so the API
    can return a friendly in-app message instead of raw Scaleway JSON."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.friendly = message


def _classify_deploy_error(code, body):
    """Map a Scaleway deployment-create rejection to a friendly, in-app message.

    The raw Scaleway body is a wall of JSON users shouldn't have to parse; the UI
    shows ``friendly`` inline and keeps the picker open so the user can switch
    node/model instead of staring at a toast (or a silent failure)."""
    b = (body or '').lower()
    if code in (403, 401):
        return ('auth', 'Scaleway rejected the request (check SCW_SECRET_KEY and API permissions).')
    if code == 404:
        return ('not_found', 'The model or deployment was not found on Scaleway.')
    if code == 409:
        return ('conflict', 'A deployment with that name already exists.')
    if any(k in b for k in ('out of stock', 'out_of_stock', 'no available',
                            'not available', 'no available node', 'insufficient capacity')):
        return ('out_of_stock', 'This GPU node type is out of stock right now. '
                'Try a different node type, or retry in a few minutes.')
    if 'quota' in b:
        return ('quota', 'Scaleway quota exceeded — reduce the deployment size or '
                'request a quota increase.')
    if any(k in b for k in ('resource_not_usable', 'not_usable', 'not usable')):
        return ('model_not_usable', 'Scaleway says this model is not usable for '
                'deployment (architecture/format not supported, or its EULA was not accepted).')
    if 'eula' in b:
        return ('eula', 'This model requires accepting a EULA before it can be deployed.')
    if any(k in b for k in ('validation', 'invalid')):
        return ('invalid', f'Scaleway rejected the deployment request: {(body or "")[:200]}')
    return ('error', f'Scaleway deployment create failed (HTTP {code}): {(body or "")[:200]}')


def _scw_create_deployment(name, model_id, node_type, endpoint_kind, region, quantization_bits=None):
    url = f'{_INFER_BASE}/{region}/deployments'
    body = {
        'name': name,
        'model_id': model_id,
        'node_type_name': node_type,
        'accept_eula': True,
        'min_size': 1,
        'max_size': 1,
        'endpoints': [{'public_network': {}} if endpoint_kind == 'public' else {'private_network': {}}],
    }
    if quantization_bits is not None:
        # Custom models need an explicit `quantization` (DeploymentQuantization
        # requires `bits`). Catalogue models omit it so Scaleway keeps inferring
        # its default — which is what the int4/bf16 catalogue deployments rely on.
        body['quantization'] = {'bits': int(quantization_bits)}
    if _SCW_DEFAULT_PROJECT_ID:
        body['project_id'] = _SCW_DEFAULT_PROJECT_ID
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method='POST', headers=_scw_headers())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        kind, friendly = _classify_deploy_error(e.code, body)
        raise ScwDeployError(kind, friendly) from e


def _delete_deployment(scw_deployment_id, region):
    url = f'{_INFER_BASE}/{region}/deployments/{scw_deployment_id}'
    try:
        _scw_delete(url)
    except Exception as e:
        _log.warning('delete deployment %s failed: %s', scw_deployment_id, e)


def _open_window(project_id, model_name, node_type='L4', endpoint_kind='public',
                 idle_delete_minutes=30, region='fr-par', is_shared=0):
    if not _SCW_SECRET_KEY:
        return {'ok': False, 'error': 'SCW_SECRET_KEY not set'}

    model = _find_model(model_name, region)
    if not model:
        return {'ok': False, 'error': f'model not found: {model_name}'}
    model_id = model['id']
    # The chat-completions `model` field must be the model NAME (not its id), so
    # store the resolved name and reuse it for warmup and inference.
    resolved_name = model['name'] or model_name
    # Custom models (tag `custom`) need an explicit quantization on create.
    quant_bits = _pick_quantization(model, node_type) if model.get('custom') else None

    name = f'aingel-dep-{_rand6()}'
    scw_deployment_id = None
    try:
        dep = _scw_create_deployment(name, model_id, node_type, endpoint_kind, region, quant_bits)
        scw_deployment_id = dep.get('id')
        if not scw_deployment_id:
            return {'ok': False, 'error': f'scw create returned no deployment id: {dep}'}

        endpoint_url = _extract_endpoint_url(dep) or ''
        hourly_eur = _hourly_rate(node_type)
        now = _now()
        conn = agent_db.get_db()
        conn.execute(
            '''INSERT INTO scw_deployments
               (project_id, scw_deployment_id, model_name, node_type, endpoint_kind,
                endpoint_url, status, provider_status, hourly_eur, idle_delete_minutes,
                max_context_size, accrued_cost_usd, last_billed_at, created_at, is_shared)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (project_id, scw_deployment_id, resolved_name, node_type, endpoint_kind,
             endpoint_url, 'creating', 'creating', hourly_eur, idle_delete_minutes,
             model.get('max_context_size'),
             0.0, now, now, is_shared)
        )
        conn.commit()
        db_id = conn.execute(
            'SELECT id FROM scw_deployments WHERE scw_deployment_id=?',
            (scw_deployment_id,)
        ).fetchone()
        conn.close()
        db_id = db_id['id'] if db_id else None

        import threading
        def _bg():
            def _set_provider_status(s):
                connp = agent_db.get_db()
                connp.execute('UPDATE scw_deployments SET provider_status=? WHERE scw_deployment_id=?',
                              (s, scw_deployment_id))
                connp.commit()
                connp.close()

            ready_dep = _wait_ready(scw_deployment_id, region, status_cb=_set_provider_status)
            if not ready_dep:
                # Never leave a half-provisioned deployment billing hourly: delete
                # the remote deployment and surface the real provider status.
                _log.error('deployment %s did not become ready — deleting to stop billing',
                           scw_deployment_id)
                _delete_deployment(scw_deployment_id, region)
                conn2 = agent_db.get_db()
                row2 = conn2.execute(
                    'SELECT provider_status FROM scw_deployments WHERE scw_deployment_id=?',
                    (scw_deployment_id,)).fetchone()
                final_status = (row2['provider_status'] if row2 else None) or 'failed'
                conn2.execute('UPDATE scw_deployments SET status=?, provider_status=? WHERE scw_deployment_id=?',
                              ('error', final_status, scw_deployment_id))
                conn2.commit()
                conn2.close()
                return
            url = _extract_endpoint_url(ready_dep) or endpoint_url
            _warmup(url, resolved_name)
            conn2 = agent_db.get_db()
            conn2.execute('UPDATE scw_deployments SET status=?, endpoint_url=?, provider_status=? WHERE scw_deployment_id=?',
                          ('ready', url, 'ready', scw_deployment_id))
            conn2.commit()
            conn2.close()
            _log.info('deployment %s ready at %s', scw_deployment_id, url)
        t = threading.Thread(target=_bg, daemon=True)
        t.start()

        proj = agent_db.get_project(project_id) if project_id else None
        if agent_config.IS_VAULT or (proj and agent_config.eu_only_for(proj)):
            agent_config.eu_audit(
                proj.get('path') if proj else None,
                model=f'scw-dep-{scw_deployment_id}', provider='scaleway-deployment',
                allowed=True, caller='open_window', bytes_out=0,
            )

        return {
            'ok': True,
            'deployment_id': scw_deployment_id,
            'db_id': db_id,
            'endpoint_url': endpoint_url,
            'status': 'creating',
            'model_id': get_deployment_model_id(scw_deployment_id),
            'hourly_eur': hourly_eur,
            'is_shared': is_shared,
        }
    except ScwDeployError as e:
        _log.warning('open_window rejected: %s (%s)', e.friendly, e.kind)
        return {'ok': False, 'error': e.friendly, 'error_code': e.kind}
    except Exception as e:
        _log.error('open_window failed: %s', e, exc_info=True)
        if scw_deployment_id:
            _delete_deployment(scw_deployment_id, region)
        return {'ok': False, 'error': str(e)}


def open_window(project_id, model_name, node_type='L4', endpoint_kind='public',
                idle_delete_minutes=30, region='fr-par', **_):
    return _open_window(project_id, model_name, node_type, endpoint_kind,
                        idle_delete_minutes, region, is_shared=0)


def open_shared_window(model_name, node_type='L4', endpoint_kind='public',
                       idle_delete_minutes=30, region='fr-par', **_):
    """Open a cross-project GPU window (not owned by any single project)."""
    return _open_window(None, model_name, node_type, endpoint_kind,
                        idle_delete_minutes, region, is_shared=1)


def record_deployment_call(model_id, project_id=None, task_id=None, exec_id=None,
                           tokens_in=0, tokens_out=0, duration_s=0.0):
    """Record a per-call attribution row for a shared GPU window so its accrued
    cost can be split across the projects that used it. No-op for non-deployment
    models or when the deployment row can't be resolved."""
    if not model_id or not model_id.startswith('scw-dep-'):
        return
    dep = _get_deployment_by_scw_id(model_id[len('scw-dep-'):])
    if not dep:
        return
    agent_db.add_deployment_call(
        dep['id'], project_id=project_id, task_id=task_id, exec_id=exec_id,
        tokens_in=tokens_in, tokens_out=tokens_out, duration_s=duration_s)


def close_window(deployment_db_id, region='fr-par', **_):
    conn = agent_db.get_db()
    row = conn.execute(
        'SELECT * FROM scw_deployments WHERE id=?', (deployment_db_id,)
    ).fetchone()
    if not row:
        conn.close()
        return {'ok': False, 'error': f'deployment db id {deployment_db_id} not found'}
    row = dict(row)

    if row['status'] in ('deleted', 'deleting'):
        conn.close()
        return {'ok': True, 'final_cost_usd': row['accrued_cost_usd']}

    accrue_cost(deployment_db_id, region=region)
    cost_row = dict(conn.execute(
        'SELECT accrued_cost_usd FROM scw_deployments WHERE id=?',
        (deployment_db_id,)
    ).fetchone())

    conn.execute(
        "UPDATE scw_deployments SET status='deleting' WHERE id=?",
        (deployment_db_id,)
    )
    conn.commit()
    conn.close()

    _delete_deployment(row['scw_deployment_id'], region)

    conn = agent_db.get_db()
    conn.execute(
        "UPDATE scw_deployments SET status='deleted', deleted_at=? WHERE id=?",
        (_now(), deployment_db_id)
    )
    conn.commit()
    conn.close()

    # Shared windows split their accrued cost across the projects that used them.
    shares = {}
    if row.get('is_shared'):
        try:
            shares = agent_db.allocate_window_costs(deployment_db_id)
        except Exception as e:
            _log.warning('allocate_window_costs failed for %s: %s', deployment_db_id, e)

    return {'ok': True, 'final_cost_usd': cost_row['accrued_cost_usd'],
            'cost_by_project': shares}


def close_all_windows(project_id, region='fr-par', **_):
    conn = agent_db.get_db()
    rows = conn.execute(
        "SELECT id FROM scw_deployments WHERE project_id=? AND is_shared=0 "
        "AND status IN ('ready','creating')",
        (project_id,)
    ).fetchall()
    conn.close()

    closed = []
    for r in rows:
        result = close_window(r['id'], region=region)
        closed.append({'db_id': r['id'], 'ok': result.get('ok', False),
                       'final_cost_usd': result.get('final_cost_usd', 0.0)})
    return {'ok': True, 'closed': closed, 'count': len(closed)}


def accrue_cost(deployment_db_id, region='fr-par', **_):
    conn = agent_db.get_db()
    row = conn.execute(
        'SELECT * FROM scw_deployments WHERE id=?', (deployment_db_id,)
    ).fetchone()
    if not row:
        conn.close()
        return 0.0
    row = dict(row)

    if row['status'] not in ('ready', 'creating'):
        conn.close()
        return row.get('accrued_cost_usd', 0.0)

    last = _parse_iso(row.get('last_billed_at')) or _parse_iso(row.get('created_at'))
    now_dt = datetime.now(timezone.utc)
    if last:
        elapsed_hours = max(0.0, (now_dt - last).total_seconds() / 3600.0)
    else:
        elapsed_hours = 0.0

    cost_usd = elapsed_hours * row.get('hourly_eur', 0.0) * _EUR_TO_USD
    new_accrued = round(row.get('accrued_cost_usd', 0.0) + cost_usd, 6)
    now_iso = now_dt.isoformat()

    conn.execute(
        'UPDATE scw_deployments SET accrued_cost_usd=?, last_billed_at=? WHERE id=?',
        (new_accrued, now_iso, deployment_db_id)
    )
    conn.commit()
    conn.close()
    return new_accrued


def accrue_all(region='fr-par', **_):
    conn = agent_db.get_db()
    rows = conn.execute(
        "SELECT id, project_id FROM scw_deployments WHERE status IN ('ready','creating')"
    ).fetchall()
    conn.close()

    summary = {'count': 0, 'total_accrued_usd': 0.0, 'by_project': {}}
    for r in rows:
        cost = accrue_cost(r['id'], region=region)
        summary['count'] += 1
        summary['total_accrued_usd'] = round(summary['total_accrued_usd'] + cost, 6)
        pid = r['project_id']
        summary['by_project'].setdefault(pid, 0.0)
        summary['by_project'][pid] = round(summary['by_project'][pid] + cost, 6)
    return summary


def get_deployment_model_id(scw_deployment_id, **_):
    return f'scw-dep-{scw_deployment_id}'


def _get_deployment_by_scw_id(scw_deployment_id):
    conn = agent_db.get_db()
    row = conn.execute(
        'SELECT * FROM scw_deployments WHERE scw_deployment_id=?',
        (scw_deployment_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def call_deployment(model_id, prompt, max_tokens=8192, *, project_path=None, **_):
    if not model_id.startswith('scw-dep-'):
        raise ValueError(f'call_deployment expects scw-dep-* model_id, got {model_id}')
    scw_deployment_id = model_id[len('scw-dep-'):]
    dep = _get_deployment_by_scw_id(scw_deployment_id)
    if not dep:
        raise RuntimeError(f'no deployment found for {model_id}')
    if dep['status'] != 'ready':
        raise RuntimeError(f'deployment {model_id} status is {dep["status"]}, not ready')

    accrue_cost(dep['id'])

    endpoint_url = dep['endpoint_url']
    body = json.dumps({
        'model': dep['model_name'],
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
    }).encode()
    req = urllib.request.Request(
        endpoint_url + '/v1/chat/completions',
        data=body,
        method='POST',
        headers={
            'Authorization': f'Bearer {_SCW_SECRET_KEY}',
            'Content-Type': 'application/json',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors='replace')
        raise RuntimeError(
            f'deployment {scw_deployment_id} HTTP {e.code}: {body[:600]}') from e

    text = ''
    choices = data.get('choices') or []
    if choices:
        text = choices[0].get('message', {}).get('content', '') or ''
    usage = data.get('usage', {})
    tok_in = usage.get('prompt_tokens', 0) or 0
    tok_out = usage.get('completion_tokens', 0) or 0
    return text, tok_in, tok_out, 0.0


def auto_cleanup_check(region='fr-par', **_):
    conn = agent_db.get_db()
    rows = conn.execute(
        "SELECT * FROM scw_deployments WHERE status IN ('ready','creating')"
    ).fetchall()
    deployments = [dict(r) for r in rows]
    conn.close()

    closed = 0
    now_dt = datetime.now(timezone.utc)
    for dep in deployments:
        idle_min = dep.get('idle_delete_minutes', 30) or 30
        last = _parse_iso(dep.get('last_billed_at')) or _parse_iso(dep.get('created_at'))
        idle = False
        if last:
            elapsed_min = (now_dt - last).total_seconds() / 60.0
            if elapsed_min > idle_min:
                idle = True

        over_budget = False
        budget_monthly = 0.0
        current_spend = 0.0
        try:
            budget = agent_db.get_project_budget(dep['project_id'])
            if budget:
                budget_monthly = budget.get('budget_monthly', 0.0) or 0.0
                current_spend = budget.get('current_month_spend', 0.0) or 0.0
        except Exception:
            pass
        remaining = budget_monthly - current_spend
        accrued = dep.get('accrued_cost_usd', 0.0) or 0.0
        if budget_monthly > 0 and accrued > remaining:
            over_budget = True

        if idle or over_budget:
            reason = 'idle' if idle else 'budget_exceeded'
            _log.info('auto_cleanup: closing deployment %s (%s)', dep['id'], reason)
            result = close_window(dep['id'], region=region)
            if result.get('ok'):
                closed += 1
    return closed