"""
CASI — PostgreSQL persistence layer (Phase 1 rewrite)
Full schema: projects, api_keys, runs, sprint_scores,
             chat_messages, diagnostics, decisions
"""

import os
import json
import hashlib
import secrets
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pgpool

DB_URL = os.environ.get('DATABASE_URL')

# ── Enforce TLS for remote PostgreSQL connections ────────────────────────────
# Railway / Postgres providers use URLs starting with postgres:// or postgresql://.
# We append sslmode=require unless the URL is a local connection (no host or
# host=localhost/127.x) so that dev mode still works without SSL.
def _maybe_add_sslmode(url: str) -> str:
    if not url:
        return url
    if 'sslmode=' in url:
        return url
    _local_hosts = ('localhost', '127.0.0.1', '::1')
    # Quick heuristic: local if no host part or host is loopback
    import re
    m = re.search(r'@([^:/]+)', url)
    host = m.group(1) if m else ''
    if not host or host in _local_hosts:
        return url
    sep = '&' if '?' in url else '?'
    return url + sep + 'sslmode=require'

DB_URL = _maybe_add_sslmode(DB_URL)

# ── Connection pool (thread-safe, shared across gunicorn threads) ─────────────
# min=2, max=10 — well within Railway's 25-connection limit even with 4 workers.
_pool: pgpool.ThreadedConnectionPool | None = None


def _get_pool() -> pgpool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = pgpool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=DB_URL,
            connect_timeout=10,
        )
    return _pool


class _PooledConn:
    """
    Thin wrapper around a psycopg2 connection so that conn.close() returns
    the connection to the pool rather than destroying the socket.

    All other attributes/methods (cursor, commit, rollback, …) are transparently
    forwarded to the underlying connection, so routes require zero changes.
    """
    __slots__ = ('_conn',)

    def __init__(self, conn):
        object.__setattr__(self, '_conn', conn)

    def close(self):
        _get_pool().putconn(object.__getattribute__(self, '_conn'))

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, '_conn'), name, value)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

# ── Schema ────────────────────────────────────────────────────────────────────

DEFAULT_LIMITS = {
    'ai_daily_requests': '10',
    'ai_daily_tokens':   '15000',
    'ai_weekly_tokens':  '75000',
}

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE TABLE IF NOT EXISTS allowed_users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      TEXT UNIQUE NOT NULL,
    added_by   TEXT,
    added_at   TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP
);

-- Projects
CREATE TABLE IF NOT EXISTS projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TIMESTAMP DEFAULT NOW(),
    updated_at  TIMESTAMP DEFAULT NOW()
);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS created_by TEXT;

-- API keys (one or more per project)
CREATE TABLE IF NOT EXISTS api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID REFERENCES projects(id) ON DELETE CASCADE,
    key_hash    TEXT UNIQUE NOT NULL,
    prefix      TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW(),
    last_used   TIMESTAMP
);

-- Runs (one per uploaded Excel file)
CREATE TABLE IF NOT EXISTS runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID REFERENCES projects(id) ON DELETE CASCADE,
    filename    TEXT,
    file_path   TEXT,
    result      JSONB,
    computed_at TIMESTAMP DEFAULT NOW()
);

-- Sprint scores (queryable rows for cross-run trend)
CREATE TABLE IF NOT EXISTS sprint_scores (
    id           SERIAL PRIMARY KEY,
    run_id       UUID REFERENCES runs(id) ON DELETE CASCADE,
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    sprint_start DATE,
    sprint_end   DATE,
    asi_score    FLOAT,
    casi_score   FLOAT,
    asi_gate     TEXT,
    casi_gate    TEXT,
    n_fail       INT,
    components   JSONB
);

-- Chat messages
CREATE TABLE IF NOT EXISTS chat_messages (
    id          SERIAL PRIMARY KEY,
    project_id  UUID REFERENCES projects(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW()
);

-- AI diagnostics
CREATE TABLE IF NOT EXISTS diagnostics (
    id           SERIAL PRIMARY KEY,
    run_id       UUID REFERENCES runs(id) ON DELETE CASCADE,
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    root_cause   TEXT,
    components   TEXT[],
    actions      JSONB,
    generated_at TIMESTAMP DEFAULT NOW()
);

-- Release decisions
CREATE TABLE IF NOT EXISTS decisions (
    id          SERIAL PRIMARY KEY,
    run_id      UUID REFERENCES runs(id) ON DELETE CASCADE,
    project_id  UUID REFERENCES projects(id) ON DELETE CASCADE,
    decision    TEXT,
    notes       TEXT,
    decided_by  TEXT,
    decided_at  TIMESTAMP DEFAULT NOW()
);

-- Legacy sessions table (kept for backward compat)
CREATE TABLE IF NOT EXISTS sessions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at   TIMESTAMP DEFAULT NOW(),
    filename     TEXT,
    tc_count     INT,
    sprint_count INT
);

-- Uploaded files (one row per Excel file uploaded to a project)
CREATE TABLE IF NOT EXISTS uploads (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    file_path    TEXT,
    record_count INT  DEFAULT 0,
    uploaded_at  TIMESTAMP DEFAULT NOW()
);

-- Individual test case rows stored per upload
CREATE TABLE IF NOT EXISTS test_records (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id   UUID REFERENCES uploads(id) ON DELETE CASCADE,
    project_id  UUID REFERENCES projects(id) ON DELETE CASCADE,
    data        JSONB NOT NULL
);

-- Link runs to the upload that triggered them
ALTER TABLE runs ADD COLUMN IF NOT EXISTS upload_id UUID REFERENCES uploads(id) ON DELETE SET NULL;

-- Track which Firebase user uploaded each file (for per-user limits)
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS user_id TEXT;

-- Persist the accepted-variances count from each ingest, so project-level
-- recompute can reconstruct the F (Variances) component without re-parsing
-- the original Excel files.
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS accepted_vars INT DEFAULT 0;

-- Project ownership (email of the user who created it)
ALTER TABLE projects ADD COLUMN IF NOT EXISTS created_by TEXT;

-- User display name (written from Firebase token on login) and sharing opt-in
ALTER TABLE allowed_users ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE allowed_users ADD COLUMN IF NOT EXISTS allow_sharing BOOLEAN NOT NULL DEFAULT FALSE;

