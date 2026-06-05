"""
Tests for run upload, compute, retrieval, deletion, trend, and decisions.
Uses the real CASI_QA_TestSuite_v2.xlsx fixture.
"""

import os
import io
import pytest
from tests.conftest import auth, requires_firebase, FIXTURE_XLSX


# ── Shared fixture: project with one completed run ────────────────────────────

@pytest.fixture
def run(client, project):
    """Upload and compute one run, return (project_id, api_key, run_id, result)."""
    pid, api_key = project

    if not os.path.exists(FIXTURE_XLSX):
        pytest.skip(f'Fixture xlsx not found: {FIXTURE_XLSX}')

    with open(FIXTURE_XLSX, 'rb') as f:
        data = {'file': (f, 'CASI_QA_TestSuite_v2.xlsx')}
        resp = client.post(
            f'/api/projects/{pid}/runs',
            data=data,
            content_type='multipart/form-data',
            headers=auth(api_key),
        )

    assert resp.status_code == 201, f'Run creation failed: {resp.get_json()}'
    run_data = resp.get_json()
    yield pid, api_key, run_data['id'], run_data

    # Cleanup
    client.delete(
        f'/api/projects/{pid}/runs/{run_data["id"]}',
        headers=auth(api_key),
    )


# ── Upload ────────────────────────────────────────────────────────────────────

class TestRunUpload:
    @requires_firebase
    def test_upload_requires_auth(self, client, project):
        """In production, posting without auth returns 401. Skipped in dev mode."""
        pid, _ = project
        resp = client.post(f'/api/projects/{pid}/runs')
        assert resp.status_code == 401

    def test_upload_no_file_returns_400(self, client, project):
        pid, api_key = project
        resp = client.post(
            f'/api/projects/{pid}/runs',
            headers=auth(api_key),
        )
        assert resp.status_code == 400
        assert 'file' in resp.get_json()['error'].lower()

    def test_upload_non_xlsx_returns_400(self, client, project):
        pid, api_key = project
        data = {'file': (io.BytesIO(b'not an xlsx'), 'test.csv')}
        resp = client.post(
            f'/api/projects/{pid}/runs',
            data=data,
            content_type='multipart/form-data',
            headers=auth(api_key),
        )
        assert resp.status_code == 400
        assert 'xlsx' in resp.get_json()['error'].lower()

    def test_upload_real_xlsx_returns_201(self, client, run):
        pid, api_key, run_id, data = run
        assert run_id is not None
        assert 'result' in data or 'filename' in data

    def test_upload_stores_filename(self, client, run):
        pid, api_key, run_id, data = run
        assert data['filename'] == 'CASI_QA_TestSuite_v2.xlsx'

    def test_upload_file_not_kept_on_disk(self, client, run):
        """Files are deleted after parse — file_path must be None in DB."""
        pid, api_key, run_id, data = run
        assert data.get('file_path') is None


# ── List runs ─────────────────────────────────────────────────────────────────

