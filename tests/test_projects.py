"""
Tests for POST/GET/PATCH/DELETE /api/projects
and API key management endpoints.
"""

import pytest
from tests.conftest import auth


# ── Create ────────────────────────────────────────────────────────────────────

class TestCreateProject:
    def test_create_returns_201(self, client):
        resp = client.post('/api/projects', json={'name': 'Alpha'})
        assert resp.status_code == 201

    def test_create_returns_id_and_key(self, client):
        resp = client.post('/api/projects', json={'name': 'Beta', 'description': 'desc'})
        data = resp.get_json()
        assert 'id' in data
        assert 'api_key' in data
        assert data['api_key'].startswith('sk-casi-')
        assert data['name'] == 'Beta'
        assert data['description'] == 'desc'
        # Cleanup
        client.delete(f'/api/projects/{data["id"]}')

    def test_create_without_name_returns_400(self, client):
        resp = client.post('/api/projects', json={})
        assert resp.status_code == 400
        assert 'name' in resp.get_json()['error']

    def test_create_with_empty_name_returns_400(self, client):
        resp = client.post('/api/projects', json={'name': '   '})
        assert resp.status_code == 400

    def test_create_without_body_returns_400(self, client):
        resp = client.post('/api/projects')
        assert resp.status_code == 400


# ── List ──────────────────────────────────────────────────────────────────────

class TestListProjects:
    def test_list_returns_200(self, client):
        resp = client.get('/api/projects')
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_created_project_appears_in_list(self, client, project):
        pid, _ = project
        resp = client.get('/api/projects')
        ids = [p['id'] for p in resp.get_json()]
        assert pid in ids


# ── Get ───────────────────────────────────────────────────────────────────────

class TestGetProject:
    def test_get_existing(self, client, project):
        pid, _ = project
        resp = client.get(f'/api/projects/{pid}')
        assert resp.status_code == 200
        assert resp.get_json()['id'] == pid

    def test_get_nonexistent_returns_404(self, client):
        resp = client.get('/api/projects/00000000-0000-0000-0000-000000000000')
        assert resp.status_code == 404


# ── Update ────────────────────────────────────────────────────────────────────

class TestUpdateProject:
    def test_patch_name(self, client, project):
        pid, _ = project
        resp = client.patch(f'/api/projects/{pid}', json={'name': 'Renamed'})
        assert resp.status_code == 200
        assert resp.get_json()['name'] == 'Renamed'

    def test_patch_description(self, client, project):
        pid, _ = project
        resp = client.patch(f'/api/projects/{pid}', json={'description': 'Updated desc'})
        assert resp.status_code == 200
        assert resp.get_json()['description'] == 'Updated desc'

    def test_patch_nonexistent_returns_404(self, client):
        resp = client.patch(
            '/api/projects/00000000-0000-0000-0000-000000000000',
            json={'name': 'Ghost'},
        )
        assert resp.status_code == 404


# ── Delete ────────────────────────────────────────────────────────────────────

class TestDeleteProject:
    def test_delete_returns_200(self, client):
        resp = client.post('/api/projects', json={'name': 'ToDelete'})
        pid = resp.get_json()['id']
        del_resp = client.delete(f'/api/projects/{pid}')
        assert del_resp.status_code == 200
        assert del_resp.get_json()['deleted'] == pid

    def test_deleted_project_is_gone(self, client):
        resp = client.post('/api/projects', json={'name': 'ToDelete2'})
        pid = resp.get_json()['id']
        client.delete(f'/api/projects/{pid}')
        get_resp = client.get(f'/api/projects/{pid}')
        assert get_resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete('/api/projects/00000000-0000-0000-0000-000000000000')
        assert resp.status_code == 404


# ── API Keys ──────────────────────────────────────────────────────────────────

class TestApiKeys:
    def test_list_keys_shows_one_on_create(self, client, project):
        pid, _ = project
        resp = client.get(f'/api/projects/{pid}/keys')
        assert resp.status_code == 200
        keys = resp.get_json()
        assert len(keys) == 1
        # Never expose raw key in list
        assert 'key_hash' not in keys[0]
        assert 'prefix' in keys[0]

    def test_create_new_key(self, client, project):
        pid, _ = project
        resp = client.post(f'/api/projects/{pid}/keys')
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['key'].startswith('sk-casi-')
        assert 'prefix' in data
        # Now two keys exist
        list_resp = client.get(f'/api/projects/{pid}/keys')
        assert len(list_resp.get_json()) == 2

    def test_new_key_authenticates(self, client, project):
        pid, _ = project
        new_key = client.post(f'/api/projects/{pid}/keys').get_json()['key']
        resp = client.get(
            f'/api/projects/{pid}/runs',
            headers={'X-CASI-Key': new_key},
        )
        assert resp.status_code == 200

    def test_revoke_all_keys(self, client, project):
        pid, _ = project
        client.delete(f'/api/projects/{pid}/keys')
        list_resp = client.get(f'/api/projects/{pid}/keys')
        assert list_resp.get_json() == []

    def test_revoked_key_no_longer_authenticates(self, client, project):
        pid, api_key = project
        client.delete(f'/api/projects/{pid}/keys')
        resp = client.get(
            f'/api/projects/{pid}/runs',
            headers={'X-CASI-Key': api_key},
        )
        assert resp.status_code == 401