-- Project sharing — each row gives one user read access to one project
CREATE TABLE IF NOT EXISTS project_shares (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID REFERENCES projects(id) ON DELETE CASCADE,
    shared_with_email TEXT NOT NULL,
    shared_by         TEXT,
    created_at        TIMESTAMP DEFAULT NOW(),
    UNIQUE(project_id, shared_with_email)
);

-- App configuration (key-value, editable via admin CLI or DB)
CREATE TABLE IF NOT EXISTS app_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW()
);
INSERT INTO app_config (key, value) VALUES ('max_uploads_per_user', '3') ON CONFLICT DO NOTHING;
INSERT INTO app_config (key, value) VALUES ('ai_daily_requests', '10') ON CONFLICT DO NOTHING;
INSERT INTO app_config (key, value) VALUES ('ai_daily_tokens', '15000') ON CONFLICT DO NOTHING;
INSERT INTO app_config (key, value) VALUES ('ai_weekly_tokens', '75000') ON CONFLICT DO NOTHING;

-- Enterprise demo leads — captured from the "Request enterprise" CTA
CREATE TABLE IF NOT EXISTS leads (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT,
    email      TEXT NOT NULL,
    company    TEXT,
    message    TEXT,
    user_id    TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Allowed users — admin adds emails here; anyone in this list can sign in
CREATE TABLE IF NOT EXISTS allowed_users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      TEXT UNIQUE NOT NULL,
    added_by   TEXT,
    added_at   TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP
);

-- ── v2 ingestion format: normalized entities ─────────────────────────────────

-- One row per unique sprint name per project
CREATE TABLE IF NOT EXISTS sprints (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    sprint_name  TEXT NOT NULL,
    sprint_start TIMESTAMP,
    sprint_end   TIMESTAMP,
    created_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE(project_id, sprint_name)
);

-- One row per unique test suite per project
CREATE TABLE IF NOT EXISTS test_suites (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    suite_id     TEXT NOT NULL,
    suite_name   TEXT NOT NULL,
    created_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE(project_id, suite_id)
);

-- One row per unique test case per project
CREATE TABLE IF NOT EXISTS testcases (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID REFERENCES projects(id) ON DELETE CASCADE,
    tc_id        TEXT NOT NULL,
    tc_name      TEXT,
    suite_id     TEXT,
    created_at   TIMESTAMP DEFAULT NOW(),
    UNIQUE(project_id, tc_id)
);

-- One row per testcase run (from TEST EXECUTION sheet)
CREATE TABLE IF NOT EXISTS testcase_runs (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id                UUID REFERENCES uploads(id) ON DELETE CASCADE,
    project_id               UUID REFERENCES projects(id) ON DELETE CASCADE,
    tc_run_id                TEXT NOT NULL,
    suite_run_id             TEXT,
    tc_id                    TEXT,
    suite_id                 TEXT,
    suite_name               TEXT,
    sprint_name              TEXT,
    original_status          TEXT,
    effective_status         TEXT,
    executed_by              TEXT,
    start_timestamp          TIMESTAMP,
    end_timestamp            TIMESTAMP,
    duration_seconds         FLOAT,
    active_variance_applied  BOOLEAN DEFAULT FALSE,
    variance_id              TEXT,
    created_at               TIMESTAMP DEFAULT NOW(),
    UNIQUE(upload_id, tc_run_id)
);

-- One row per suite run (aggregated from testcase runs by SUITE_RUN_ID)
CREATE TABLE IF NOT EXISTS suite_runs (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    upload_id        UUID REFERENCES uploads(id) ON DELETE CASCADE,
    project_id       UUID REFERENCES projects(id) ON DELETE CASCADE,
    suite_run_id     TEXT NOT NULL,
    suite_id         TEXT,
    suite_name       TEXT,
    sprint_name      TEXT,
    start_timestamp  TIMESTAMP,
    end_timestamp    TIMESTAMP,
    duration_seconds FLOAT,
    status           TEXT,
    created_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE(upload_id, suite_run_id)
);

-- One row per variance entry (from VARIANCE SHEET)
CREATE TABLE IF NOT EXISTS variances (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id               UUID REFERENCES projects(id) ON DELETE CASCADE,
    upload_id                UUID REFERENCES uploads(id) ON DELETE CASCADE,
    variance_id              TEXT NOT NULL,
    test_case_id             TEXT,
    variance_reason          TEXT,
    variance_start           TIMESTAMP,
    variance_end             TIMESTAMP,
    variance_current_status  TEXT,
    dismissed_date           TIMESTAMP,
    is_active                BOOLEAN DEFAULT FALSE,
    created_at               TIMESTAMP DEFAULT NOW(),
    UNIQUE(upload_id, variance_id)
);

-- Track raw file size of each upload (file is deleted after parse but size is preserved)
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS file_size_bytes BIGINT DEFAULT 0;