class TestListRuns:
    def test_list_returns_empty_initially(self, client, project):
        pid, api_key = project
        resp = client.get(f'/api/projects/{pid}/runs', headers=auth(api_key))
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_list_shows_uploaded_run(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.get(f'/api/projects/{pid}/runs', headers=auth(api_key))
        ids = [r['id'] for r in resp.get_json()]
        assert run_id in ids


# ── Get run ───────────────────────────────────────────────────────────────────

class TestGetRun:
    def test_get_run_returns_full_result(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.get(f'/api/projects/{pid}/runs/{run_id}', headers=auth(api_key))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['id'] == run_id
        # result JSONB should contain scores
        result = data.get('result') or {}
        assert 'scores' in result

    def test_get_nonexistent_run_returns_404(self, client, project):
        pid, api_key = project
        resp = client.get(
            f'/api/projects/{pid}/runs/00000000-0000-0000-0000-000000000000',
            headers=auth(api_key),
        )
        assert resp.status_code == 404

    def test_cannot_access_other_projects_run(self, client, run):
        pid, api_key, run_id, _ = run
        # Create an independent second project inline
        resp_b = client.post('/api/projects', json={'name': 'Isolated Project B'})
        pid2 = resp_b.get_json()['id']
        key2 = resp_b.get_json()['api_key']
        try:
            # Project B's key is valid, but run belongs to project A
            resp = client.get(
                f'/api/projects/{pid2}/runs/{run_id}',
                headers=auth(key2),
            )
            assert resp.status_code == 404
        finally:
            client.delete(f'/api/projects/{pid2}')


# ── Latest run ────────────────────────────────────────────────────────────────

class TestLatestRun:
    def test_latest_returns_404_when_no_runs(self, client, project):
        pid, api_key = project
        resp = client.get(f'/api/projects/{pid}/runs/latest', headers=auth(api_key))
        assert resp.status_code == 404

    def test_latest_returns_run_after_upload(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.get(f'/api/projects/{pid}/runs/latest', headers=auth(api_key))
        assert resp.status_code == 200
        assert resp.get_json()['id'] == run_id


# ── Delete run ────────────────────────────────────────────────────────────────

class TestDeleteRun:
    def test_delete_run(self, client, project):
        pid, api_key = project
        if not os.path.exists(FIXTURE_XLSX):
            pytest.skip('Fixture xlsx not found')

        # Create a run specifically to delete
        with open(FIXTURE_XLSX, 'rb') as f:
            resp = client.post(
                f'/api/projects/{pid}/runs',
                data={'file': (f, 'CASI_QA_TestSuite_v2.xlsx')},
                content_type='multipart/form-data',
                headers=auth(api_key),
            )
        run_id = resp.get_json()['id']
        file_path = resp.get_json().get('file_path')

        del_resp = client.delete(
            f'/api/projects/{pid}/runs/{run_id}',
            headers=auth(api_key),
        )
        assert del_resp.status_code == 200

        # Confirm gone from DB
        get_resp = client.get(
            f'/api/projects/{pid}/runs/{run_id}',
            headers=auth(api_key),
        )
        assert get_resp.status_code == 404

        # Confirm file removed from disk
        if file_path:
            assert not os.path.exists(file_path)


# ── CASI score sanity ─────────────────────────────────────────────────────────

class TestScoreSanity:
    def test_casi_score_in_range(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.get(f'/api/projects/{pid}/runs/{run_id}', headers=auth(api_key))
        scores = resp.get_json()['result']['scores']
        assert 0 <= scores['casi_score'] <= 999

    def test_asi_score_in_range(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.get(f'/api/projects/{pid}/runs/{run_id}', headers=auth(api_key))
        scores = resp.get_json()['result']['scores']
        assert 0 <= scores['asi_score'] <= 999

    def test_gate_is_valid_value(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.get(f'/api/projects/{pid}/runs/{run_id}', headers=auth(api_key))
        scores = resp.get_json()['result']['scores']
        assert scores['casi_gate'] in ('Green', 'Yellow', 'Red')

    def test_sprint_history_populated(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.get(f'/api/projects/{pid}/runs/{run_id}', headers=auth(api_key))
        history = resp.get_json()['result'].get('sprint_history', [])
        assert len(history) > 0

    def test_dataset_fields_present(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.get(f'/api/projects/{pid}/runs/{run_id}', headers=auth(api_key))
        dataset = resp.get_json()['result'].get('dataset', {})
        assert 'tc_count' in dataset
        assert dataset['tc_count'] > 0


# ── Trend ─────────────────────────────────────────────────────────────────────

class TestTrend:
    def test_trend_returns_list(self, client, run):
        pid, api_key, _, _ = run
        resp = client.get(f'/api/projects/{pid}/trend', headers=auth(api_key))
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_trend_contains_score_fields(self, client, run):
        pid, api_key, _, _ = run
        resp = client.get(f'/api/projects/{pid}/trend', headers=auth(api_key))
        rows = resp.get_json()
        if rows:
            assert 'casi_score' in rows[0]
            assert 'asi_score' in rows[0]


# ── Decisions ─────────────────────────────────────────────────────────────────

class TestDecisions:
    def test_save_go_decision(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.post(
            f'/api/projects/{pid}/runs/{run_id}/decision',
            json={'decision': 'GO', 'notes': 'All clear', 'decided_by': 'pytest'},
            headers=auth(api_key),
        )
        assert resp.status_code == 201
        assert resp.get_json()['decision'] == 'GO'

    def test_save_nogo_decision(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.post(
            f'/api/projects/{pid}/runs/{run_id}/decision',
            json={'decision': 'NO-GO'},
            headers=auth(api_key),
        )
        assert resp.status_code == 201

    def test_save_conditional_decision(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.post(
            f'/api/projects/{pid}/runs/{run_id}/decision',
            json={'decision': 'CONDITIONAL', 'notes': 'Fix TC-042 first'},
            headers=auth(api_key),
        )
        assert resp.status_code == 201

    def test_invalid_decision_returns_400(self, client, run):
        pid, api_key, run_id, _ = run
        resp = client.post(
            f'/api/projects/{pid}/runs/{run_id}/decision',
            json={'decision': 'MAYBE'},
            headers=auth(api_key),
        )
        assert resp.status_code == 400

    def test_list_decisions(self, client, run):
        pid, api_key, run_id, _ = run
        client.post(
            f'/api/projects/{pid}/runs/{run_id}/decision',
            json={'decision': 'GO'},
            headers=auth(api_key),
        )
        resp = client.get(f'/api/projects/{pid}/decisions', headers=auth(api_key))
        assert resp.status_code == 200
        assert len(resp.get_json()) >= 1
