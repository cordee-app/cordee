"""Security regression tests for agent_webdav auth.

Context: the Authentik proxy outpost used to sit in front of the app and both
inject and sanitize forward-auth identity headers (X-Authentik-*, X-Forwarded-*).
It was removed from the request path on 2026-09-17 in favour of in-app OIDC.
That made those headers client-forgeable, so `_check_auth` must ignore them
unless AINGEL_DAV_TRUST_HEADERS explicitly says a trusted proxy is in front.

Without the fix these tests fail with full unauthenticated WebDAV access.
"""
import os
import unittest
from contextlib import contextmanager

from flask import Flask, request

import agent_webdav


@contextmanager
def _request_ctx(headers=None, remote_addr='203.0.113.10'):
    """Yield the Flask request inside an active context (public client IP)."""
    app = Flask(__name__)
    with app.test_request_context(
            '/', headers=headers or {},
            environ_overrides={'REMOTE_ADDR': remote_addr}):
        yield request


def _check(headers=None, remote_addr='203.0.113.10'):
    """Run _check_auth inside a live request context."""
    with _request_ctx(headers=headers, remote_addr=remote_addr) as req:
        return agent_webdav._check_auth(req)


class ForwardAuthHeaderSpoofingTests(unittest.TestCase):
    """Spoofed identity headers must NOT authenticate by default."""

    def setUp(self):
        self._saved = {
            k: os.environ.get(k)
            for k in ('AINGEL_DAV_TOKEN', 'AINGEL_DAV_TRUST_HEADERS')
        }
        os.environ['AINGEL_DAV_TOKEN'] = 'sekrit-dav-token'
        os.environ.pop('AINGEL_DAV_TRUST_HEADERS', None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_each_forward_auth_header_is_ignored_by_default(self):
        for header in agent_webdav.AUTH_HEADERS:
            self.assertFalse(
                _check(headers={header: 'attacker'}),
                f'{header} must not authenticate a public request by default')

    def test_no_credentials_is_denied(self):
        self.assertFalse(_check())

    def test_cloudflare_proxied_spoofed_header_is_denied(self):
        self.assertFalse(_check(
            headers={'X-Authentik-Username': 'attacker', 'Cf-Ray': 'abc123'}))

    def test_valid_token_still_authenticates(self):
        self.assertTrue(_check(headers={'Authorization': 'Bearer sekrit-dav-token'}))

    def test_wrong_token_is_denied(self):
        self.assertFalse(_check(headers={'Authorization': 'Bearer nope'}))

    def test_headers_honoured_only_when_explicitly_trusted(self):
        os.environ['AINGEL_DAV_TRUST_HEADERS'] = '1'
        self.assertTrue(_check(headers={'X-Authentik-Username': 'admin'}))

    def test_loopback_without_token_is_denied(self):
        # Agent subprocesses run on this host; loopback must not bypass auth.
        self.assertFalse(_check(remote_addr='127.0.0.1'))
        self.assertFalse(_check(remote_addr='::1'))

    def test_loopback_with_token_authenticates(self):
        self.assertTrue(_check(headers={'Authorization': 'Bearer sekrit-dav-token'},
                               remote_addr='127.0.0.1'))

    def test_no_configured_token_denies_everything(self):
        os.environ.pop('AINGEL_DAV_TOKEN', None)
        self.assertFalse(_check(headers={'Authorization': 'Bearer anything'}))
        self.assertFalse(_check(remote_addr='127.0.0.1'))

    def test_loopback_with_cf_ray_requires_real_auth(self):
        self.assertFalse(_check(headers={'Cf-Ray': 'abc123'}, remote_addr='127.0.0.1'))



class PutWriteFailureTests(unittest.TestCase):
    """A PUT whose write fails must leave the existing file untouched (C5)."""

    def setUp(self):
        import tempfile
        from unittest import mock
        self._saved = os.environ.get('AINGEL_DAV_TOKEN')
        os.environ['AINGEL_DAV_TOKEN'] = 'sekrit-dav-token'
        self.td = tempfile.TemporaryDirectory()
        self.p = self.td.name
        os.makedirs(os.path.join(self.p, 'Working Documents'))
        self.target = os.path.join(self.p, 'Working Documents', 'doc.txt')
        with open(self.target, 'w') as f:
            f.write('original content')
        proj = {'id': 1, 'path': self.p, 'slug': 'proj'}
        self._patch = mock.patch('agent_webdav._get_project_by_slug', return_value=proj)
        self._patch.start()
        self.app = agent_webdav.create_standalone_app()

    def tearDown(self):
        self._patch.stop()
        self.td.cleanup()
        if self._saved is None:
            os.environ.pop('AINGEL_DAV_TOKEN', None)
        else:
            os.environ['AINGEL_DAV_TOKEN'] = self._saved

    def test_failed_write_keeps_original_and_returns_500(self):
        from unittest import mock
        real_fdopen = os.fdopen

        class _Failing:
            def __init__(self, fd, *a, **k):
                self._f = real_fdopen(fd, *a, **k)
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self._f.close()
                return False
            def write(self, _data):
                raise OSError(28, 'No space left on device')

        with mock.patch('agent_webdav.os.fdopen', _Failing):
            resp = self.app.test_client().put(
                '/dav/proj/Working Documents/doc.txt', data=b'new content',
                headers={'Authorization': 'Bearer sekrit-dav-token'})
        self.assertEqual(resp.status_code, 500)
        with open(self.target) as f:
            self.assertEqual(f.read(), 'original content')
        leftovers = [n for n in os.listdir(os.path.dirname(self.target)) if n.startswith('.aingel-put-')]
        self.assertEqual(leftovers, [])


if __name__ == '__main__':
    unittest.main()
