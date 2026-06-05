"""
CASI Tool — Flask entry point.

Blueprint routes:
  /api/projects          → routes/projects.py
  /api/projects/*/runs   → routes/runs.py
  /api/projects/*/chat   → routes/chat.py
  /api/me                → routes/chat.py (me_bp)
  /api/gate, /api/admin, /api/test-execution, etc.
"""

import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ── App setup ──────────────────────────────────────────────────────────────────

app = Flask(__name__)

# ── File size limit (25 MB) — Flask returns 413 automatically above this ──────
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Default: 200 requests per hour per IP across all endpoints.
# Sensitive write endpoints apply tighter per-route overrides.
# Storage defaults to in-memory (suitable for single-process Railway deploys).
# Disabled when RATELIMIT_ENABLED=false (set in test environments).
_rl_enabled = os.environ.get('RATELIMIT_ENABLED', 'true').lower() not in ('false', '0', 'no')
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=['200 per hour', '30 per minute'] if _rl_enabled else [],
    headers_enabled=True,    # adds X-RateLimit-* response headers
    enabled=_rl_enabled,
)

# ── CORS ─────────────────────────────────────────────────────────────────────────────
# Explicit origins are required when supports_credentials=True.
# Set CORS_ORIGINS in Railway to a comma-separated list (e.g.
# https://casi-64bdd.web.app,https://casi-64bdd.firebaseapp.com).
# Falls back to the known Firebase Hosting domains for this project.
_cors_env = os.environ.get('CORS_ORIGINS', '').strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(',') if o.strip()]
else:
    _cors_origins = [
        'https://casi-64bdd.web.app',
        'https://casi-64bdd.firebaseapp.com',
    ]
    print('[CASI] CORS_ORIGINS not set — defaulting to Firebase Hosting domains')

if os.environ.get('FLASK_ENV') == 'development':
    _cors_origins += ['http://localhost:5173', 'http://localhost:5174']

CORS(
    app,
    origins=_cors_origins,
    supports_credentials=True,
    allow_headers=['Content-Type', 'Authorization', 'X-CASI-Key'],
    methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
)

_key = os.environ.get('ANTHROPIC_API_KEY', '')
print(f"[CASI] Anthropic API key: {'SET' if _key else 'NOT SET — fallback mode'}")

# Register blueprints
from routes.projects import bp as projects_bp
from routes.runs import bp as runs_bp
from routes.chat import bp as chat_bp, me_bp
from routes.gate import bp as gate_bp
from routes.admin import bp as admin_bp
from routes.test_execution import bp as test_execution_bp
from routes.template import bp as template_bp
from routes.users import bp as users_bp
from routes.leads import bp as leads_bp

app.register_blueprint(projects_bp)
app.register_blueprint(runs_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(me_bp)
app.register_blueprint(gate_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(test_execution_bp)
app.register_blueprint(template_bp)
app.register_blueprint(users_bp)
app.register_blueprint(leads_bp)

# ── Schema initialisation — run once at startup, not on every connection ──────
try:
    from db import init_schema
    init_schema()
    print('[CASI] Database schema initialised ✓')
except Exception as _schema_exc:
    print(f'[CASI] WARNING: Schema init failed — {_schema_exc}')
    # Do not abort startup; Railway may start before DB is ready on cold deploy.


# ── Security response headers ─────────────────────────────────────────────────
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options']  = 'nosniff'
    response.headers['X-Frame-Options']         = 'DENY'
    response.headers['Referrer-Policy']         = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy']      = 'geolocation=(), microphone=(), camera=()'
    # Strict-Transport-Security: enforce HTTPS for 1 year (Railway is always HTTPS)
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    # Content-Security-Policy: API-only backend — no HTML pages served
    if not response.content_type.startswith('text/html'):
        response.headers['Content-Security-Policy'] = "default-src 'none'"
    return response


# ── Global error handlers — always return JSON, never HTML ─────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    app.logger.exception('Unhandled exception')
    return jsonify({'error': 'An unexpected error occurred'}), 500


@app.errorhandler(413)
def handle_413(e):
    return jsonify({'error': 'File too large. Maximum upload size is 25 MB.'}), 413


@app.errorhandler(429)
def handle_429(e):
    return jsonify({'error': 'Too many requests. Please slow down and try again shortly.'}), 429


@app.errorhandler(404)
def handle_404(e):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(405)
def handle_405(e):
    return jsonify({'error': 'Method not allowed'}), 405


# ── Health check ──────────────────────────────────────────────────────────────
@app.route('/api/health', methods=['GET'])
def health():
    db_ok = False
    try:
        from db import get_db
        conn = get_db()
        db_ok = True
        conn.close()
    except Exception:
        pass
    # Do not expose whether optional services are configured — that is internal info.
    return jsonify({'status': 'ok', 'db': db_ok})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    debug = os.environ.get('FLASK_ENV', 'production') == 'development'
    app.run(debug=debug, port=port, host='0.0.0.0')
