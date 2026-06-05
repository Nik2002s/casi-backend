"""
CASI — Auth middleware.

Supports two auth methods:
  1. Firebase ID token  → Authorization: Bearer <token>   (web UI)
  2. CASI API key       → X-CASI-Key: sk-casi-...          (programmatic)

User approval flow:
  - New Google sign-ins are blocked until you set approved=true via Firebase custom claim.
  - Run: python admin.py approve user@email.com
  - Or disable/enable users directly in Firebase Console → Authentication → Users.

Dev mode:
  - If FIREBASE_SERVICE_ACCOUNT_KEY is not set, auth is bypassed (local dev only).
"""

import os
import re
import json
import functools

from flask import request, jsonify, g

# ── UUID validation regex ──────────────────────────────────────────────────────
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def is_valid_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value or ''))

# ── Firebase Admin init (lazy, once) ──────────────────────────────────────────

_firebase_app = None


def _get_firebase():
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    key_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY', '').strip()
    if not key_json:
        return None  # dev mode — no Firebase

    try:
        import firebase_admin
        from firebase_admin import credentials

        if not firebase_admin._apps:
            cred = credentials.Certificate(json.loads(key_json))
            _firebase_app = firebase_admin.initialize_app(cred)
        else:
            _firebase_app = firebase_admin.get_app()

        print('[CASI] Firebase Admin SDK initialized ✓')
        return _firebase_app
    except Exception as exc:
        print(f'[CASI] Firebase Admin init failed: {exc}')
        return None


def _verify_firebase_token(token: str) -> dict:
    """
    Verify Firebase ID token.

    Access flow:
      1. Firebase checks token validity + revocation + disabled status.
      2. If the email is in ADMIN_EMAILS → mark as admin.
      3. Auto-register / touch last_login in the users table for tracking.
         All valid Google sign-ins are accepted — no allowlist gate.

    Dev mode (no FIREBASE_SERVICE_ACCOUNT_KEY set) → bypass all checks.
    """
    app = _get_firebase()
    if app is None:
        # Dev mode: accept any token, synthesize an approved user
        return {'uid': 'dev_user', 'email': 'dev@localhost', 'approved': True}

    from firebase_admin import auth as fb_auth
    try:
        decoded = fb_auth.verify_id_token(token, check_revoked=True)
    except fb_auth.RevokedIdTokenError:
        raise PermissionError('Token revoked — please sign in again.')
    except fb_auth.UserDisabledError:
        raise PermissionError('Account disabled. Contact your admin.')
    except Exception as exc:
        raise ValueError(f'Invalid token: {exc}')

    user_email = (decoded.get('email') or '').lower()
    display_name = decoded.get('name') or ''

    # ── Mark admins ───────────────────────────────────────────────────────────
    if user_email in _ADMIN_EMAILS_SET:
        decoded['is_admin'] = True

    # ── Auto-register / update last_login (non-fatal) ─────────────────────────
    try:
        from db import get_db, add_allowed_user, touch_user_login
        conn = get_db()
        try:
            add_allowed_user(conn, user_email, added_by='system:self')
            touch_user_login(conn, user_email, display_name=display_name)
        finally:
            conn.close()
    except Exception:
        pass  # non-fatal — never block login due to DB issues

    return decoded


def is_admin(email: str) -> bool:
    """Check if email is in ADMIN_EMAILS env var."""
    return (email or '').lower() in _ADMIN_EMAILS_SET


def _reload_admin_emails():
    """Re-parse ADMIN_EMAILS — useful in tests where the env var changes."""
    global _ADMIN_EMAILS_SET
    _ADMIN_EMAILS_SET = frozenset(
        e.strip().lower()
        for e in os.environ.get('ADMIN_EMAILS', '').split(',')
        if e.strip()
    )


# Parse once at module load — avoids repeated os.environ lookups on hot paths
_ADMIN_EMAILS_SET: frozenset = frozenset()
_reload_admin_emails()


# ── Decorators ────────────────────────────────────────────────────────────────

def require_auth(f):
    """
    Accept Firebase token OR CASI API key.
    Sets g.user_id, g.user_email, g.auth_method on success.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # ── 1. Firebase Bearer token ──────────────────────────────────────────
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
            try:
                decoded = _verify_firebase_token(token)
                g.user_id     = decoded['uid']
                g.user_email  = decoded.get('email', '')
                g.auth_method = 'firebase'
                return f(*args, **kwargs)
            except PermissionError as exc:
                return jsonify({'error': str(exc)}), 403
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 401

        # ── 2. CASI API key (programmatic ingestion) ──────────────────────────
        raw_key = request.headers.get('X-CASI-Key', '').strip()
        if raw_key:
            from db import get_db, resolve_api_key
            try:
                conn = get_db()
            except Exception as exc:
                return jsonify({'error': f'DB unavailable: {exc}'}), 503
            try:
                project_id = resolve_api_key(conn, raw_key)
            finally:
                conn.close()

            if not project_id:
                return jsonify({'error': 'Invalid or revoked API key'}), 401

            g.project_id  = str(project_id)
            g.user_id     = f'api:{project_id}'
            g.user_email  = ''
            g.auth_method = 'api_key'
            return f(*args, **kwargs)

        # ── 3. Dev mode bypass (no Firebase configured) ───────────────────────
        if _get_firebase() is None:
            g.user_id     = 'dev_user'
            g.user_email  = 'dev@localhost'
            g.auth_method = 'dev'
            return f(*args, **kwargs)

        return jsonify({
            'error': 'Authentication required. Include Authorization: Bearer <firebase_token>'
        }), 401

    return decorated


def require_api_key(f):
    """Legacy alias — now delegates to require_auth."""
    return require_auth(f)


def guard_project_access(project_id: str, conn, write: bool = False):
    """
    Enforce project-level access control from any route handler.

    Call this after opening a DB connection. Returns a (Response, status_code)
    tuple when access is denied, or None when access is allowed.

    Access model
    ────────────
    API key auth   — key must belong to this exact project (403 if not).
                     Write routes: API key already implies ownership.
    Firebase auth  — READ:  user must own, share, or project must be public (404 if not).
                     WRITE: user must be the project owner (403 if not).
    Dev mode       — all access allowed (local only, no Firebase configured).
    """
    import db

    # Reject malformed IDs before they reach PostgreSQL
    if not is_valid_uuid(project_id):
        return jsonify({'error': 'Project not found'}), 404

    auth_method = getattr(g, 'auth_method', None)
    user_email  = getattr(g, 'user_email', '')

    # ── API key: key must be bound to this project ────────────────────────────
    if auth_method == 'api_key':
        if getattr(g, 'project_id', None) != project_id:
            return jsonify({'error': 'API key does not belong to this project'}), 403
        return None  # API key proves project ownership — read & write both allowed

    # ── Firebase / dev: check project visibility ──────────────────────────────
    if not db.can_access_project(conn, project_id, user_email):
        return jsonify({'error': 'Project not found'}), 404

    if write and not db.is_project_owner(conn, project_id, user_email):
        return jsonify({'error': 'Only the project owner can perform this action'}), 403

    return None
