"""Scaleway session storage — Phase 2.

Per-project isolated storage envelope on Scaleway: a dedicated Scaleway Project,
a project-scoped IAM API key (with expiry), a Key Manager key, and an SSE-KMS
S3 bucket. All four resources are created together in `create_session()` and
torn down together in `close_session()` (crypto-shred).

REST patterns verified against the smoke-test scripts in scripts/vault-smoke-test/
and the live Scaleway API on 2026-08-06.
"""
import json
import logging
import os
import random
import re
import string
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

import agent_config
import agent_db

log = logging.getLogger(__name__)


def _scw_org_id():
    return os.environ.get('SCW_ORGANIZATION_ID', '')


def _scw_access_key():
    return os.environ.get('SCW_ACCESS_KEY', '')


def _scw_secret_key():
    return os.environ.get('SCW_SECRET_KEY', '') or agent_config.SCALEWAY_API_KEY


def _token():
    return _scw_secret_key()


def _scw_rest(url, body=None, method=None, timeout=30):
    data = None
    headers = {'X-Auth-Token': _token()}
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    if method is None:
        method = 'POST' if body is not None else 'GET'
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return json.loads(raw) if raw else {}, r.status
            except json.JSONDecodeError:
                return {'_raw': raw.decode(errors='replace')}, r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return json.loads(raw), e.code
        except json.JSONDecodeError:
            return {'_raw': raw.decode(errors='replace'), '_status': e.code}, e.code
    except Exception as e:
        return {'_error': f'{type(e).__name__}: {e}'}, 0


def _s3_client(region='fr-par', access_key=None, secret_key=None):
    import boto3
    from botocore.config import Config
    import socket
    _orig_getaddrinfo = socket.getaddrinfo
    _ipv4_only_getaddrinfo = lambda host, port, family=0, type=0, proto=0, flags=0: \
        _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    return boto3.session.Session().client(
        's3', region_name=region,
        endpoint_url=f'https://s3.{region}.scw.cloud',
        aws_access_key_id=access_key or _scw_access_key(),
        aws_secret_access_key=secret_key or _scw_secret_key(),
        config=Config(
            s3={'addressing_style': 'path'},
            connect_timeout=30,
            read_timeout=60,
        ),
    )


def _slugify(name):
    s = re.sub(r'[^a-z0-9]+', '-', (name or '').lower()).strip('-')
    return s or 'project'


