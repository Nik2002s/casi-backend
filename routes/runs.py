"""
CASI — Runs blueprint.

Auth model:
  ALL routes require authentication.
  READ  routes — accessible to project owner, members with share, and public projects.
  WRITE routes — additionally require the caller to be the project owner or hold a
                 project-scoped CASI API key.
"""

import os
from flask import Blueprint, request, jsonify, g, current_app

import db
from app import limiter
from routes.auth import require_auth, require_api_key, guard_project_access, is_admin, is_valid_uuid
from services.run_service import (
    process_upload,
    delete_run_with_file,
    delete_upload_with_recompute,
    recompute_project,
)
from llm_diagnostic import get_diagnostic

bp = Blueprint('runs', __name__, url_prefix='/api/projects')


def _conn():
    return db.get_db()


# ── Runs ──────────────────────────────────────────────────────────────────────

@bp.route('/<project_id>/runs', methods=['POST'])
@require_auth
@limiter.limit('10 per hour')
def create_run(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn, write=True)
        if guard:
            return guard
    finally:
        conn.close()

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    if not f.filename.lower().endswith('.xlsx'):
        return jsonify({'error': 'Only .xlsx files are accepted'}), 400
    # Reject by MIME type too — prevents extension-spoofed uploads
    _allowed_mimes = {
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/octet-stream',   # some browsers send this for xlsx
        'application/zip',            # xlsx is a zip container
    }
    if f.mimetype and f.mimetype not in _allowed_mimes:
        return jsonify({'error': 'Only .xlsx files are accepted'}), 400

    user_id = getattr(g, 'user_id', None)

    conn = _conn()
    try:
        # ── Per-user upload limit check (0 = unlimited) ─────────────────────
        # (access already verified above — open a fresh connection here)
        if user_id and not user_id.startswith('api:'):
            max_uploads = int(
                db.get_config(conn, 'max_uploads_per_user',
                              os.environ.get('MAX_UPLOADS_PER_USER', '0'))
            )
            if max_uploads > 0:
                current_count = db.count_uploads_by_user(conn, project_id, user_id)
                if current_count >= max_uploads:
                    return jsonify({
                        'error': (
                            f'Upload limit reached ({max_uploads} files per user per project). '
                            'Contact admin to increase the limit.'
                        )
                    }), 429

        run = process_upload(conn, project_id, f, f.filename, user_id=user_id)
    except ValueError as exc:
        # User-facing validation errors (file limits, bad format) — safe to show
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        current_app.logger.exception('Upload failed for project %s', project_id)
        return jsonify({'error': 'Upload processing failed. Please try again.'}), 500
    finally:
        conn.close()

    return jsonify(run), 201


@bp.route('/<project_id>/runs', methods=['GET'])
@require_auth
def list_runs(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        runs = db.list_runs(conn, project_id)
    finally:
        conn.close()
    return jsonify(runs)


@bp.route('/<project_id>/runs/latest', methods=['GET'])
@require_auth
def latest_run(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        run = db.get_latest_run(conn, project_id)
    finally:
        conn.close()

    if not run:
        return jsonify({'error': 'No runs found'}), 404
    return jsonify(run)


@bp.route('/<project_id>/runs/<run_id>', methods=['GET'])
@require_auth
def get_run(project_id, run_id):
    if not is_valid_uuid(run_id):
        return jsonify({'error': 'Run not found'}), 404
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        run = db.get_run(conn, run_id)
    finally:
        conn.close()

    if not run or str(run.get('project_id')) != project_id:
        return jsonify({'error': 'Run not found'}), 404
    return jsonify(run)


@bp.route('/<project_id>/runs/<run_id>', methods=['DELETE'])
@require_auth
def delete_run(project_id, run_id):
    if not is_valid_uuid(run_id):
        return jsonify({'error': 'Run not found'}), 404
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn, write=True)
        if guard:
            return guard
    finally:
        conn.close()

    conn = _conn()
    try:
        run = db.get_run(conn, run_id)
        if not run or str(run.get('project_id')) != project_id:
            return jsonify({'error': 'Run not found'}), 404
        delete_run_with_file(conn, run_id)
    finally:
        conn.close()
    return jsonify({'deleted': run_id})


# ── Recompute (manual) ────────────────────────────────────────────────────────

@bp.route('/<project_id>/recompute', methods=['POST'])
@require_auth
@limiter.limit('20 per hour')
def recompute(project_id):
    """Recompute CASI from all currently stored test records for this project.
    Returns the new run."""
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn, write=True)
        if guard:
            return guard
    finally:
        conn.close()

    conn = _conn()
    try:
        if not db.has_project_records(conn, project_id):
            return jsonify({'error': 'No data to recompute. Upload a file first.'}), 400
        new_run = recompute_project(conn, project_id)
    except Exception as exc:
        current_app.logger.exception('Recompute failed for project %s', project_id)
        return jsonify({'error': 'Recompute failed. Please try again.'}), 500
    finally:
        conn.close()
    return jsonify(new_run), 201


