"""Unit tests for agent_auth public-URL handling (Phase 6.5).

Focus: OIDC redirect URIs must use the configured external origin when the app
runs behind a TLS-terminating tunnel (Cloudflare → cloudflared → Flask over
plain HTTP), otherwise ``url_for(..., _external=True)`` emits ``http://`` and
Authentik's strict redirect-URI matching rejects the authorization request.

These tests exercise the pure helpers and the login/logout route wiring with a
minimal Flask app, so they never touch the real database or an IdP.
"""
import os
import unittest

from flask import Flask


class PublicUrlHelperTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get('AINGEL_PUBLIC_URL')
        os.environ.pop('AINGEL_PUBLIC_URL', None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop('AINGEL_PUBLIC_URL', None)
        else:
            os.environ['AINGEL_PUBLIC_URL'] = self._saved

    def test_unset_returns_none(self):
        import agent_auth
        self.assertIsNone(agent_auth.public_base_url())

    def test_empty_returns_none(self):
        import agent_auth
        os.environ['AINGEL_PUBLIC_URL'] = '   '
        self.assertIsNone(agent_auth.public_base_url())

    def test_strips_trailing_slash(self):
        import agent_auth
        os.environ['AINGEL_PUBLIC_URL'] = 'https://cordee.example/'
        self.assertEqual(
            agent_auth.public_base_url(), 'https://cordee.example')

    def test_callback_url_uses_public_origin(self):
        import agent_auth
        os.environ['AINGEL_PUBLIC_URL'] = 'https://cordee.example'
        app = Flask(__name__)
        with app.test_request_context('/', base_url='http://127.0.0.1:8001/'):
            self.assertEqual(
                agent_auth._callback_url(),
                'https://cordee.example/api/auth/callback')

    def test_callback_url_falls_back_to_request(self):
        import agent_auth
        app = Flask(__name__)
        app.secret_key = 'test'
        app.register_blueprint(agent_auth.auth_bp)
        with app.test_request_context('/', base_url='http://localhost:8001/'):
            self.assertEqual(
                agent_auth._callback_url(),
                'http://localhost:8001/api/auth/callback')

    def test_post_logout_url_uses_public_origin(self):
        import agent_auth
        os.environ['AINGEL_PUBLIC_URL'] = 'https://cordee.example'
        app = Flask(__name__)
        with app.test_request_context('/', base_url='http://127.0.0.1:8001/'):
            self.assertEqual(
                agent_auth._post_logout_url(),
                'https://cordee.example/?loggedout=1')

    def test_post_logout_url_falls_back_to_request(self):
        import agent_auth
        app = Flask(__name__)
        with app.test_request_context('/', base_url='http://localhost:8001/'):
            self.assertEqual(
                agent_auth._post_logout_url(),
                'http://localhost:8001/?loggedout=1')


class LogoutRouteTests(unittest.TestCase):
    """The logout route returns an end-session URL carrying the public origin."""

    def setUp(self):
        self._saved = {
            k: os.environ.get(k) for k in
            ('AINGEL_AUTH', 'AINGEL_PUBLIC_URL', 'AINGEL_OIDC_ISSUER',
             'AINGEL_SESSION_SECRET', 'AINGEL_OIDC_CLIENT_ID')
        }
        os.environ.pop('AINGEL_OIDC_CLIENT_ID', None)
        os.environ['AINGEL_AUTH'] = 'oidc'
        os.environ['AINGEL_SESSION_SECRET'] = 'test-secret-' + 'x' * 32
        os.environ['AINGEL_OIDC_ISSUER'] = 'https://auth.example.com/application/o/aingel/'
        os.environ['AINGEL_PUBLIC_URL'] = 'https://cordee.example'

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _app(self):
        import agent_auth
        app = Flask(__name__)
        app.secret_key = 'test'
        app.register_blueprint(agent_auth.auth_bp)
        return app

    def test_logout_includes_public_post_logout_redirect(self):
        app = self._app()
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['uid'] = 1
                sess['id_token'] = 'header.payload.sig'
            resp = client.post('/api/auth/logout')
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertIn('end-session', payload['end_session_url'])
            self.assertIn(
                'post_logout_redirect_uri=https%3A%2F%2Fcordee.example%2F%3Floggedout%3D1',
                payload['end_session_url'])
            self.assertIn('id_token_hint=header.payload.sig', payload['end_session_url'])
            self.assertNotIn('client_id=', payload['end_session_url'])
            with client.session_transaction() as sess:
                self.assertNotIn('id_token', sess)

    def test_logout_without_id_token_omits_redirect(self):
        # Authentik rejects post_logout_redirect_uri without id_token_hint.
        app = self._app()
        with app.test_client() as client:
            payload = client.post('/api/auth/logout').get_json()
            self.assertIn('end-session', payload['end_session_url'])
            self.assertNotIn('post_logout_redirect_uri', payload['end_session_url'])
            self.assertNotIn('id_token_hint', payload['end_session_url'])

    def test_logout_identifies_client_when_configured(self):
        os.environ['AINGEL_OIDC_CLIENT_ID'] = 'cordee-client'
        app = self._app()
        with app.test_client() as client:
            payload = client.post('/api/auth/logout').get_json()
            self.assertIn('client_id=cordee-client', payload['end_session_url'])


class OidcPkceConfigTests(unittest.TestCase):
    """PKCE must be configured on the authlib client, not passed as a stray
    authorize kwarg. Regression: passing code_challenge_method as a kwarg made
    the authorize URL declare S256 without a code_challenge, so Authentik
    accepted the login but rejected the token exchange (token_exchange_failed).
    """

    def setUp(self):
        self._saved = {
            k: os.environ.get(k) for k in
            ('AINGEL_OIDC_ISSUER', 'AINGEL_OIDC_CLIENT_ID',
             'AINGEL_OIDC_CLIENT_SECRET')
        }
        os.environ['AINGEL_OIDC_ISSUER'] = 'https://auth.example.com/application/o/aingel/'
        os.environ['AINGEL_OIDC_CLIENT_ID'] = 'test-client'
        os.environ['AINGEL_OIDC_CLIENT_SECRET'] = 'test-secret'

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_registration_sets_code_challenge_method_on_client(self):
        import agent_auth
        from unittest.mock import patch

        captured = {}

        def fake_register(name, **kwargs):
            captured.update(kwargs)

        with patch.object(agent_auth.oauth, 'create_client', return_value=None), \
                patch.object(agent_auth.oauth, 'register', side_effect=fake_register):
            agent_auth._oidc_client()

        client_kwargs = captured.get('client_kwargs', {})
        self.assertEqual(client_kwargs.get('code_challenge_method'), 'S256',
                         'PKCE method must be set on the client for authlib to '
                         'compute code_challenge and persist code_verifier')


if __name__ == '__main__':
    unittest.main()
