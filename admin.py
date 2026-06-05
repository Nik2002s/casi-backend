#!/usr/bin/env python3
"""
CASI Admin CLI — manage users and app configuration.

Usage:
  python admin.py add-user <email>             # Add user to DB allowlist (instant access)
  python admin.py remove-user <email>          # Remove user from DB allowlist
  python admin.py list-db-users               # List all users in DB allowlist
  python admin.py approve <email>             # Add to DB + set Firebase approved claim
  python admin.py revoke <email>              # Remove from DB + revoke Firebase session
  python admin.py list-users                  # List all Firebase users + approval status
  python admin.py set-limit <number>          # Set max uploads per user (e.g. 5)
  python admin.py show-config                 # Show current app config

Bootstrap (first admin):
  1. Set ADMIN_EMAILS=your@email.com in your backend .env / Railway env vars.
     Admins bypass the allowlist check — you can log in immediately.
  2. Once logged in, use the Admin panel (UI) to add other users.
  3. OR run: python admin.py add-user other@email.com  (requires DATABASE_URL)

Requirements:
  DATABASE_URL env var must be set for DB commands.
  FIREBASE_SERVICE_ACCOUNT_KEY env var must be set for Firebase commands.
"""

import os
import sys
import json


# ── Firebase helpers (optional) ───────────────────────────────────────────────

def get_firebase_auth():
    key_json = os.environ.get('FIREBASE_SERVICE_ACCOUNT_KEY', '').strip()
    if not key_json:
        print('❌  FIREBASE_SERVICE_ACCOUNT_KEY is not set.')
        sys.exit(1)
    import firebase_admin
    from firebase_admin import credentials, auth
    if not firebase_admin._apps:
        cred = credentials.Certificate(json.loads(key_json))
        firebase_admin.initialize_app(cred)
    return auth


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db_conn():
    """Return a DB connection or exit with a clear message."""
    db_url = os.environ.get('DATABASE_URL', '').strip()
    if not db_url:
        print('❌  DATABASE_URL is not set.')
        sys.exit(1)
    try:
        import db
        return db.get_db()
    except Exception as exc:
        print(f'❌  Cannot connect to database: {exc}')
        sys.exit(1)


# ── Commands ──────────────────────────────────────────────────────────────────

def add_db_user(email: str, added_by: str = 'cli'):
    """Add a user to the PostgreSQL allowed_users table (grants immediate login access)."""
    email = email.strip().lower()
    if not email or '@' not in email:
        print('❌  Provide a valid email address.')
        sys.exit(1)

    import db
    conn = get_db_conn()
    try:
        db.add_allowed_user(conn, email, added_by=added_by)
        print(f'✅  Added to allowlist: {email}')
        print('    User can now log in immediately.')
    except Exception as exc:
        print(f'❌  DB error: {exc}')
    finally:
        conn.close()


def remove_db_user(email: str):
    """Remove a user from the PostgreSQL allowed_users table (blocks login immediately)."""
    email = email.strip().lower()
    import db
    conn = get_db_conn()
    try:
        db.remove_allowed_user(conn, email)
        print(f'🚫  Removed from allowlist: {email}')
        print('    User will be blocked on next request.')
    except Exception as exc:
        print(f'❌  DB error: {exc}')
    finally:
        conn.close()


def list_db_users():
    """List all users in the PostgreSQL allowed_users table."""
    import db
    conn = get_db_conn()
    try:
        users = db.list_allowed_users(conn)
    finally:
        conn.close()

    if not users:
        print('\n  (no users in allowlist — set ADMIN_EMAILS to bootstrap)\n')
        return

    print(f'\n{"EMAIL":<35} {"ADDED BY":<20} {"ADDED AT":<25} {"LAST LOGIN"}')
    print('-' * 95)
    for u in users:
        added   = str(u.get('added_at', ''))[:19]
        login   = str(u.get('last_login', '') or '—')[:19]
        by      = (u.get('added_by') or '—')[:18]
        print(f'{u["email"]:<35} {by:<20} {added:<25} {login}')
    print()