-- AI token usage log (per user, per request)
CREATE TABLE IF NOT EXISTS ai_usage_log (
    id            SERIAL PRIMARY KEY,
    user_email    TEXT NOT NULL,
    project_id    UUID REFERENCES projects(id) ON DELETE SET NULL,
    source        TEXT NOT NULL DEFAULT 'chat',  -- 'chat' | 'diagnostic' | 'explain'
    input_tokens  INT  NOT NULL DEFAULT 0,
    output_tokens INT  NOT NULL DEFAULT 0,
    model         TEXT,
    created_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ai_usage_log_user_idx ON ai_usage_log (user_email, created_at);

-- Migration tracking — each applied migration is recorded here exactly once.
-- New non-idempotent DDL goes into _MIGRATIONS below, not into _SCHEMA.
CREATE TABLE IF NOT EXISTS schema_migrations (
    id         TEXT PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT NOW()
);
"""

# ── Incremental migrations ─────────────────────────────────────────────────────
#
# Rules:
#   1. Append only — never edit an existing entry.
#   2. Each SQL block must be idempotent when possible (use IF NOT EXISTS /
#      IF EXISTS / ON CONFLICT).  For non-idempotent changes wrap them in
#      a DO $$ BEGIN … EXCEPTION WHEN … END $$; block.
#   3. id must be unique and sortable (YYYYMMDD_nnn_slug format).
#
_MIGRATIONS: list[tuple[str, str]] = [
    ('20260507_001_add_gate_signoffs', """
        CREATE TABLE IF NOT EXISTS gate_signoffs (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id         UUID REFERENCES runs(id) ON DELETE CASCADE,
            project_id     UUID REFERENCES projects(id) ON DELETE CASCADE,
            role           TEXT NOT NULL,
            assigned_email TEXT NOT NULL,
            assigned_name  TEXT,
            assigned_by    TEXT NOT NULL,
            assigned_at    TIMESTAMP DEFAULT NOW(),
            verdict        TEXT,
            verdict_notes  TEXT,
            verdict_at     TIMESTAMP,
            UNIQUE(run_id, role)
        );
        CREATE INDEX IF NOT EXISTS gate_signoffs_run_idx ON gate_signoffs (run_id);
        CREATE INDEX IF NOT EXISTS gate_signoffs_email_idx ON gate_signoffs (LOWER(assigned_email));
    """),
    ('20260507_002_add_terms_acceptance', """
        ALTER TABLE allowed_users
            ADD COLUMN IF NOT EXISTS terms_accepted_at   TIMESTAMP,
            ADD COLUMN IF NOT EXISTS privacy_accepted_at TIMESTAMP,
            ADD COLUMN IF NOT EXISTS terms_version       TEXT;
    """),
]


# ── Connection ─────────────────────────────────────────────────────────────────

def get_db() -> _PooledConn:
    """
    Return a pooled connection wrapped in _PooledConn.
    Callers call conn.close() as usual — that returns the connection to the pool.
    """
    raw = _get_pool().getconn()
    return _PooledConn(raw)


def release_db(conn):
    """Explicit release — identical to conn.close() for _PooledConn."""
    conn.close()


def init_schema():
    """
    One-time schema initialisation.  Call once at application startup.

    Runs the idempotent _SCHEMA baseline, then applies any pending incremental
    _MIGRATIONS exactly once each.  Not called from get_db() — running DDL on
    every pooled connection acquisition is wasteful.
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Apply baseline schema (all idempotent CREATE TABLE IF NOT EXISTS / ALTER … IF NOT EXISTS)
            cur.execute(_SCHEMA)
            conn.commit()
            # Apply any pending incremental migrations exactly once
            for migration_id, sql in _MIGRATIONS:
                cur.execute(
                    "SELECT 1 FROM schema_migrations WHERE id = %s", (migration_id,)
                )
                if cur.fetchone() is None:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (id) VALUES (%s)", (migration_id,)
                    )
                    conn.commit()
    finally:
        release_db(conn)


def rows(conn, sql, params=()):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def row(conn, sql, params=()):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        r = cur.fetchone()
        return dict(r) if r else None


