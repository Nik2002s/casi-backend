"""
CASI — User search endpoint.

GET /api/users/search?q=<query>
  Returns allowed_users who have opted in to sharing (allow_sharing = TRUE)
  and whose display_name or email matches the query (case-insensitive ILIKE).
  Admins can search ALL users regardless of allow_sharing.

  Response: [{ email, display_name, first_name }, ...]
"""

from flask import Blueprint, request, jsonify, g
import db
from routes.auth import require_auth, is_admin

bp = Blueprint('users', __name__, url_prefix='/api/users')


@bp.route('/search')
@require_auth
def search_users():
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify([])

    conn = db.get_db()
    try:
        pattern = f'%{q}%'
        if is_admin(g.user_email):
            # Admins can find everyone
            results = db.rows(conn, """
                SELECT email,
                       COALESCE(display_name, email) AS display_name
                FROM allowed_users
                WHERE (display_name ILIKE %s OR email ILIKE %s)
                  AND LOWER(email) != LOWER(%s)
                ORDER BY display_name
                LIMIT 10
            """, (pattern, pattern, g.user_email or ''))
        else:
            # Regular users only see people who opted in
            results = db.rows(conn, """
                SELECT email,
                       COALESCE(display_name, email) AS display_name
                FROM allowed_users
                WHERE allow_sharing = TRUE
                  AND (display_name ILIKE %s OR email ILIKE %s)
                  AND LOWER(email) != LOWER(%s)
                ORDER BY display_name
                LIMIT 10
            """, (pattern, pattern, g.user_email or ''))
    finally:
        conn.close()

    # Add first_name convenience field (first word of display_name)
    for r in results:
        name = r['display_name'] or ''
        r['first_name'] = name.split('@')[0].split(' ')[0] if name else ''

    return jsonify(results)
