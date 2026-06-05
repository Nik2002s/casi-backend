"""
Tests for POST /api/projects/<id>/gate — CI/CD quality gate endpoint.
"""

import os
import pytest
from tests.conftest import auth, requires_firebase, FIXTURE_XLSX


@pytest.fixture
def run(client, project):
    """Project + one uploaded run, return (pid, api_key, run_id)."""
    pid, api_key = project
    if not os.path.exists(FIXTURE_XLSX):
        pytest.skip(f'Fixture xlsx not found: {FIXTURE_XLSX}')

    with open(FIXTURE_XLSX, 'rb') as f:
        resp = client.post(
            f'/api/projects/{pid}/runs',
            data={'file': (f, 'CASI_QA_TestSuite_v2.xlsx')},
            content_type='multipart/form-data',
            headers=auth(api_key),
        )
    assert resp.status_code == 201
    run_id = resp.get_json()['id']
    yield pid, api_key, run_id
    client.delete(f'/api/projects/{pid}/runs/{run_id}', headers=auth(api_key))


class TestGateAuth:
    @requires_firebase
    def test_requires_api_key(self, client, project):
        pid, _ = project
        resp = client.post(f'/api/projects/{pid}/gate')
        assert resp.status_code == 401

    @requires_firebase
    def test_wrong_project_key_returns_403(self, client, run):
        pid, api_key, run_id = run
        resp_b = client.post('/api/projects', json={'name': 'Other'})
        key_b  = resp_b.get_json()['api_key']
        pid_b  = resp_b.get_json()['id']
        try:
            resp = client.post(
                f'/api/projects/{pid_b}/gate',
                json={'run_id': run_id},
                headers=auth(api_key),   # key belongs to pid, not pid_b
            )
            assert resp.status_code == 403
        finally:
            client.delete(f'/api/projects/{pid_b}')


class TestGateWithRunId:
    def test_pass_false_no_runs(self, client, project):
        """No runs → gate returns pass: false, not an HTTP error."""
        pid, api_key = project
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={},
            headers=auth(api_key),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['pass'] is False
        assert 'error' in data

    def test_gate_response_shape(self, client, run):
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id},
            headers=auth(api_key),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        for key in ('pass', 'score', 'gate', 'threshold', 'run_id', 'details'):
            assert key in data, f'Missing key: {key}'

    def test_gate_uses_casi_by_default(self, client, run):
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id},
            headers=auth(api_key),
        )
        assert resp.get_json()['metric'] == 'CASI'

    def test_gate_uses_asi_when_requested(self, client, run):
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id, 'use_asi': 'true'},
            headers=auth(api_key),
        )
        assert resp.get_json()['metric'] == 'ASI'

    def test_threshold_700_matches_green_gate(self, client, run):
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id, 'threshold': 700},
            headers=auth(api_key),
        )
        data = resp.get_json()
        expected_pass = data['score'] >= 700
        assert data['pass'] == expected_pass

    def test_threshold_0_always_passes(self, client, run):
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id, 'threshold': 0},
            headers=auth(api_key),
        )
        assert resp.get_json()['pass'] is True

    def test_threshold_999_always_fails(self, client, run):
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id, 'threshold': 999},
            headers=auth(api_key),
        )
        assert resp.get_json()['pass'] is False

    def test_fail_on_gate_red_green_passes(self, client, run):
        """fail_on_gate=Red means only Red fails. Green should pass."""
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id, 'fail_on_gate': 'Red'},
            headers=auth(api_key),
        )
        data = resp.get_json()
        if data['gate'] == 'Green':
            assert data['pass'] is True
        elif data['gate'] == 'Red':
            assert data['pass'] is False

    def test_fail_on_gate_yellow_fails_yellow(self, client, run):
        """fail_on_gate=Yellow fails on Yellow or Red."""
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id, 'fail_on_gate': 'Yellow'},
            headers=auth(api_key),
        )
        data = resp.get_json()
        if data['gate'] == 'Green':
            assert data['pass'] is True
        else:
            assert data['pass'] is False

    def test_nonexistent_run_returns_404(self, client, project):
        pid, api_key = project
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': '00000000-0000-0000-0000-000000000000'},
            headers=auth(api_key),
        )
        assert resp.status_code == 404

    def test_details_has_required_fields(self, client, run):
        pid, api_key, run_id = run
        resp = client.post(
            f'/api/projects/{pid}/gate',
            json={'run_id': run_id},
            headers=auth(api_key),
        )
        details = resp.get_json()['details']
        for field in ('casi_score', 'asi_score', 'tc_count', 'sprint_count', 'n_fail'):
            assert field in details, f'Missing detail: {field}'


class TestGateWithFileUpload:
    def test_gate_accepts_xlsx_upload(self, client, project):
        """Gate can receive an xlsx directly without a prior /runs call."""
        pid, api_key = project
        if not os.path.exists(FIXTURE_XLSX):
            pytest.skip('Fixture xlsx not found')

        with open(FIXTURE_XLSX, 'rb') as f:
            resp = client.post(
                f'/api/projects/{pid}/gate',
                data={
                    'file': (f, 'CASI_QA_TestSuite_v2.xlsx'),
                    'threshold': '700',
                },
                content_type='multipart/form-data',
                headers=auth(api_key),
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert 'pass' in data
        assert 'score' in data
        assert 0 <= data['score'] <= 999