def execute(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


# ── API Key helpers ────────────────────────────────────────────────────────────

def generate_api_key():
    """Return (raw_key, prefix, key_hash) — store hash, show raw once."""
    raw = 'sk-casi-' + secrets.token_urlsafe(32)
    prefix = raw[:16]
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, prefix, key_hash


def hash_key(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


# ── Projects ───────────────────────────────────────────────────────────────────

def create_project(conn, name, description='', created_by=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "INSERT INTO projects (name, description, created_by) VALUES (%s, %s, %s) RETURNING *",
            (name, description, created_by),
        )
        project = dict(cur.fetchone())
    conn.commit()

    # Auto-generate first API key
    raw, prefix, key_hash = generate_api_key()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (project_id, key_hash, prefix) VALUES (%s, %s, %s)",
            (project['id'], key_hash, prefix),
        )
    conn.commit()

    # Auto-share with every admin — silently, best-effort
    admin_emails = [
        e.strip().lower()
        for e in os.environ.get('ADMIN_EMAILS', '').split(',')
        if e.strip()
    ]
    for admin_email in admin_emails:
        # Don't share back to the owner themselves
        if created_by and created_by.lower() == admin_email:
            continue
        try:
            share_project(conn, project['id'], admin_email, shared_by='system:auto')
        except Exception:
            pass  # non-fatal

    project['api_key'] = raw   # returned once only
    return project


# ── Project quota helpers ─────────────────────────────────────────────────────

def count_projects_by_owner(conn, email: str) -> int:
    """Count projects created by this email."""
    r = row(conn,
        "SELECT COUNT(*) AS cnt FROM projects WHERE LOWER(created_by) = LOWER(%s)",
        (email,),
    )
    return int(r['cnt']) if r else 0


def count_public_projects_by_owner(conn, email: str) -> int:
    """Count public projects created by this email."""
    r = row(conn,
        "SELECT COUNT(*) AS cnt FROM projects WHERE LOWER(created_by) = LOWER(%s) AND is_public = TRUE",
        (email,),
    )
    return int(r['cnt']) if r else 0


def count_projects_shared_with(conn, email: str) -> int:
    """Count projects shared with this email, excluding public projects
    (public projects don't count toward the shared-project quota)."""
    r = row(conn, """
        SELECT COUNT(*) AS cnt
        FROM project_shares ps
        JOIN projects p ON p.id = ps.project_id
        WHERE LOWER(ps.shared_with_email) = LOWER(%s)
          AND p.is_public = FALSE
    """, (email,))
    return int(r['cnt']) if r else 0


def list_projects(conn, user_email=None):
    """
    Return projects visible to user_email:
      - Projects they created
      - Projects shared with them
    If user_email is None (dev/API key mode), return all projects.
    """
    _SELECT = """
            SELECT p.*,
                   r.computed_at       AS last_run_at,
                   r.result->>'scores' AS last_scores,
                   (SELECT COUNT(*) FROM runs
                    WHERE project_id = p.id) AS run_count,
                   (SELECT COUNT(DISTINCT sr.suite_run_id)
                    FROM suite_runs sr WHERE sr.project_id = p.id
                      AND sr.start_timestamp >= NOW() - INTERVAL '6 months') AS suite_run_count_6m,
                   (SELECT COUNT(DISTINCT tr.tc_run_id)
                    FROM testcase_runs tr WHERE tr.project_id = p.id
                      AND tr.start_timestamp >= NOW() - INTERVAL '6 months') AS tc_run_count_6m,
                   (SELECT MAX(sr.end_timestamp)
                    FROM suite_runs sr WHERE sr.project_id = p.id) AS last_suite_run_at,
                   COALESCE(au.display_name, p.created_by) AS created_by_name
            FROM projects p
            LEFT JOIN allowed_users au ON LOWER(au.email) = LOWER(p.created_by)
            LEFT JOIN LATERAL (
                SELECT computed_at, result
                FROM runs
                WHERE project_id = p.id
                ORDER BY computed_at DESC
                LIMIT 1
            ) r ON true
    """

    if not user_email:
        return rows(conn, _SELECT + " ORDER BY p.updated_at DESC")

    return rows(conn, _SELECT + """
        WHERE p.created_by = %s
           OR p.is_public = TRUE
           OR EXISTS (
               SELECT 1 FROM project_shares ps
               WHERE ps.project_id = p.id
                 AND ps.shared_with_email = %s
           )
        ORDER BY p.updated_at DESC
    """, (user_email, user_email))


def get_project(conn, project_id):
    return row(conn, "SELECT * FROM projects WHERE id = %s", (project_id,))


def is_project_owner(conn, project_id, user_email):
    """Return True if user_email is the creator of the project.

    Legacy projects with created_by IS NULL are owned by no one — they
    do NOT grant write access to arbitrary authenticated users.  Only
    admin-level callers should be able to mutate them.
    """
    if not user_email:
        return False
    p = get_project(conn, project_id)
    if not p:
        return False
    owner = p.get('created_by')
    return owner is not None and owner.lower() == user_email.lower()


def can_access_project(conn, project_id, user_email):
    """
    Return True if user_email is allowed to READ this project.

    Access is granted when ANY of the following is true:
      - user_email is None/empty (dev or API-key mode — caller enforces key ownership)
      - user is the project creator (or created_by IS NULL for legacy rows)
      - project is marked is_public = TRUE
      - project has been explicitly shared with user_email

    Uses a single SQL query to keep it atomic.
    """
    if not user_email:
        return True

    result = row(conn, """
        SELECT 1
        FROM projects p
        WHERE p.id = %s
          AND (
              LOWER(p.created_by) = LOWER(%s)
              OR p.created_by IS NULL
              OR p.is_public = TRUE
              OR EXISTS (
                  SELECT 1 FROM project_shares ps
                  WHERE ps.project_id = p.id
                    AND LOWER(ps.shared_with_email) = LOWER(%s)
              )
          )
        LIMIT 1
    """, (project_id, user_email, user_email))
    return result is not None


def share_project(conn, project_id, shared_with_email, shared_by):
    """Add a user to project_shares. Idempotent (ON CONFLICT DO NOTHING)."""
    execute(conn,
        """
        INSERT INTO project_shares (project_id, shared_with_email, shared_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (project_id, shared_with_email) DO NOTHING
        """,
        (project_id, shared_with_email.lower().strip(), shared_by),
    )


def get_project_shares(conn, project_id):
    return rows(conn,
        "SELECT * FROM project_shares WHERE project_id = %s ORDER BY created_at",
        (project_id,),
    )


def remove_project_share(conn, project_id, shared_with_email):
    execute(conn,
        "DELETE FROM project_shares WHERE project_id = %s AND shared_with_email = %s",
        (project_id, shared_with_email.lower().strip()),
    )


def update_project(conn, project_id, name=None, description=None, is_public=None):
    fields, vals = [], []
    if name is not None:
        fields.append('name = %s'); vals.append(name)
    if description is not None:
        fields.append('description = %s'); vals.append(description)
    if is_public is not None:
        fields.append('is_public = %s'); vals.append(bool(is_public))
    if not fields:
        return
    fields.append('updated_at = NOW()')
    vals.append(project_id)
    execute(conn, f"UPDATE projects SET {', '.join(fields)} WHERE id = %s", vals)


def delete_project(conn, project_id):
    execute(conn, "DELETE FROM projects WHERE id = %s", (project_id,))


# ── API Keys ───────────────────────────────────────────────────────────────────

def create_api_key(conn, project_id):
    raw, prefix, key_hash = generate_api_key()
    execute(conn,
        "INSERT INTO api_keys (project_id, key_hash, prefix) VALUES (%s, %s, %s)",
        (project_id, key_hash, prefix),
    )
    return raw, prefix


def revoke_api_keys(conn, project_id):
    execute(conn, "DELETE FROM api_keys WHERE project_id = %s", (project_id,))


def resolve_api_key(conn, raw_key):
    """Return project_id if valid key, else None. Updates last_used."""
    kh = hash_key(raw_key)
    r = row(conn, "SELECT project_id, id FROM api_keys WHERE key_hash = %s", (kh,))
    if r:
        execute(conn,
            "UPDATE api_keys SET last_used = NOW() WHERE id = %s", (r['id'],))
    return r['project_id'] if r else None


def list_api_keys(conn, project_id):
    return rows(conn,
        "SELECT id, prefix, created_at, last_used FROM api_keys WHERE project_id = %s",
        (project_id,),
    )


# ── Runs ───────────────────────────────────────────────────────────────────────

def create_run(conn, project_id, filename, file_path, result, upload_id=None):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO runs (project_id, filename, file_path, result, upload_id)
               VALUES (%s, %s, %s, %s, %s) RETURNING *""",
            (project_id, filename, file_path, json.dumps(result), upload_id),
        )
        run = dict(cur.fetchone())
    conn.commit()

    # Persist sprint scores
    for s in result.get('sprint_history', []):
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO sprint_scores
                   (run_id, project_id, sprint_start, sprint_end,
                    asi_score, casi_score, asi_gate, casi_gate, n_fail, components)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run['id'], project_id,
                 s.get('sprint_start'), s.get('sprint_end'),
                 s.get('asi_score'), s.get('casi_score'),
                 s.get('asi_gate'), s.get('casi_gate'),
                 s.get('n_fail'), json.dumps({})),
            )
    conn.commit()

    # Touch project updated_at
    execute(conn,
        "UPDATE projects SET updated_at = NOW() WHERE id = %s", (project_id,))

    return run


def trim_old_runs(conn, project_id: str, keep: int = 3) -> int:
    """Delete all but the most recent `keep` runs for a project.
    sprint_scores rows cascade-delete automatically.
    Returns the number of rows deleted."""
    r = row(conn,
        "SELECT COUNT(*) AS cnt FROM runs WHERE project_id = %s",
        (project_id,),
    )
    total = int(r['cnt']) if r else 0
    if total <= keep:
        return 0
    execute(conn, """
        DELETE FROM runs
        WHERE project_id = %s
          AND id NOT IN (
              SELECT id FROM runs
              WHERE project_id = %s
              ORDER BY computed_at DESC
              LIMIT %s
          )
    """, (project_id, project_id, keep))
    return total - keep


def list_runs(conn, project_id):
    return rows(conn, """
        SELECT id, upload_id, filename, computed_at,
               result->'dataset'   AS dataset,
               result->'scores'    AS scores
        FROM runs
        WHERE project_id = %s
        ORDER BY computed_at DESC
    """, (project_id,))


def get_run(conn, run_id):
    r = row(conn, "SELECT * FROM runs WHERE id = %s", (run_id,))
    if r and isinstance(r.get('result'), str):
        r['result'] = json.loads(r['result'])
    return r


def get_latest_run(conn, project_id):
    r = row(conn, """
        SELECT * FROM runs WHERE project_id = %s
        ORDER BY computed_at DESC LIMIT 1
    """, (project_id,))
    if r and isinstance(r.get('result'), str):
        r['result'] = json.loads(r['result'])
    return r


def delete_run(conn, run_id):
    execute(conn, "DELETE FROM runs WHERE id = %s", (run_id,))


def get_cross_run_trend(conn, project_id):
    """Sprint scores across all runs for this project — for trend chart."""
    return rows(conn, """
        SELECT ss.*, r.filename, r.computed_at
        FROM sprint_scores ss
        JOIN runs r ON r.id = ss.run_id
        WHERE ss.project_id = %s
        ORDER BY ss.sprint_start ASC, r.computed_at ASC
    """, (project_id,))


# ── Chat ───────────────────────────────────────────────────────────────────────

def save_message(conn, project_id, role, content):
    execute(conn,
        "INSERT INTO chat_messages (project_id, role, content) VALUES (%s,%s,%s)",
        (project_id, role, content),
    )


def get_messages(conn, project_id, limit=50):
    return rows(conn, """
        SELECT role, content, created_at
        FROM chat_messages
        WHERE project_id = %s
        ORDER BY created_at ASC
        LIMIT %s
    """, (project_id, limit))


def clear_messages(conn, project_id):
    execute(conn, "DELETE FROM chat_messages WHERE project_id = %s", (project_id,))


# ── AI Usage Logging ───────────────────────────────────────────────────────────

def log_ai_usage(conn, user_email, project_id, source, input_tokens, output_tokens, model=None):
    """Record one AI call's token consumption for a user."""
    execute(conn, """
        INSERT INTO ai_usage_log (user_email, project_id, source, input_tokens, output_tokens, model)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (user_email, project_id, source, int(input_tokens), int(output_tokens), model))


def get_token_usage(conn, user_email):
    """Return aggregated input/output token counts across 3 windows for a user.

    Uses the same UTC-midnight / UTC-week boundaries as check_ai_quota so that
    'today_requests' here always matches 'daily_requests_used' in the quota widget.
    """
    return row(conn, """
        SELECT
            COALESCE(SUM(input_tokens)  FILTER (WHERE created_at >= date_trunc('day',  NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS today_input,
            COALESCE(SUM(output_tokens) FILTER (WHERE created_at >= date_trunc('day',  NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS today_output,
            COALESCE(COUNT(*)           FILTER (WHERE created_at >= date_trunc('day',  NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS today_requests,
            COALESCE(SUM(input_tokens)  FILTER (WHERE created_at >= date_trunc('week', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS week_input,
            COALESCE(SUM(output_tokens) FILTER (WHERE created_at >= date_trunc('week', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS week_output,
            COALESCE(COUNT(*)           FILTER (WHERE created_at >= date_trunc('week', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS week_requests,
            COALESCE(SUM(input_tokens)  FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'), 0) AS month_input,
            COALESCE(SUM(output_tokens) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'), 0) AS month_output,
            COALESCE(COUNT(*)           FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'), 0) AS month_requests
        FROM ai_usage_log
        WHERE user_email = %s
    """, (user_email,))


# ── AI Quota Checking ─────────────────────────────────────────────────────────

def check_ai_quota(conn, user_email):
    """
    Check whether user_email is within their AI usage limits.
    Returns a dict with allowed flag, block reason, and current counters.
    """
    import datetime

    daily_requests_limit = int(get_config(conn, 'ai_daily_requests', DEFAULT_LIMITS['ai_daily_requests']))
    daily_tokens_limit   = int(get_config(conn, 'ai_daily_tokens',   DEFAULT_LIMITS['ai_daily_tokens']))
    weekly_tokens_limit  = int(get_config(conn, 'ai_weekly_tokens',  DEFAULT_LIMITS['ai_weekly_tokens']))

    stats = row(conn, """
        SELECT
            COALESCE(COUNT(*) FILTER (
                WHERE created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            ), 0) AS daily_requests,
            COALESCE(SUM(input_tokens + output_tokens) FILTER (
                WHERE created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            ), 0) AS daily_tokens,
            COALESCE(SUM(input_tokens + output_tokens) FILTER (
                WHERE created_at >= date_trunc('week', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            ), 0) AS weekly_tokens
        FROM ai_usage_log
        WHERE user_email = %s
    """, (user_email,))

    daily_requests_used = int(stats['daily_requests'])
    daily_tokens_used   = int(stats['daily_tokens'])
    weekly_tokens_used  = int(stats['weekly_tokens'])

    # Compute reset times (all UTC, timezone-aware)
    now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    next_midnight = (now_utc + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    days_until_monday = (7 - now_utc.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_monday = (now_utc + datetime.timedelta(days=days_until_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    block_reason = None
    if daily_requests_used >= daily_requests_limit:
        block_reason = 'daily_requests'
    elif daily_tokens_used >= daily_tokens_limit:
        block_reason = 'daily_tokens'
    elif weekly_tokens_used >= weekly_tokens_limit:
        block_reason = 'weekly_tokens'

    return {
        'allowed':              block_reason is None,
        'block_reason':         block_reason,
        'daily_requests_used':  daily_requests_used,
        'daily_requests_limit': daily_requests_limit,
        'daily_tokens_used':    daily_tokens_used,
        'daily_tokens_limit':   daily_tokens_limit,
        'weekly_tokens_used':   weekly_tokens_used,
        'weekly_tokens_limit':  weekly_tokens_limit,
        'daily_reset_at':       next_midnight.isoformat() + 'Z',
        'weekly_reset_at':      next_monday.isoformat() + 'Z',
    }


def get_user_quota(conn, user_email):
    """Same as check_ai_quota but always returns allowed=True (for display purposes)."""
    quota = check_ai_quota(conn, user_email)
    quota['allowed'] = True
    return quota


# ── Diagnostics ────────────────────────────────────────────────────────────────

def save_diagnostic(conn, run_id, project_id, diag):
    execute(conn, """
        INSERT INTO diagnostics (run_id, project_id, root_cause, components, actions)
        VALUES (%s, %s, %s, %s, %s)
    """, (
        run_id, project_id,
        diag.get('root_cause'),
        diag.get('components_implicated', []),
        json.dumps(diag.get('actions', [])),
    ))


def get_latest_diagnostic(conn, project_id):
    return row(conn, """
        SELECT * FROM diagnostics
        WHERE project_id = %s
        ORDER BY generated_at DESC LIMIT 1
    """, (project_id,))


# ── Decisions ─────────────────────────────────────────────────────────────────

def save_decision(conn, run_id, project_id, decision, notes='', decided_by=''):
    execute(conn, """
        INSERT INTO decisions (run_id, project_id, decision, notes, decided_by)
        VALUES (%s, %s, %s, %s, %s)
    """, (run_id, project_id, decision, notes, decided_by))


# ── Gate sign-off assignments & verdicts ───────────────────────────────────────

VALID_SIGNOFF_ROLES = ('eng_lead', 'tech_lead', 'product_lead')


def get_signoffs(conn, run_id):
    """Return all three sign-off rows for a run (missing roles return None entries)."""
    existing = {r['role']: r for r in rows(conn, """
        SELECT id, run_id, role, assigned_email, assigned_name,
               assigned_by, assigned_at, verdict, verdict_notes, verdict_at
        FROM gate_signoffs WHERE run_id = %s
    """, (run_id,))}
    return [existing.get(role) for role in VALID_SIGNOFF_ROLES]


def upsert_signoff(conn, run_id, project_id, role, assigned_email,
                   assigned_name, assigned_by, is_admin: bool = False):
    """Assign (or reassign) a role; resets any previous verdict.

    Reassignment is blocked if a verdict already exists, unless the caller is
    an admin.  This prevents a project owner from nullifying an existing
    approval/rejection.
    """
    if role not in VALID_SIGNOFF_ROLES:
        raise ValueError(f'Invalid role: {role}')
    # Check for an existing verdict before proceeding
    if not is_admin:
        existing = row(conn,
            "SELECT verdict FROM gate_signoffs WHERE run_id = %s AND role = %s",
            (run_id, role)
        )
        if existing and existing.get('verdict') is not None:
            raise ValueError(
                'Cannot reassign this role — a verdict has already been recorded. '
                'Contact an admin to reset it.'
            )
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO gate_signoffs
                (run_id, project_id, role, assigned_email, assigned_name, assigned_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, role) DO UPDATE
                SET assigned_email = EXCLUDED.assigned_email,
                    assigned_name  = EXCLUDED.assigned_name,
                    assigned_by    = EXCLUDED.assigned_by,
                    assigned_at    = NOW(),
                    verdict        = NULL,
                    verdict_notes  = NULL,
                    verdict_at     = NULL
            RETURNING *
        """, (run_id, project_id, role, assigned_email, assigned_name, assigned_by))
        result = dict(cur.fetchone())
    conn.commit()
    return result


def save_signoff_verdict(conn, run_id, role, actor_email, verdict, verdict_notes=''):
    """Record the assigned user's approve/reject verdict.

    Returns the updated row, or None if the caller is not the assigned user
    OR if a verdict has already been recorded (verdicts are immutable).
    """
    if role not in VALID_SIGNOFF_ROLES:
        raise ValueError(f'Invalid role: {role}')
    if verdict not in ('approved', 'rejected'):
        raise ValueError(f'Invalid verdict: {verdict}')
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            UPDATE gate_signoffs
               SET verdict       = %s,
                   verdict_notes = %s,
                   verdict_at    = NOW()
             WHERE run_id = %s
               AND role   = %s
               AND LOWER(assigned_email) = LOWER(%s)
               AND verdict IS NULL
            RETURNING *
        """, (verdict, verdict_notes, run_id, role, actor_email))
        updated = cur.fetchone()
    if updated:
        conn.commit()
        return dict(updated)
    return None


def get_decisions(conn, project_id):
    return rows(conn, """
        SELECT d.*, r.filename
        FROM decisions d
        JOIN runs r ON r.id = d.run_id
        WHERE d.project_id = %s
        ORDER BY d.decided_at DESC
    """, (project_id,))


# ── Uploads ────────────────────────────────────────────────────────────────────

def create_upload(conn, project_id, filename, file_path, record_count=0, user_id=None, accepted_vars=0, file_size_bytes=0):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO uploads (project_id, filename, file_path, record_count, user_id, accepted_vars, file_size_bytes)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (project_id, filename, file_path, record_count, user_id, accepted_vars, file_size_bytes),
        )
        upload = dict(cur.fetchone())
    conn.commit()
    return upload


def sum_project_accepted_vars(conn, project_id):
    """Sum of accepted_vars across all uploads for a project. Used by recompute."""
    r = row(conn,
        "SELECT COALESCE(SUM(accepted_vars), 0) AS total FROM uploads WHERE project_id = %s",
        (project_id,),
    )
    return int(r['total']) if r else 0


def count_uploads_by_user(conn, project_id, user_id):
    """Return number of uploads a Firebase user has in this project."""
    r = row(conn,
        "SELECT COUNT(*) AS cnt FROM uploads WHERE project_id = %s AND user_id = %s",
        (project_id, user_id),
    )
    return int(r['cnt']) if r else 0


# ── App Config ─────────────────────────────────────────────────────────────────

def get_config(conn, key, default=None):
    r = row(conn, "SELECT value FROM app_config WHERE key = %s", (key,))
    return r['value'] if r else default


def set_config(conn, key, value):
    execute(conn,
        """INSERT INTO app_config (key, value, updated_at)
           VALUES (%s, %s, NOW())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
        (key, str(value)),
    )


# ── Enterprise Leads ──────────────────────────────────────────────────────────

def save_lead(conn, name: str, email: str, company: str, message: str, user_id: str = None):
    """Persist an enterprise demo lead captured from the CTA modal."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """INSERT INTO leads (name, email, company, message, user_id)
               VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at""",
            (name, email, company, message, user_id),
        )
        result = dict(cur.fetchone())
    conn.commit()
    return result


# ── Allowed Users ──────────────────────────────────────────────────────────────

def is_user_allowed(conn, email: str) -> bool:
    r = row(conn, "SELECT 1 FROM allowed_users WHERE LOWER(email) = LOWER(%s)", (email,))
    return r is not None


def add_allowed_user(conn, email: str, added_by: str = ''):
    execute(conn,
        """INSERT INTO allowed_users (email, added_by)
           VALUES (LOWER(%s), %s)
           ON CONFLICT (email) DO NOTHING""",
        (email, added_by),
    )


def remove_allowed_user(conn, email: str):
    execute(conn, "DELETE FROM allowed_users WHERE LOWER(email) = LOWER(%s)", (email,))


def list_allowed_users(conn):
    return rows(conn,
        "SELECT id, email, added_by, added_at, last_login FROM allowed_users ORDER BY added_at DESC"
    )


def touch_user_login(conn, email: str, display_name: str = None):
    if display_name:
        execute(conn,
            """UPDATE allowed_users
               SET last_login = NOW(), display_name = %s
               WHERE LOWER(email) = LOWER(%s)""",
            (display_name, email),
        )
    else:
        execute(conn,
            "UPDATE allowed_users SET last_login = NOW() WHERE LOWER(email) = LOWER(%s)",
            (email,),
        )


# Current policy version — bump this string to force re-acceptance after policy changes
TERMS_VERSION = '2026-05-07'


def get_user_terms_status(conn, email: str) -> dict:
    """Return terms/privacy acceptance timestamps and current version for a user."""
    r = row(conn, """
        SELECT terms_accepted_at, privacy_accepted_at, terms_version
        FROM allowed_users WHERE LOWER(email) = LOWER(%s)
    """, (email,))
    if not r:
        return {'terms_accepted': False, 'privacy_accepted': False, 'needs_acceptance': True}
    terms_ok   = bool(r.get('terms_accepted_at'))   and r.get('terms_version') == TERMS_VERSION
    privacy_ok = bool(r.get('privacy_accepted_at')) and r.get('terms_version') == TERMS_VERSION
    return {
        'terms_accepted':   terms_ok,
        'privacy_accepted': privacy_ok,
        'needs_acceptance': not (terms_ok and privacy_ok),
        'terms_version':    r.get('terms_version'),
    }


def record_terms_acceptance(conn, email: str, accepted_terms: bool, accepted_privacy: bool):
    """Record that a user accepted the current terms and/or privacy policy."""
    if not accepted_terms or not accepted_privacy:
        raise ValueError('Both terms and privacy policy must be accepted')
    execute(conn, """
        UPDATE allowed_users
        SET terms_accepted_at   = NOW(),
            privacy_accepted_at = NOW(),
            terms_version       = %s
        WHERE LOWER(email) = LOWER(%s)
    """, (TERMS_VERSION, email))


def list_uploads(conn, project_id):
    return rows(conn, """
        SELECT u.*,
               (SELECT COUNT(*) FROM testcase_runs WHERE upload_id = u.id) AS record_count
        FROM uploads u
        WHERE u.project_id = %s
        ORDER BY u.uploaded_at DESC
    """, (project_id,))


def get_upload(conn, upload_id):
    return row(conn, "SELECT * FROM uploads WHERE id = %s", (upload_id,))


def delete_upload(conn, upload_id):
    """Delete upload; test_records are cascade-deleted automatically."""
    execute(conn, "DELETE FROM uploads WHERE id = %s", (upload_id,))


def update_upload_count(conn, upload_id, record_count):
    execute(conn,
        "UPDATE uploads SET record_count = %s WHERE id = %s",
        (record_count, upload_id),
    )


# ── Test Records ───────────────────────────────────────────────────────────────

def save_test_records(conn, upload_id, project_id, records):
    """Bulk-insert test case records. records is a list of JSON-safe dicts."""
    if not records:
        return
    with conn.cursor() as cur:
        for rec in records:
            cur.execute(
                """INSERT INTO test_records (upload_id, project_id, data)
                   VALUES (%s, %s, %s)""",
                (upload_id, project_id, json.dumps(rec)),
            )
    conn.commit()


def get_project_records(conn, project_id):
    """Return all test_records.data dicts for a project (all uploads combined)."""
    rs = rows(conn,
        "SELECT data FROM test_records WHERE project_id = %s",
        (project_id,),
    )
    out = []
    for r in rs:
        d = r['data']
        if isinstance(d, str):
            d = json.loads(d)
        out.append(d)
    return out


def has_project_records(conn, project_id):
    """True if the project has any testcase_run rows (the source of truth for recompute)."""
    r = row(conn,
        "SELECT 1 FROM testcase_runs WHERE project_id = %s LIMIT 1",
        (project_id,),
    )
    return r is not None


def count_project_testcase_runs(conn, project_id: str) -> int:
    """Return the total number of testcase_run rows for a project."""
    r = row(conn,
        "SELECT COUNT(*) AS cnt FROM testcase_runs WHERE project_id = %s",
        (project_id,),
    )
    return int(r['cnt']) if r else 0


def get_upload_records(conn, upload_id: str) -> list[dict]:
    """Return all test_records.data dicts for a specific upload."""
    rs = rows(conn,
        "SELECT data FROM test_records WHERE upload_id = %s ORDER BY id",
        (upload_id,),
    )
    out = []
    for r in rs:
        d = r['data']
        if isinstance(d, str):
            d = json.loads(d)
        out.append(d)
    return out


# ── v2 Ingestion: normalized entity persistence ────────────────────────────────

def upsert_sprints(conn, project_id: str, sprints: list[dict]) -> None:
    """Insert or update sprint records (keyed on project_id + sprint_name)."""
    if not sprints:
        return
    with conn.cursor() as cur:
        for sp in sprints:
            cur.execute(
                """INSERT INTO sprints (project_id, sprint_name, sprint_start, sprint_end)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (project_id, sprint_name)
                   DO UPDATE SET
                       sprint_start = LEAST(sprints.sprint_start, EXCLUDED.sprint_start),
                       sprint_end   = GREATEST(sprints.sprint_end, EXCLUDED.sprint_end)""",
                (project_id, sp['sprint_name'], sp.get('sprint_start'), sp.get('sprint_end')),
            )
    conn.commit()


def upsert_test_suites(conn, project_id: str, test_suites: list[dict]) -> None:
    """Insert or ignore test suite records (keyed on project_id + suite_id)."""
    if not test_suites:
        return
    with conn.cursor() as cur:
        for ts in test_suites:
            cur.execute(
                """INSERT INTO test_suites (project_id, suite_id, suite_name)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (project_id, suite_id) DO NOTHING""",
                (project_id, ts['suite_id'], ts['suite_name']),
            )
    conn.commit()


def upsert_testcases(conn, project_id: str, testcases: list[dict]) -> None:
    """Insert or update testcase records (keyed on project_id + tc_id)."""
    if not testcases:
        return
    with conn.cursor() as cur:
        for tc in testcases:
            cur.execute(
                """INSERT INTO testcases (project_id, tc_id, tc_name, suite_id)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (project_id, tc_id)
                   DO UPDATE SET tc_name = EXCLUDED.tc_name,
                                 suite_id = EXCLUDED.suite_id""",
                (project_id, tc['tc_id'], tc.get('tc_name'), tc.get('suite_id')),
            )
    conn.commit()


def insert_testcase_runs(conn, project_id: str, upload_id: str, tc_runs: list[dict]) -> None:
    """Bulk-insert testcase run records. Silently skips duplicates (same upload + tc_run_id)."""
    if not tc_runs:
        return
    with conn.cursor() as cur:
        for r in tc_runs:
            cur.execute(
                """INSERT INTO testcase_runs (
                       upload_id, project_id, tc_run_id, suite_run_id,
                       tc_id, suite_id, suite_name, sprint_name,
                       original_status, effective_status, executed_by,
                       start_timestamp, end_timestamp, duration_seconds,
                       active_variance_applied, variance_id
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (upload_id, tc_run_id) DO NOTHING""",
                (
                    upload_id, project_id,
                    r['tc_run_id'], r['suite_run_id'],
                    r['tc_id'], r['suite_id'], r['suite_name'], r['sprint_name'],
                    r['original_status'], r['effective_status'], r.get('executed_by'),
                    r.get('start_timestamp'), r.get('end_timestamp'),
                    r.get('duration_seconds'),
                    r.get('active_variance_applied', False),
                    r.get('variance_id'),
                ),
            )
    conn.commit()


def insert_suite_runs(conn, project_id: str, upload_id: str, suite_runs: list[dict]) -> None:
    """Bulk-insert suite run records. Silently skips duplicates."""
    if not suite_runs:
        return
    with conn.cursor() as cur:
        for sr in suite_runs:
            cur.execute(
                """INSERT INTO suite_runs (
                       upload_id, project_id, suite_run_id,
                       suite_id, suite_name, sprint_name,
                       start_timestamp, end_timestamp, duration_seconds, status
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (upload_id, suite_run_id) DO NOTHING""",
                (
                    upload_id, project_id, sr['suite_run_id'],
                    sr.get('suite_id'), sr.get('suite_name'), sr.get('sprint_name'),
                    sr.get('start_timestamp'), sr.get('end_timestamp'),
                    sr.get('duration_seconds'), sr.get('status'),
                ),
            )
    conn.commit()


def insert_variances(conn, project_id: str, upload_id: str, variances: list[dict]) -> None:
    """Bulk-insert variance records. Silently skips duplicates."""
    if not variances:
        return
    with conn.cursor() as cur:
        for v in variances:
            cur.execute(
                """INSERT INTO variances (
                       project_id, upload_id, variance_id, test_case_id,
                       variance_reason, variance_start, variance_end,
                       variance_current_status, dismissed_date, is_active
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (upload_id, variance_id) DO NOTHING""",
                (
                    project_id, upload_id,
                    v.get('variance_id'), v.get('test_case_id'),
                    v.get('variance_reason'),
                    v.get('variance_start'), v.get('variance_end'),
                    v.get('variance_current_status'),
                    v.get('dismissed_date'),
                    v.get('is_active', False),
                ),
            )
    conn.commit()


def get_testcase_runs(conn, project_id: str, upload_id: str = None) -> list[dict]:
    """Return testcase runs for a project, optionally filtered by upload."""
    if upload_id:
        return rows(conn,
            "SELECT * FROM testcase_runs WHERE project_id=%s AND upload_id=%s ORDER BY start_timestamp",
            (project_id, upload_id),
        )
    return rows(conn,
        "SELECT * FROM testcase_runs WHERE project_id=%s ORDER BY start_timestamp",
        (project_id,),
    )


def get_suite_runs(conn, project_id: str, upload_id: str = None) -> list[dict]:
    """Return suite runs for a project, optionally filtered by upload."""
    if upload_id:
        return rows(conn,
            "SELECT * FROM suite_runs WHERE project_id=%s AND upload_id=%s ORDER BY start_timestamp",
            (project_id, upload_id),
        )
    return rows(conn,
        "SELECT * FROM suite_runs WHERE project_id=%s ORDER BY start_timestamp",
        (project_id,),
    )


def get_variances(conn, project_id: str) -> list[dict]:
    """Return all variances for a project."""
    return rows(conn,
        "SELECT * FROM variances WHERE project_id=%s ORDER BY variance_start",
        (project_id,),
    )