def approve_user(email: str):
    """Add to DB allowlist AND set Firebase approved custom claim."""
    email = email.strip().lower()

    # Step 1 — DB allowlist (primary auth gate)
    import db
    conn = get_db_conn()
    try:
        db.add_allowed_user(conn, email, added_by='cli:approve')
        print(f'✅  Added to DB allowlist: {email}')
    except Exception as exc:
        print(f'⚠️   DB error (continuing): {exc}')
    finally:
        conn.close()

    # Step 2 — Firebase custom claim (optional, for legacy compatibility)
    try:
        auth = get_firebase_auth()
        user = auth.get_user_by_email(email)
        auth.set_custom_user_claims(user.uid, {'approved': True})
        print(f'✅  Firebase claim set: approved=True (uid={user.uid})')
    except SystemExit:
        print('ℹ️   Skipped Firebase claim (FIREBASE_SERVICE_ACCOUNT_KEY not set).')
    except Exception as exc:
        print(f'⚠️   Firebase claim failed (user still has DB access): {exc}')

    print(f'\n    {email} can now log in immediately.')


def revoke_user(email: str):
    """Remove from DB allowlist AND revoke Firebase session."""
    email = email.strip().lower()

    # Step 1 — DB allowlist
    import db
    conn = get_db_conn()
    try:
        db.remove_allowed_user(conn, email)
        print(f'🚫  Removed from DB allowlist: {email}')
    except Exception as exc:
        print(f'⚠️   DB error (continuing): {exc}')
    finally:
        conn.close()

    # Step 2 — Firebase revocation
    try:
        auth = get_firebase_auth()
        user = auth.get_user_by_email(email)
        auth.set_custom_user_claims(user.uid, {'approved': False})
        auth.revoke_refresh_tokens(user.uid)
        print(f'🚫  Firebase session revoked (uid={user.uid})')
    except SystemExit:
        print('ℹ️   Skipped Firebase revocation (FIREBASE_SERVICE_ACCOUNT_KEY not set).')
    except Exception as exc:
        print(f'⚠️   Firebase revocation failed: {exc}')

    print(f'\n    {email} is now blocked.')


def list_users():
    """List all Firebase users with their approval status and whether they're in the DB."""
    auth = get_firebase_auth()

    # Load DB allowlist for cross-reference
    db_emails = set()
    try:
        import db
        conn = get_db_conn()
        try:
            users_db = db.list_allowed_users(conn)
            db_emails = {u['email'].lower() for u in users_db}
        finally:
            conn.close()
    except Exception:
        pass

    print(f'\n{"EMAIL":<35} {"UID":<28} {"DB LIST":<10} {"FB CLAIM":<10} {"DISABLED"}')
    print('-' * 95)
    page = auth.list_users()
    while page:
        for user in page.users:
            claims   = user.custom_claims or {}
            fb_claim = '✅ yes' if claims.get('approved') else '❌ no'
            in_db    = '✅ yes' if (user.email or '').lower() in db_emails else '❌ no'
            disabled = '🚫 yes' if user.disabled else 'no'
            print(f'{user.email or "(no email)":<35} {user.uid:<28} {in_db:<10} {fb_claim:<10} {disabled}')
        page = page.get_next_page()
    print()


def set_limit(value: str):
    try:
        n = int(value)
        assert n > 0
    except Exception:
        print(f'❌  Invalid value: {value!r}. Must be a positive integer.')
        sys.exit(1)

    import db
    conn = get_db_conn()
    try:
        db.set_config(conn, 'max_uploads_per_user', n)
        print(f'✅  max_uploads_per_user set to {n}')
    finally:
        conn.close()


def show_config():
    import db
    conn = get_db_conn()
    try:
        rows = db.rows(conn, "SELECT key, value, updated_at FROM app_config ORDER BY key")
        print(f'\n{"KEY":<30} {"VALUE":<15} {"UPDATED"}')
        print('-' * 65)
        for r in rows:
            print(f'{r["key"]:<30} {r["value"]:<15} {r["updated_at"]}')
        print()
    finally:
        conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    'add-user':     (add_db_user,    '<email>'),
    'remove-user':  (remove_db_user, '<email>'),
    'list-db-users':(list_db_users,  ''),
    'approve':      (approve_user,   '<email>'),
    'revoke':       (revoke_user,    '<email>'),
    'list-users':   (list_users,     ''),
    'set-limit':    (set_limit,      '<number>'),
    'show-config':  (show_config,    ''),
}

if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print('Commands:')
        for cmd, (_, arg) in COMMANDS.items():
            print(f'  python admin.py {cmd} {arg}')
        sys.exit(1)

    cmd = sys.argv[1]
    fn, arg_hint = COMMANDS[cmd]
    if arg_hint and len(sys.argv) < 3:
        print(f'Usage: python admin.py {cmd} {arg_hint}')
        sys.exit(1)

    fn(*sys.argv[2:]) if arg_hint else fn()
