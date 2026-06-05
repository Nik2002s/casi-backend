"""
CASI — Enterprise leads blueprint.

Routes:
  POST /api/leads   capture a demo/enterprise request lead (public, no auth required)
"""

import os
import re
import logging

import requests as http_req
from flask import Blueprint, request, jsonify

import db
from app import limiter

bp = Blueprint('leads', __name__, url_prefix='/api')

log = logging.getLogger(__name__)

# ── reCAPTCHA v2 verification ─────────────────────────────────────────────────────
if not os.environ.get('RECAPTCHA_SECRET_KEY'):
    log.warning(
        '[CASI] RECAPTCHA_SECRET_KEY is not set — captcha verification is DISABLED. '
        'Set this environment variable in production to protect /api/leads.'
    )

_RECAPTCHA_SECRET = os.environ.get('RECAPTCHA_SECRET_KEY', '')
_RECAPTCHA_VERIFY = 'https://www.google.com/recaptcha/api/siteverify'
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _verify_captcha(token: str) -> bool:
    """Returns True when the reCAPTCHA token is valid, or when no secret is configured (dev mode)."""
    if not _RECAPTCHA_SECRET:
        return True  # dev mode: skip verification
    if not token:
        return False
    try:
        resp = http_req.post(
            _RECAPTCHA_VERIFY,
            data={'secret': _RECAPTCHA_SECRET, 'response': token},
            timeout=5,
        )
        return bool(resp.json().get('success'))
    except Exception:
        return False


@bp.route('/leads', methods=['POST'])
@limiter.limit('10 per hour')
def create_lead():
    data    = request.get_json(silent=True) or {}
    name    = (data.get('name')    or '').strip()[:200]
    email   = (data.get('email')   or '').strip()[:200]
    company = (data.get('company') or '').strip()[:200]
    message = (data.get('message') or '').strip()[:2000]
    captcha = (data.get('captcha_token') or '').strip()

    if not name or not email or not company:
        return jsonify({'error': 'name, email, and company are required'}), 400
    if not _EMAIL_RE.match(email):
        return jsonify({'error': 'Invalid email address'}), 400
    if not _verify_captcha(captcha):
        return jsonify({'error': 'CAPTCHA verification failed. Please try again.'}), 400

    conn = db.get_db()
    try:
        # ── Per-email 24h cooldown ──────────────────────────────────────────────────
        # Prevents the same email from being inserted more than once per day
        # even if the IP-based rate limit is bypassed via proxy rotation.
        recent = db.row(conn, """
            SELECT 1 FROM leads
             WHERE LOWER(email) = LOWER(%s)
               AND created_at > NOW() - INTERVAL '24 hours'
        """, (email,))
        if recent:
            # Return 201 so we don't leak whether the email is known.
            return jsonify({'queued': True}), 201

        result = db.save_lead(conn, name, email, company, message, None)
    finally:
        conn.close()

    return jsonify({'id': str(result['id']), 'created_at': str(result['created_at'])}), 201
