"""
Tests for API key authentication middleware.
Covers every path through routes/auth.py.
"""

import pytest
from tests.conftest import auth, requires_firebase, DEV_MODE


class TestAuthMiddleware:

    @requires_firebase
    def test_no_header_returns_401(self, client, project):
        """In production (Firebase configured), no header → 401.
        Skipped in dev mode where the auth bypass lets requests through."""
        pid, _ = project
        resp = client.get(f'/api/projects/{pid}/runs')
        assert resp.status_code == 401

    @requires_firebase
    def test_empty_header_returns_401(self, client, project):
        pid, _ = project
        resp = client.get(f'/api/projects/{pid}/runs', headers={'X-CASI-Key': ''})
        assert resp.status_code == 401

    @requires_firebase
    def test_whitespace_header_returns_401(self, client, project):
        pid, _ = project
        resp = client.get(f'/api/projects/{pid}/runs', headers={'X-CASI-Key': '   '})
        assert resp.status_code == 401

    def test_garbage_key_returns_401(self, client, project):
        """Explicit garbage API key always fails regardless of dev mode."""
        pid, _ = project
        resp = client.get(
            f'/api/projects/{pid}/runs',
            headers={'X-CASI-Key': 'not-a-real-key'},
        )
        assert resp.status_code == 401
        assert 'Invalid' in resp.get_json()['error']

    def test_valid_key_returns_200(self, client, project):
        pid, api_key = project
        resp = client.get(f'/api/projects/{pid}/runs', headers=auth(api_key))
        assert resp.status_code == 200

    def test_key_for_different_project_recompute_returns_403(self, client, project):
        """API key must not trigger a write on a different project.
        Uses POST /recompute (a write route that calls _check_project)."""
        pid, api_key = project
        resp2 = client.post('/api/projects', json={'name': 'Project B'})
        pid2 = resp2.get_json()['id']
        try:
            resp = client.post(
                f'/api/projects/{pid2}/recompute',
                headers=auth(api_key),   # key belongs to pid, not pid2
            )
            assert resp.status_code == 403
        finally:
            client.delete(f'/api/projects/{pid2}')

    def test_last_used_updated_on_valid_auth(self, client, project):
        """Successful authentication should update last_used on the key row."""
        pid, api_key = project
        client.get(f'/api/projects/{pid}/runs', headers=auth(api_key))
        keys = client.get(f'/api/projects/{pid}/keys').get_json()
        assert keys[0]['last_used'] is not None

    def test_revoked_key_returns_401(self, client, project):
        pid, api_key = project
        client.delete(f'/api/projects/{pid}/keys')
        resp = client.get(f'/api/projects/{pid}/runs', headers=auth(api_key))
        assert resp.status_code == 401

    def test_legacy_upload_removed(self, client):
        """Legacy /api/upload endpoint has been removed — should 404."""
        resp = client.post('/api/upload')
        assert resp.status_code == 404

    def test_auth_not_required_for_health(self, client):
        resp = client.get('/api/health')
        assert resp.status_code == 200

    def test_auth_not_required_for_projects_list(self, client):
        resp = client.get('/api/projects')
        assert resp.status_code == 200

    def test_dev_mode_bypass_when_no_firebase(self, client, project):
        """In dev mode (no Firebase), requests with no auth header are allowed."""
        if not DEV_MODE:
            pytest.skip('Only meaningful when Firebase is not configured')
        pid, _ = project
        resp = client.get(f'/api/projects/{pid}/runs')
        assert resp.status_code == 200