# ── Trend ─────────────────────────────────────────────────────────────────────

@bp.route('/<project_id>/trend', methods=['GET'])
@require_auth
def trend(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        data = db.get_cross_run_trend(conn, project_id)
    finally:
        conn.close()
    return jsonify(data)


# ── Diagnostic ────────────────────────────────────────────────────────────────

@bp.route('/<project_id>/runs/<run_id>/diagnostic', methods=['POST'])
@require_auth
@limiter.limit('20 per hour')
def diagnostic(project_id, run_id):
    if not is_valid_uuid(run_id):
        return jsonify({'error': 'Run not found'}), 404
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn, write=True)
        if guard:
            return guard
    finally:
        conn.close()

    conn = _conn()
    try:
        # Quota check — same gate as the chat endpoint (admins bypass)
        user_email = getattr(g, 'user_email', '')
        if not is_admin(user_email):
            quota = db.check_ai_quota(conn, user_email)
            if not quota['allowed']:
                return jsonify({'error': 'quota_exceeded', **quota}), 429

        run = db.get_run(conn, run_id)
        if not run or str(run.get('project_id')) != project_id:
            return jsonify({'error': 'Run not found'}), 404

        result = run.get('result') or {}
        data = request.get_json(silent=True) or {}

        # Pass user context so token usage is logged against the caller
        diag = get_diagnostic(
            result,
            sprint=data.get('sprint'),
            module=data.get('module'),
            user_email=user_email,
            project_id=project_id,
            conn=conn,
        )
        db.save_diagnostic(conn, run_id, project_id, diag)
    except Exception as exc:
        current_app.logger.exception('Diagnostic failed run=%s', run_id)
        return jsonify({'error': 'Diagnostic generation failed. Please try again.'}), 500
    finally:
        conn.close()

    return jsonify(diag)


# ── Decisions ─────────────────────────────────────────────────────────────────

@bp.route('/<project_id>/runs/<run_id>/decision', methods=['POST'])
@require_auth
def save_decision(project_id, run_id):
    if not is_valid_uuid(run_id):
        return jsonify({'error': 'Run not found'}), 404
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn, write=True)
        if guard:
            return guard
    finally:
        conn.close()

    data = request.get_json(silent=True) or {}
    decision = (data.get('decision') or '').strip()
    if decision not in ('GO', 'NO-GO', 'CONDITIONAL'):
        return jsonify({'error': "decision must be GO, NO-GO, or CONDITIONAL"}), 400
    notes = (data.get('notes') or '').strip()
    if len(notes) > 2000:
        return jsonify({'error': 'Notes must be 2000 characters or fewer'}), 400

    conn = _conn()
    try:
        run = db.get_run(conn, run_id)
        if not run or str(run.get('project_id')) != project_id:
            return jsonify({'error': 'Run not found'}), 404
        # decided_by is always the authenticated user — never caller-supplied
        db.save_decision(
            conn, run_id, project_id,
            decision=decision,
            notes=notes,
            decided_by=getattr(g, 'user_email', ''),
        )
    finally:
        conn.close()
    return jsonify({'saved': True, 'decision': decision}), 201


# ── Uploads ───────────────────────────────────────────────────────────────────

