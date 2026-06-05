"""
CASI — AI Chat blueprint.

Routes:
  POST   /api/projects/<id>/chat         send message, get AI reply
  GET    /api/projects/<id>/chat         fetch message history
  DELETE /api/projects/<id>/chat         clear history

The assistant is given the latest run context (scores, open failures)
so every reply is grounded in real project data.
"""

import os
import json
from flask import Blueprint, request, jsonify, g, current_app

import db
from app import limiter
from routes.auth import require_auth, is_admin, guard_project_access

bp = Blueprint('chat', __name__, url_prefix='/api/projects')

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')

SYSTEM_PROMPT = """\
You are CASI-AI, an expert engineering maturity assistant embedded in the
CASI dashboard. You help engineering teams understand maturity scores,
diagnose failures, and make release decisions.

When answering:
- Be concise and actionable — teams read you under time pressure.
- Ground your answers in the project data provided (scores, failures, sprints).
- If a score is below 700, proactively suggest the top 1-2 actions.
- Use plain language; avoid excessive jargon.
- Never hallucinate TC IDs or metric values not present in the context.
"""


def _build_context(project_id: str, conn) -> str:
    """Build a compact context string from the latest run."""
    run = db.get_latest_run(conn, project_id)
    if not run:
        return 'No runs found for this project yet.'

    result = run.get('result') or {}
    scores = result.get('scores', {})
    dataset = result.get('dataset', {})
    history = result.get('sprint_history', [])
    failures = result.get('open_failures', [])

    last_sprint = history[-1] if history else {}

    lines = [
        f"Project latest run: {run.get('filename', 'unknown')} (computed {run.get('computed_at', '')})",
        f"CASI score: {scores.get('casi_score', 'N/A')} ({scores.get('casi_gate', '')})",
        f"ASI score: {scores.get('asi_score', 'N/A')} ({scores.get('asi_gate', '')})",
        f"Total TCs: {dataset.get('tc_count', 'N/A')}, Modules: {len(dataset.get('modules', []))}",
        f"Sprint range: {last_sprint.get('sprint_start', '')} to {last_sprint.get('sprint_end', '')}",
        f"Failing TCs (last sprint): {last_sprint.get('n_fail', 0)}",
    ]

    if failures:
        lines.append('Top open failures:')
        for f in failures[:5]:
            lines.append(
                f"  - {f.get('tc_id', '')}: {f.get('name', '')} "
                f"({f.get('priority', '')}, {f.get('days_open', 0)} days open)"
            )

    return '\n'.join(lines)


MODEL = 'claude-haiku-4-5-20251001'


def _call_claude(messages: list, context: str):
    """Call Claude and return (reply_text, input_tokens, output_tokens)."""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system = SYSTEM_PROMPT + '\n\nProject context:\n' + context

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system,
        messages=messages,
    )
    text = resp.content[0].text
    usage = resp.usage
    return text, getattr(usage, 'input_tokens', 0), getattr(usage, 'output_tokens', 0)


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route('/<project_id>/chat', methods=['GET'])
@require_auth
def get_chat(project_id):

    conn = db.get_db()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        msgs = db.get_messages(conn, project_id, limit=100)
    finally:
        conn.close()
    return jsonify(msgs)


@bp.route('/<project_id>/chat', methods=['POST'])
@require_auth
@limiter.limit('60 per hour')
def send_message(project_id):

    data = request.get_json(silent=True) or {}
    user_msg = (data.get('message') or '').strip()
    if not user_msg:
        return jsonify({'error': 'message is required'}), 400
    if len(user_msg) > 4000:
        return jsonify({'error': 'Message must be 4000 characters or fewer'}), 400

    conn = db.get_db()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard

        # Check quota before calling Claude (admins bypass limits)
        if not is_admin(getattr(g, 'user_email', '')):
            quota = db.check_ai_quota(conn, getattr(g, 'user_email', ''))
            if not quota['allowed']:
                return jsonify({'error': 'quota_exceeded', **quota}), 429

        # Persist user message
        db.save_message(conn, project_id, 'user', user_msg)

        # Build history for Claude (last 20 messages)
        history = db.get_messages(conn, project_id, limit=20)
        claude_msgs = [
            {'role': m['role'], 'content': m['content']}
            for m in history
        ]

        if not ANTHROPIC_API_KEY:
            reply = (
                'CASI-AI is not configured — set ANTHROPIC_API_KEY to enable '
                'AI chat. In the meantime, review the Diagnostic tab for '
                'automated root-cause analysis.'
            )
        else:
            try:
                context = _build_context(project_id, conn)
                # source is determined server-side from the route, not from the
                # request body, to prevent callers from spoofing usage attribution.
                source = 'chat'
                reply, in_tok, out_tok = _call_claude(claude_msgs, context)
                # Log token usage attributed to the calling user
                try:
                    db.log_ai_usage(
                        conn,
                        user_email  = getattr(g, 'user_email', ''),
                        project_id  = project_id,
                        source      = source,
                        input_tokens= in_tok,
                        output_tokens=out_tok,
                        model       = MODEL,
                    )
                except Exception:
                    pass   # never let logging break the main flow
            except Exception as exc:
                current_app.logger.exception('AI call failed for project %s', project_id)
                reply = 'AI service error. Please try again.'

        # Persist assistant reply
        db.save_message(conn, project_id, 'assistant', reply)

    finally:
        conn.close()

    return jsonify({'role': 'assistant', 'content': reply})


