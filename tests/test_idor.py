"""
IDOR (Insecure Direct Object Reference) tests.

Verify that an authenticated user holding an API key for project A
cannot read or write data belonging to project B.

In dev mode (no Firebase), two separate API keys are used — one per project.
"""

import pytest
from tests.conftest import auth


@pytest.fixture
def two_projects(client):
    """Create two independent projects, yield (pid_a, key_a, pid_b, key_b)."""
    a = client.post('/api/projects', json={'name': 'Owner A'}).get_json()
    b = client.post('/api/projects', json={'name': 'Owner B'}).get_json()
    yield a['id'], a['api_key'], b['id'], b['api_key']
    client.delete(f"/api/projects/{a['id']}")
    client.delete(f"/api/projects/{b['id']}")


class TestIDOR:
    """
    Using project B's API key must not grant access to project A's data.

    For API key auth: returns 403 (key is valid but bound to a different project).
    For Firebase auth: returns 404 (existence must not be confirmed to unauthorised callers).
    Both are acceptable — the test uses API keys so 403 is the expected signal.
    """

    def test_get_project_other_owner_returns_403_or_404(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(f'/api/projects/{pid_a}', headers=auth(key_b))
        assert resp.status_code in (403, 404)

    def test_list_runs_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(f'/api/projects/{pid_a}/runs', headers=auth(key_b))
        assert resp.status_code in (403, 404)

    def test_latest_run_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(f'/api/projects/{pid_a}/runs/latest', headers=auth(key_b))
        assert resp.status_code in (403, 404)

    def test_trend_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(f'/api/projects/{pid_a}/trend', headers=auth(key_b))
        assert resp.status_code in (403, 404)

    def test_list_uploads_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(f'/api/projects/{pid_a}/uploads', headers=auth(key_b))
        assert resp.status_code in (403, 404)

    def test_get_chat_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(f'/api/projects/{pid_a}/chat', headers=auth(key_b))
        assert resp.status_code in (403, 404)

    def test_post_chat_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.post(
            f'/api/projects/{pid_a}/chat',
            json={'message': 'hello'},
            headers=auth(key_b),
        )
        assert resp.status_code in (403, 404)

    def test_delete_chat_other_project_returns_403(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.delete(f'/api/projects/{pid_a}/chat', headers=auth(key_b))
        assert resp.status_code == 403

    def test_upload_to_other_project_returns_403(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        data = {'file': (b'fake xlsx content', 'test.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
        resp = client.post(
            f'/api/projects/{pid_a}/runs',
            data=data,
            content_type='multipart/form-data',
            headers=auth(key_b),
        )
        assert resp.status_code == 403

    def test_recompute_other_project_returns_403(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.post(f'/api/projects/{pid_a}/recompute', headers=auth(key_b))
        assert resp.status_code == 403

    def test_decisions_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(f'/api/projects/{pid_a}/decisions', headers=auth(key_b))
        assert resp.status_code in (403, 404)

    def test_test_execution_suite_runs_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(
            f'/api/projects/{pid_a}/test-execution/suite-runs',
            headers=auth(key_b),
        )
        assert resp.status_code in (403, 404)

    def test_test_execution_tc_runs_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(
            f'/api/projects/{pid_a}/test-execution/testcase-runs',
            headers=auth(key_b),
        )
        assert resp.status_code in (403, 404)

    def test_filter_options_other_project_denied(self, client, two_projects):
        pid_a, _, _, key_b = two_projects
        resp = client.get(
            f'/api/projects/{pid_a}/test-execution/filter-options',
            headers=auth(key_b),
        )
        assert resp.status_code in (403, 404)


class TestOwnerCanAccessOwnProject:
    """Sanity check — the owner's own key must still work."""

    def test_owner_can_list_runs(self, client, project):
        pid, key = project
        resp = client.get(f'/api/projects/{pid}/runs', headers=auth(key))
        assert resp.status_code == 200

    def test_owner_can_get_chat(self, client, project):
        pid, key = project
        resp = client.get(f'/api/projects/{pid}/chat', headers=auth(key))
        assert resp.status_code == 200

    def test_owner_can_clear_chat(self, client, project):
        pid, key = project
        resp = client.delete(f'/api/projects/{pid}/chat', headers=auth(key))
        assert resp.status_code == 200

    def test_owner_can_get_test_execution(self, client, project):
        pid, key = project
        resp = client.get(f'/api/projects/{pid}/test-execution/suite-runs', headers=auth(key))
        assert resp.status_code == 200