@bp.route('/<project_id>/uploads', methods=['GET'])
@require_auth
def list_uploads(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        uploads = db.list_uploads(conn, project_id)
    finally:
        conn.close()
    return jsonify(uploads)


@bp.route('/<project_id>/uploads/<upload_id>', methods=['DELETE'])
@require_auth
def delete_upload(project_id, upload_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn, write=True)
        if guard:
            return guard
    finally:
        conn.close()

    conn = _conn()
    try:
        result = delete_upload_with_recompute(conn, upload_id, project_id)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 404
    except Exception as exc:
        current_app.logger.exception('Delete upload failed project=%s upload=%s', project_id, upload_id)
        return jsonify({'error': 'Delete failed. Please try again.'}), 500
    finally:
        conn.close()

    return jsonify(result)


@bp.route('/<project_id>/uploads/<upload_id>/tests', methods=['GET'])
@require_auth
def get_upload_tests(project_id, upload_id):
    """Return raw test records for a specific upload (for the Test Cases browser)."""
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        upload = db.get_upload(conn, upload_id)
        if not upload or str(upload.get('project_id')) != project_id:
            return jsonify({'error': 'Upload not found'}), 404
        records = db.get_upload_records(conn, upload_id)
        return jsonify(records)
    except Exception as exc:
        current_app.logger.exception('get_upload_records failed upload=%s', upload_id)
        return jsonify({'error': 'Failed to load test records. Please try again.'}), 500
    finally:
        conn.close()


@bp.route('/<project_id>/decisions', methods=['GET'])
@require_auth
def list_decisions(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        decisions = db.get_decisions(conn, project_id)
    finally:
        conn.close()
    return jsonify(decisions)


# ── Gate sign-offs ─────────────────────────────────────────────────────────────

@bp.route('/<project_id>/runs/<run_id>/signoffs', methods=['GET'])
@require_auth
def get_signoffs(project_id, run_id):
    if not is_valid_uuid(run_id):
        return jsonify({'error': 'Run not found'}), 404
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        signoffs = db.get_signoffs(conn, run_id)
    finally:
        conn.close()
    # Return as a list; None entries become null in JSON
    return jsonify(signoffs)


@bp.route('/<project_id>/runs/<run_id>/signoffs', methods=['POST'])
@require_auth
@limiter.limit('60 per hour')
def assign_signoff(project_id, run_id):
    """Assign a sign-off role to a user (project write access required)."""
    if not is_valid_uuid(run_id):
        return jsonify({'error': 'Run not found'}), 404

    data  = request.get_json(silent=True) or {}
    role  = (data.get('role')  or '').strip()
    email = (data.get('email') or '').strip().lower()

    if role not in db.VALID_SIGNOFF_ROLES:
        return jsonify({'error': f'role must be one of: {", ".join(db.VALID_SIGNOFF_ROLES)}'}), 400
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email is required'}), 400

    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn, write=True)
        if guard:
            return guard

        # Verify run belongs to this project
        run = db.get_run(conn, run_id)
        if not run or str(run.get('project_id')) != project_id:
            return jsonify({'error': 'Run not found'}), 404

        # Verify the target email exists in allowed_users
        target = db.row(conn, """
            SELECT email, COALESCE(display_name, email) AS display_name
            FROM allowed_users WHERE LOWER(email) = LOWER(%s)
        """, (email,))
        if not target:
            return jsonify({'error': 'No CASI account found for that email address'}), 404

        result = db.upsert_signoff(
            conn, run_id, project_id,
            role=role,
            assigned_email=target['email'],
            assigned_name=target['display_name'],
            assigned_by=getattr(g, 'user_email', ''),
            is_admin=is_admin(getattr(g, 'user_email', '')),
        )
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception:
        current_app.logger.exception('assign_signoff failed project=%s run=%s', project_id, run_id)
        return jsonify({'error': 'Could not assign sign-off. Please try again.'}), 500
    finally:
        conn.close()

    return jsonify(result), 201


@bp.route('/<project_id>/runs/<run_id>/signoffs/<role>/verdict', methods=['POST'])
@require_auth
@limiter.limit('20 per hour')
def submit_signoff_verdict(project_id, run_id, role):
    """The assigned user submits their approve/reject verdict (immutable once set)."""
    if not is_valid_uuid(run_id):
        return jsonify({'error': 'Run not found'}), 404
    if role not in db.VALID_SIGNOFF_ROLES:
        return jsonify({'error': f'role must be one of: {", ".join(db.VALID_SIGNOFF_ROLES)}'}), 400

    data          = request.get_json(silent=True) or {}
    verdict       = (data.get('verdict')       or '').strip()
    verdict_notes = (data.get('verdict_notes') or '').strip()[:2000]

    if verdict not in ('approved', 'rejected'):
        return jsonify({'error': "verdict must be 'approved' or 'rejected'"}), 400

    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard

        run = db.get_run(conn, run_id)
        if not run or str(run.get('project_id')) != project_id:
            return jsonify({'error': 'Run not found'}), 404

        updated = db.save_signoff_verdict(
            conn, run_id, role,
            actor_email=getattr(g, 'user_email', ''),
            verdict=verdict,
            verdict_notes=verdict_notes,
        )
        if not updated:
            # Could be: not the assigned user, or verdict already recorded.
            # Check which case to give a precise error.
            existing = db.row(conn,
                "SELECT verdict, LOWER(assigned_email) AS ae FROM gate_signoffs WHERE run_id = %s AND role = %s",
                (run_id, role)
            )
            if existing and existing.get('verdict') is not None:
                return jsonify({'error': 'A verdict has already been recorded for this role and cannot be changed.'}), 409
            return jsonify({'error': 'You are not assigned to this sign-off role'}), 403
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception:
        current_app.logger.exception('submit_signoff_verdict failed project=%s run=%s', project_id, run_id)
        return jsonify({'error': 'Could not save verdict. Please try again.'}), 500
    finally:
        conn.close()

    return jsonify(updated), 200
