"""
Tests for multi-turn AI chat endpoints.
Claude API is not expected to be configured in test env;
we verify persistence, history, and graceful fallback.
"""

import pytest
from tests.conftest import auth, requires_firebase


class TestChatGet:
    def test_empty_history_returns_list(self, client, project):
        pid, api_key = project
        resp = client.get(f'/api/projects/{pid}/chat', headers=auth(api_key))
        assert resp.status_code == 200
        assert resp.get_json() == []

    @requires_firebase
    def test_requires_auth(self, client, project):
        pid, _ = project
        resp = client.get(f'/api/projects/{pid}/chat')
        assert resp.status_code == 401


class TestChatPost:
    def test_send_message_returns_assistant_reply(self, client, project):
        pid, api_key = project
        resp = client.post(
            f'/api/projects/{pid}/chat',
            json={'message': 'What is CASI?'},
            headers=auth(api_key),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['role'] == 'assistant'
        assert len(data['content']) > 0

    def test_empty_message_returns_400(self, client, project):
        pid, api_key = project
        resp = client.post(
            f'/api/projects/{pid}/chat',
            json={'message': ''},
            headers=auth(api_key),
        )
        assert resp.status_code == 400

    def test_missing_message_returns_400(self, client, project):
        pid, api_key = project
        resp = client.post(
            f'/api/projects/{pid}/chat',
            json={},
            headers=auth(api_key),
        )
        assert resp.status_code == 400

    @requires_firebase
    def test_requires_auth(self, client, project):
        pid, _ = project
        resp = client.post(f'/api/projects/{pid}/chat', json={'message': 'hello'})
        assert resp.status_code == 401

    def test_fallback_message_when_no_llm_key(self, client, project, monkeypatch):
        """Without ANTHROPIC_API_KEY the chat should still return a helpful message."""
        import routes.chat as chat_module
        monkeypatch.setattr(chat_module, 'ANTHROPIC_API_KEY', None)

        pid, api_key = project
        resp = client.post(
            f'/api/projects/{pid}/chat',
            json={'message': 'Any question'},
            headers=auth(api_key),
        )
        assert resp.status_code == 200
        content = resp.get_json()['content']
        assert 'ANTHROPIC_API_KEY' in content or 'not configured' in content.lower()


class TestChatHistory:
    def test_messages_persist_in_order(self, client, project):
        pid, api_key = project
        client.post(
            f'/api/projects/{pid}/chat',
            json={'message': 'First question'},
            headers=auth(api_key),
        )
        client.post(
            f'/api/projects/{pid}/chat',
            json={'message': 'Second question'},
            headers=auth(api_key),
        )
        resp = client.get(f'/api/projects/{pid}/chat', headers=auth(api_key))
        msgs = resp.get_json()
        # Should have: user, assistant, user, assistant = 4 messages
        assert len(msgs) == 4
        assert msgs[0]['role'] == 'user'
        assert msgs[0]['content'] == 'First question'
        assert msgs[1]['role'] == 'assistant'
        assert msgs[2]['role'] == 'user'
        assert msgs[2]['content'] == 'Second question'

    def test_chat_history_is_project_scoped(self, client, project):
        """Messages from project A must not appear in project B."""
        pid, api_key = project

        # Create project B
        resp_b = client.post('/api/projects', json={'name': 'Project B'})
        pid_b = resp_b.get_json()['id']
        key_b = resp_b.get_json()['api_key']

        try:
            client.post(
                f'/api/projects/{pid}/chat',
                json={'message': 'Secret message for A'},
                headers=auth(api_key),
            )
            msgs_b = client.get(
                f'/api/projects/{pid_b}/chat',
                headers=auth(key_b),
            ).get_json()
            contents = [m['content'] for m in msgs_b]
            assert 'Secret message for A' not in contents
        finally:
            client.delete(f'/api/projects/{pid_b}')


class TestChatClear:
    def test_clear_empties_history(self, client, project):
        pid, api_key = project
        client.post(
            f'/api/projects/{pid}/chat',
            json={'message': 'Will be cleared'},
            headers=auth(api_key),
        )
        client.delete(f'/api/projects/{pid}/chat', headers=auth(api_key))
        msgs = client.get(
            f'/api/projects/{pid}/chat',
            headers=auth(api_key),
        ).get_json()
        assert msgs == []

    @requires_firebase
    def test_clear_requires_auth(self, client, project):
        pid, _ = project
        resp = client.delete(f'/api/projects/{pid}/chat')
        assert resp.status_code == 401