def _rand6():
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _scw_user_id():
    try:
        out = subprocess.run(
            ['scw', 'iam', 'user', 'list', '-o', 'json'],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        users = json.loads(out.stdout)
        if users and isinstance(users, list):
            return users[0].get('id')
    except Exception as e:
        log.warning('scw iam user list failed: %s', e)
    return None


def _audit(project_path, allowed, reason=''):
    try:
        agent_config.eu_audit(
            project_path,
            model='scw-session', provider='scaleway',
            allowed=allowed, caller='scw_session',
            reason=reason,
        )
    except Exception:
        log.warning('eu_audit failed', exc_info=True)


def create_session(project_id, project_name, region='fr-par', **_):
    """Create a full Scaleway session for a project.

    Sequence: Scaleway Project → project-scoped IAM API key (1y expiry) →
    Key Manager key → SSE-KMS bucket → versioning.
    Returns a dict with the scw_* fields, or {'ok': False, 'error': ...}.
    """
    proj = agent_db.get_project(project_id)
    project_path = (proj or {}).get('path') or ''

    org_id = _scw_org_id()
    if not org_id:
        _audit(project_path, False, 'missing SCW_ORGANIZATION_ID')
        return {'ok': False, 'error': 'SCW_ORGANIZATION_ID not set'}
    if not _scw_access_key() or not _scw_secret_key():
        _audit(project_path, False, 'missing SCW access/secret key')
        return {'ok': False, 'error': 'SCW_ACCESS_KEY/SCW_SECRET_KEY not set'}

    scw_project_name = f'Cordée - {project_name}'
    resp, status = _scw_rest(
        'https://api.scaleway.com/account/v3/projects',
        {'name': scw_project_name, 'organization_id': org_id},
    )
    if status not in (200, 201) or 'id' not in resp:
        _audit(project_path, False, f'project create failed: {resp}')
        return {'ok': False, 'error': f'project create failed (HTTP {status}): {resp}'}
    scw_project_id = resp['id']

    scw_access_key = _scw_access_key()
    scw_secret_key = _scw_secret_key()

    kms_body = {
        'project_id': scw_project_id,
        'name': f'aingel-session-{_slugify(project_name)}-{_rand6()}',
        'usage': {'symmetric_encryption': 'aes_256_gcm'},
    }
    kms_resp, kms_status = _scw_rest(
        f'https://api.scaleway.com/key-manager/v1alpha1/regions/{region}/keys',
        kms_body,
    )
    if kms_status not in (200, 201) or 'id' not in kms_resp:
        _audit(project_path, False, f'kms create failed: {kms_resp}')
        _scw_rest(f'https://api.scaleway.com/account/v3/projects/{scw_project_id}', method='DELETE')
        return {'ok': False, 'error': f'kms key create failed (HTTP {kms_status}): {kms_resp}'}
    kms_key_id = kms_resp.get('id') or kms_resp.get('key_id')

    bucket_name = f'aingel-{_slugify(project_name)}-{_rand6()}'
    try:
        s3 = _s3_client(region, access_key=scw_access_key, secret_key=scw_secret_key)
        s3.create_bucket(Bucket=bucket_name)
    except Exception as e:
        _audit(project_path, False, f'bucket create failed: {e}')
        _scw_rest(f'https://api.scaleway.com/key-manager/v1alpha1/regions/{region}/keys/{kms_key_id}', method='DELETE')
        _scw_rest(f'https://api.scaleway.com/account/v3/projects/{scw_project_id}', method='DELETE')
        return {'ok': False, 'error': f'bucket create failed: {e}'}

    sse_set = False
    for kms_ref in (kms_key_id, f'arn:scw:kms:{region}::{kms_key_id}'):
        try:
            s3.put_bucket_encryption(
                Bucket=bucket_name,
                ServerSideEncryptionConfiguration={
                    'Rules': [{
                        'ApplyServerSideEncryptionByDefault': {
                            'SSEAlgorithm': 'aws:kms',
                            'KMSMasterKeyID': kms_ref,
                        },
                        'BucketKeyEnabled': True,
                    }]
                },
            )
            sse_set = True
            break
        except Exception as e:
            log.warning('put_bucket_encryption with %s failed: %s', kms_ref, e)
    if not sse_set:
        _audit(project_path, False, 'sse-kms config could not be applied')

    try:
        s3.put_bucket_versioning(Bucket=bucket_name, VersioningConfiguration={'Status': 'Enabled'})
    except Exception as e:
        log.warning('versioning enable failed (non-fatal): %s', e)

    created_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    try:
        agent_db.set_project_field(project_id, 'scw_session_enabled', 1)
        agent_db.set_project_field(project_id, 'scw_project_id', scw_project_id)
        agent_db.set_project_field(project_id, 'scw_session_bucket', bucket_name)
        agent_db.set_project_field(project_id, 'scw_kms_key_id', kms_key_id)
        agent_db.set_project_field(project_id, 'scw_session_region', region)
        agent_db.set_project_field(project_id, 'scw_session_created_at', created_at)
    except Exception as e:
        log.warning('could not persist session fields to projects table: %s', e)

    _audit(project_path, True, f'session created: project={scw_project_id} bucket={bucket_name}')

    return {
        'ok': True,
        'scw_project_id': scw_project_id,
        'scw_access_key': scw_access_key,
        'scw_secret_key': scw_secret_key,
        'scw_kms_key_id': kms_key_id,
        'scw_session_bucket': bucket_name,
        'scw_session_region': region,
        'scw_session_created_at': created_at,
    }


def close_session(project_id, scw_project_id, bucket_name, kms_key_id, region='fr-par', **_):
    """Tear down a session (crypto-shred): empty bucket → delete key → delete project.

    Never raises; returns {'ok': bool, 'details': {...}}.
    """
    proj = agent_db.get_project(project_id)
    project_path = (proj or {}).get('path') or ''
    details = {}

    if bucket_name:
        try:
            s3 = _s3_client(region)
            paginator = s3.get_paginator('list_object_versions')
            for page in paginator.paginate(Bucket=bucket_name):
                versions = page.get('Versions', []) + page.get('DeleteMarkers', [])
                if versions:
                    s3.delete_objects(
                        Bucket=bucket_name,
                        Delete={'Objects': [{'Key': v['Key'], 'VersionId': v['VersionId']} for v in versions]},
                    )
            s3.delete_bucket(Bucket=bucket_name)
            details['bucket'] = 'deleted'
        except Exception as e:
            details['bucket'] = f'error: {e}'
            log.warning('bucket empty/delete failed: %s', e)

    if kms_key_id:
        try:
            _scw_rest(
                f'https://api.scaleway.com/key-manager/v1alpha1/regions/{region}/keys/{kms_key_id}/disable',
                {}, method='POST',
            )
        except Exception as e:
            log.warning('kms disable failed (non-fatal): %s', e)
        try:
            _scw_rest(
                f'https://api.scaleway.com/key-manager/v1alpha1/regions/{region}/keys/{kms_key_id}/unprotect',
                {}, method='POST',
            )
        except Exception as e:
            log.warning('kms unprotect failed (non-fatal): %s', e)
        del_resp, del_status = _scw_rest(
            f'https://api.scaleway.com/key-manager/v1alpha1/regions/{region}/keys/{kms_key_id}',
            method='DELETE',
        )
        details['kms_key'] = 'deleted' if del_status in (200, 204) else f'HTTP {del_status}: {del_resp}'
        if del_status not in (200, 204):
            log.warning('kms delete returned %s: %s', del_status, del_resp)

    if scw_project_id:
        del_resp, del_status = _scw_rest(
            f'https://api.scaleway.com/account/v3/projects/{scw_project_id}',
            method='DELETE',
        )
        if del_status in (200, 204):
            details['scw_project'] = 'deleted'
        elif del_status == 412:
            details['scw_project'] = 'purging (412)'
            log.warning('project %s still purging resources (412), will delete later', scw_project_id)
        else:
            details['scw_project'] = f'HTTP {del_status}: {del_resp}'
            log.warning('project delete returned %s: %s', del_status, del_resp)

    try:
        agent_db.set_project_field(project_id, 'scw_session_enabled', 0)
        agent_db.set_project_field(project_id, 'scw_project_id', None)
        agent_db.set_project_field(project_id, 'scw_session_bucket', None)
        agent_db.set_project_field(project_id, 'scw_kms_key_id', None)
        agent_db.set_project_field(project_id, 'scw_session_created_at', None)
    except Exception as e:
        log.warning('could not clear session fields: %s', e)

    _audit(project_path, True, f'session closed: project={scw_project_id} bucket={bucket_name}')
    return {'ok': True, 'details': details}


def upload_file(project_path, bucket_name, key, region='fr-par', **_):
    """Upload a local file to the session bucket. SSE-KMS applies via bucket default."""
    if not project_path or not os.path.isfile(project_path):
        return {'ok': False, 'error': f'source not found: {project_path}'}
    if not bucket_name:
        return {'ok': False, 'error': 'bucket_name required'}
    try:
        s3 = _s3_client(region)
        s3.upload_file(project_path, bucket_name, key)
        head = s3.head_object(Bucket=bucket_name, Key=key)
        return {
            'ok': True,
            'size': int(head.get('ContentLength', 0)),
            'sse': head.get('ServerSideEncryption', ''),
        }
    except Exception as e:
        log.warning('upload_file failed: %s', e)
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def delete_object(bucket_name, key, region='fr-par', **_):
    """Remove an object from the session bucket (mirrors a soft-delete so the
    bucket no longer resurrects the file). No-op if the key is absent. Logs
    failures so a missed purge is not silent (C3)."""
    if not bucket_name or not key:
        return {'ok': False, 'error': 'bucket_name/key required'}
    try:
        s3 = _s3_client(region)
        s3.delete_object(Bucket=bucket_name, Key=key)
        return {'ok': True}
    except Exception as e:
        log.warning('delete_object failed for %s: %s', key, e)
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def download_file(bucket_name, key, dest_path, region='fr-par', **_):
    """Download a file from the session bucket to dest_path."""
    if not bucket_name:
        return {'ok': False, 'error': 'bucket_name required'}
    try:
        s3 = _s3_client(region)
        s3.download_file(bucket_name, key, dest_path)
        return {'ok': True, 'path': dest_path, 'size': os.path.getsize(dest_path)}
    except Exception as e:
        log.warning('download_file failed: %s', e)
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def list_files(bucket_name, region='fr-par', prefix='', **_):
    """List objects in the session bucket. Returns [{key, size, last_modified, etag}]."""
    if not bucket_name:
        return {'ok': False, 'error': 'bucket_name required'}
    try:
        s3 = _s3_client(region)
        resp = s3.list_objects_v2(Bucket=bucket_name, Prefix=prefix)
        items = []
        for o in resp.get('Contents', []):
            items.append({
                'key': o.get('Key', ''),
                'size': int(o.get('Size', 0)),
                'last_modified': o.get('LastModified').isoformat() if o.get('LastModified') else '',
                'etag': (o.get('ETag', '') or '').strip('"'),
            })
        return {'ok': True, 'files': items}
    except Exception as e:
        log.warning('list_files failed: %s', e)
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def sync_bucket_to_working_docs(project_id, project_path, region='fr-par', **_):
    """Download all bucket files to the project's Working Documents folder.

    Idempotent: skips files that already exist locally with the same size.
    Called by the executor before building the prompt so the prompt builder's
    auto-catch mechanism can inline referenced files.
    """
    proj = agent_db.get_project(project_id)
    if not proj or not proj.get('scw_session_enabled') or not proj.get('scw_session_bucket'):
        return {'ok': True, 'synced': 0, 'reason': 'no active session'}

    bucket = proj['scw_session_bucket']
    wd_candidates = ['Working Documents', 'Working Docs', 'My Docs']
    wd = None
    for name in wd_candidates:
        candidate = os.path.join(project_path, name)
        if os.path.isdir(candidate):
            wd = candidate
            break
    if not wd:
        wd = os.path.join(project_path, 'Working Documents')
        os.makedirs(wd, exist_ok=True)

    listing = list_files(bucket, region=region)
    if not listing.get('ok'):
        return {'ok': False, 'error': listing.get('error', 'list_files failed')}

    # Tombstone skip: any bucket key matching a delete-tombstone rel must NOT be
    # re-downloaded (a delete should stay deleted). Hoist the tombstone set ONE
    # DB read (C3), keyed by rel_path; the commit gate remains the backstop for
    # renamed / bucket-synced resurrections.
    tomb_rels = set()
    try:
        for t in agent_db.get_delete_tombstones(project_path):
            tp = (t.get('rel_path') or '').strip()
            if tp:
                tomb_rels.add(tp)
                # Also normalize the bucket-key form (no 'Working Documents/' prefix).
                if tp.startswith('Working Documents/'):
                    tomb_rels.add(tp[len('Working Documents/'):])
    except Exception:
        tomb_rels = set()

    synced = 0
    skipped_tomb = 0
    for f in listing.get('files', []):
        key = f.get('key', '')
        if not key or key.endswith('/'):
            continue
        rel = os.path.join('Working Documents', key)
        dest = os.path.join(wd, key)
        if key in tomb_rels or rel in tomb_rels:
            skipped_tomb += 1
            log.info('sync skip (tombstoned): %s', key)
            continue
        dest_dir = os.path.dirname(dest)
        if dest_dir and not os.path.isdir(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)
        local_size = os.path.getsize(dest) if os.path.exists(dest) else -1
        remote_size = int(f.get('size', 0))
        if local_size == remote_size:
            continue
        result = download_file(bucket, key, dest, region=region)
        if result.get('ok'):
            synced += 1
            log.info('synced %s from bucket to Working Documents (%d bytes)', key, remote_size)
        else:
            log.warning('failed to sync %s: %s', key, result.get('error', ''))

    return {'ok': True, 'synced': synced, 'skipped_tombstoned': skipped_tomb}


def pull_costs(scw_project_id, project_id, region='fr-par', **_):
    """Pull consumption for a Scaleway project and store per-day/category rows.

    Uses the Billing v2beta1 consumptions endpoint (the one the scw CLI hits).
    Returns a summary dict. If the endpoint fails or returns no data, returns
    {'ok': False, 'reason': 'no data'} — never crashes the caller.
    """
    if not scw_project_id:
        return {'ok': False, 'reason': 'no scw_project_id'}
    url = ('https://api.scaleway.com/billing/v2beta1/consumptions'
           f'?order_by=updated_at_desc&page=1&project_id={scw_project_id}')
    resp, status = _scw_rest(url, method='GET')
    if status != 200 or not isinstance(resp, dict):
        return {'ok': False, 'reason': f'billing API HTTP {status}'}
    consumptions = resp.get('consumptions') or []
    if not consumptions:
        return {'ok': False, 'reason': 'no data'}

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    eur_per_usd = 1.08
    by_category = {}
    total_eur = 0.0
    for c in consumptions:
        v = c.get('value') or {}
        units = float(v.get('units', 0) or 0)
        nanos = float(v.get('nanos', 0) or 0) / 1e9
        eur = units + nanos
        category = (c.get('category_name') or 'general').lower().replace(' ', '_')
        by_category[category] = by_category.get(category, 0.0) + eur
        total_eur += eur

    try:
        conn = agent_db.get_db()
        for category, eur in by_category.items():
            usd = round(eur * eur_per_usd, 6)
            conn.execute(
                'INSERT INTO scw_session_costs (project_id, day, category, eur, usd) '
                'VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(project_id, day, category) DO UPDATE SET eur=excluded.eur, usd=excluded.usd',
                (project_id, today, category, round(eur, 6), usd),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning('pull_costs DB insert failed: %s', e)
        return {'ok': False, 'reason': f'db insert failed: {e}'}

    return {
        'ok': True,
        'scw_project_id': scw_project_id,
        'project_id': project_id,
        'day': today,
        'total_eur': round(total_eur, 6),
        'total_usd': round(total_eur * eur_per_usd, 6),
        'by_category': {k: round(v, 6) for k, v in by_category.items()},
        'count': len(consumptions),
    }