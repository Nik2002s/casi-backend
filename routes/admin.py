"""
CASI — Admin blueprint.

Routes (all require admin auth — email must be in ADMIN_EMAILS env var):
  GET    /api/admin/users              list allowed users
  POST   /api/admin/users              add a user by email
  DELETE /api/admin/users/<email>      remove a user
  GET    /api/admin/config             get app config
  POST   /api/admin/config             update config key/value
"""

import functools
from flask import Blueprint, request, jsonify, g

import db
from routes.auth import require_auth, is_admin

bp = Blueprint('admin', __name__, url_prefix='/api/admin')


def require_admin(f):
    """Require Firebase auth AND admin email."""
    @functools.wraps(f)
    @require_auth
    def decorated(*args, **kwargs):
        if not is_admin(getattr(g, 'user_email', '')):
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated


def _conn():
    return db.get_db()


# ── Users ─────────────────────────────────────────────────────────────────────

@bp.route('/users', methods=['GET'])
@require_admin
def list_users():
    conn = _conn()
    try:
        users = db.list_allowed_users(conn)
    finally:
        conn.close()
    return jsonify(users)


@bp.route('/users', methods=['POST'])
@require_admin
def add_user():
    data  = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email is required'}), 400

    conn = _conn()
    try:
        db.add_allowed_user(conn, email, added_by=g.user_email)
    finally:
        conn.close()

    return jsonify({'added': email}), 201


@bp.route('/users/<path:email>', methods=['DELETE'])
@require_admin
def remove_user(email):
    email = email.strip().lower()
    conn  = _conn()
    try:
        db.remove_allowed_user(conn, email)
    finally:
        conn.close()
    return jsonify({'removed': email})


# ── Config ────────────────────────────────────────────────────────────────────

@bp.route('/config', methods=['GET'])
@require_admin
def get_config():
    conn = _conn()
    try:
        config_rows = db.rows(conn, "SELECT key, value, updated_at FROM app_config ORDER BY key")
    finally:
        conn.close()
    return jsonify(config_rows)


@bp.route('/config', methods=['POST'])
@require_admin
def set_config():
    _ALLOWED_CONFIG_KEYS = {
        'ai_daily_requests',
        'ai_daily_tokens',
        'ai_weekly_tokens',
        'max_uploads_per_user',
    }
    data  = request.get_json(silent=True) or {}
    key   = (data.get('key') or '').strip()
    value = str(data.get('value', '')).strip()
    if not key:
        return jsonify({'error': 'key is required'}), 400
    if key not in _ALLOWED_CONFIG_KEYS:
        return jsonify({'error': f"Unknown config key '{key}'. Allowed: {sorted(_ALLOWED_CONFIG_KEYS)}"}), 400

    conn = _conn()
    try:
        db.set_config(conn, key, value)
    finally:
        conn.close()
    return jsonify({'key': key, 'value': value})


# ── AI Usage ──────────────────────────────────────────────────────────────────

@bp.route('/ai-usage', methods=['GET'])
@require_admin
def ai_usage():
    """Per-user AI token and cost breakdown across multiple time windows."""
    conn = _conn()
    try:
        usage_rows = db.rows(conn, """
            SELECT
                user_email                                                          AS email,
                COALESCE(SUM(input_tokens)  FILTER (WHERE created_at >= date_trunc('day',  NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS today_input,
                COALESCE(SUM(output_tokens) FILTER (WHERE created_at >= date_trunc('day',  NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS today_output,
                COALESCE(COUNT(*)           FILTER (WHERE created_at >= date_trunc('day',  NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS today_requests,
                COALESCE(SUM(input_tokens)  FILTER (WHERE created_at >= NOW() - INTERVAL '7 days'), 0) AS week_input,
                COALESCE(SUM(output_tokens) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days'), 0) AS week_output,
                COALESCE(COUNT(*)           FILTER (WHERE created_at >= NOW() - INTERVAL '7 days'), 0) AS week_requests,
                COALESCE(SUM(input_tokens)  FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'), 0) AS month_input,
                COALESCE(SUM(output_tokens) FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'), 0) AS month_output,
                COALESCE(COUNT(*)           FILTER (WHERE created_at >= NOW() - INTERVAL '30 days'), 0) AS month_requests,
                COALESCE(SUM(input_tokens),  0) AS total_input,
                COALESCE(SUM(output_tokens), 0) AS total_output,
                COALESCE(COUNT(*),           0) AS total_requests
            FROM ai_usage_log
            GROUP BY user_email
            ORDER BY COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) DESC
        """)
        # Ensure all values are plain Python ints for JSON serialisation
        result = []
        for r in usage_rows:
            result.append({k: (int(v) if isinstance(v, (int, float)) and k != 'email' else v) for k, v in r.items()})
    finally:
        conn.close()
    return jsonify(result)


# ── AI Limits ─────────────────────────────────────────────────────────────────

@bp.route('/ai-limits', methods=['GET'])
@require_admin
def get_ai_limits():
    """Return current AI quota limits."""
    conn = _conn()
    try:
        daily_requests = int(db.get_config(conn, 'ai_daily_requests', '10'))
        daily_tokens   = int(db.get_config(conn, 'ai_daily_tokens',   '15000'))
        weekly_tokens  = int(db.get_config(conn, 'ai_weekly_tokens',  '75000'))
    finally:
        conn.close()
    return jsonify({
        'daily_requests': daily_requests,
        'daily_tokens':   daily_tokens,
        'weekly_tokens':  weekly_tokens,
    })


@bp.route('/ai-limits', methods=['POST'])
@require_admin
def set_ai_limits():
    """Update AI quota limits."""
    data = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        if 'daily_requests' in data:
            db.set_config(conn, 'ai_daily_requests', str(int(data['daily_requests'])))
        if 'daily_tokens' in data:
            db.set_config(conn, 'ai_daily_tokens', str(int(data['daily_tokens'])))
        if 'weekly_tokens' in data:
            db.set_config(conn, 'ai_weekly_tokens', str(int(data['weekly_tokens'])))
        daily_requests = int(db.get_config(conn, 'ai_daily_requests', '10'))
        daily_tokens   = int(db.get_config(conn, 'ai_daily_tokens',   '15000'))
        weekly_tokens  = int(db.get_config(conn, 'ai_weekly_tokens',  '75000'))
    finally:
        conn.close()
    return jsonify({
        'daily_requests': daily_requests,
        'daily_tokens':   daily_tokens,
        'weekly_tokens':  weekly_tokens,
    })
