"""
CASI — pytest fixtures shared across all test modules.
"""

import os
import pytest
import psycopg2

# Disable rate limiting during tests so requests aren't throttled
os.environ.setdefault('RATELIMIT_ENABLED', 'false')

# Point tests at a separate test DB so they never touch production data
TEST_DB_URL = os.environ.get(
    'TEST_DATABASE_URL',
    'postgresql://casi:casi@localhost:5432/casi_test',
)

# Valid xlsx fixture — has the correct TEST EXECUTION / VARIANCE SHEET tabs
FIXTURE_XLSX = os.path.abspath(
    os.path.join(os.path.dirname(__file__), 'fixtures/test_suite.xlsx')
)

# True when Firebase is not configured (dev mode — auth bypass is active)
DEV_MODE = not os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY', '').strip()

requires_firebase = pytest.mark.skipif(
    DEV_MODE,
    reason='Auth tests that check 401/403 for no-header requests only work when Firebase is configured',
)


# ── Patch DB_URL before importing app ─────────────────────────────────────────

@pytest.fixture(scope='session', autouse=True)
def patch_db_url():
    """Redirect all DB calls to the test database for the whole session."""
    import db as db_module
    original_url  = db_module.DB_URL
    original_pool = db_module._pool
    db_module.DB_URL = TEST_DB_URL
    db_module._pool  = None   # force pool to recreate with the test URL
    yield
    db_module.DB_URL = original_url
    db_module._pool  = original_pool


# ── Ensure test DB exists ──────────────────────────────────────────────────────

@pytest.fixture(scope='session', autouse=True)
def ensure_test_db(patch_db_url):
    """
    Create casi_test database if it doesn't exist.
    Runs once per session before any test.
    """
    # Connect to the default 'casi' db to issue CREATE DATABASE
    admin_url = TEST_DB_URL.rsplit('/', 1)[0] + '/casi'
    try:
        conn = psycopg2.connect(admin_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname='casi_test'")
            if not cur.fetchone():
                cur.execute('CREATE DATABASE casi_test')
        conn.close()
    except Exception as exc:
        pytest.skip(f'Cannot connect to PostgreSQL: {exc}')

    # Bootstrap schema in the test DB
    import db as db_module
    conn = db_module.get_db()
    conn.close()
    yield

    # Teardown: drop all tables so next session starts clean
    try:
        conn = psycopg2.connect(TEST_DB_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                DROP TABLE IF EXISTS
                    decisions, diagnostics, chat_messages,
                    sprint_scores, runs, api_keys, projects, sessions
                CASCADE
            """)
        conn.close()
    except Exception:
        pass


# ── Flask test client ─────────────────────────────────────────────────────────

@pytest.fixture(scope='session')
def client(ensure_test_db):
    """Flask test client with TESTING=True."""
    import app as app_module
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as c:
        yield c


# ── Helper: create a project and return (project_id, api_key) ─────────────────

@pytest.fixture
def project(client):
    """Create a fresh project for each test, clean up after."""
    resp = client.post('/api/projects', json={'name': 'Test Project', 'description': 'pytest'})
    assert resp.status_code == 201
    data = resp.get_json()
    yield data['id'], data['api_key']
    # Cleanup
    client.delete(f'/api/projects/{data["id"]}')


# ── Helper: authenticated headers ─────────────────────────────────────────────

def auth(api_key):
    return {'X-CASI-Key': api_key}