@bp.route('/<project_id>/chat', methods=['DELETE'])
@require_auth
def clear_chat(project_id):

    conn = db.get_db()
    try:
        guard = guard_project_access(project_id, conn, write=True)
        if guard:
            return guard
        db.clear_messages(conn, project_id)
    finally:
        conn.close()
    return jsonify({'cleared': True})


# ── User-level usage stats (separate blueprint at /api) ───────────────────────

me_bp = Blueprint('me', __name__, url_prefix='/api/me')


@me_bp.route('', methods=['GET'])
@require_auth
def me_profile():
    """Return current user's identity, admin flag, and terms acceptance status."""
    email = getattr(g, 'user_email', '')
    conn = db.get_db()
    try:
        au = db.row(conn,
            "SELECT display_name FROM allowed_users WHERE LOWER(email) = LOWER(%s)",
            (email,),
        )
        terms = db.get_user_terms_status(conn, email)
    finally:
        conn.close()
    return jsonify({
        'email':            email,
        'is_admin':         is_admin(email),
        'display_name':     (au or {}).get('display_name') or email,
        'terms_accepted':   terms['terms_accepted'],
        'privacy_accepted': terms['privacy_accepted'],
        'needs_acceptance': terms['needs_acceptance'],
        'terms_version':    db.TERMS_VERSION,
    })


@me_bp.route('/accept-terms', methods=['POST'])
@require_auth
def accept_terms():
    """Record that the authenticated user has accepted the T&C and Privacy Policy."""
    data             = request.get_json(silent=True) or {}
    accepted_terms   = bool(data.get('accepted_terms'))
    accepted_privacy = bool(data.get('accepted_privacy'))
    if not accepted_terms or not accepted_privacy:
        return jsonify({'error': 'Both terms and privacy policy must be accepted'}), 400

    email = getattr(g, 'user_email', '')
    if not email:
        return jsonify({'error': 'Not authenticated'}), 401

    conn = db.get_db()
    try:
        db.record_terms_acceptance(conn, email, accepted_terms, accepted_privacy)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    finally:
        conn.close()
    return jsonify({'ok': True, 'terms_version': db.TERMS_VERSION}), 200


@me_bp.route('/token-usage', methods=['GET'])
@require_auth
def token_usage():
    """Return this user's AI token consumption for today / 7 days / 30 days."""
    conn = db.get_db()
    try:
        usage = db.get_token_usage(conn, getattr(g, 'user_email', ''))
    finally:
        conn.close()
    return jsonify(usage or {})


@me_bp.route('/quota', methods=['GET'])
@require_auth
def user_quota():
    """Return this user's current quota status and limits, plus is_admin flag."""
    conn = db.get_db()
    try:
        quota    = db.get_user_quota(conn, getattr(g, 'user_email', ''))
        au_row   = db.row(conn,
            "SELECT allow_sharing FROM allowed_users WHERE LOWER(email) = LOWER(%s)",
            (getattr(g, 'user_email', ''),),
        )
    finally:
        conn.close()
    quota['is_admin']     = is_admin(getattr(g, 'user_email', ''))
    quota['allow_sharing'] = bool(au_row.get('allow_sharing')) if au_row else False
    return jsonify(quota)


@me_bp.route('/sharing', methods=['PATCH'])
@require_auth
def update_sharing():
    """Toggle allow_sharing for the current user."""
    data  = request.get_json(silent=True) or {}
    value = data.get('allow_sharing')
    if not isinstance(value, bool):
        return jsonify({'error': 'allow_sharing must be true or false'}), 400

    email = getattr(g, 'user_email', '')
    if not email:
        return jsonify({'error': 'Not authenticated'}), 401

    conn = db.get_db()
    try:
        db.execute(conn,
            "UPDATE allowed_users SET allow_sharing = %s WHERE LOWER(email) = LOWER(%s)",
            (value, email),
        )
    finally:
        conn.close()
    return jsonify({'allow_sharing': value})
