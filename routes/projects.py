"""
CASI — Projects CRUD blueprint.

Routes:
  POST   /api/projects              create project (returns raw API key once)
  GET    /api/projects              list projects visible to the current user
  GET    /api/projects/<id>         get one project
  PATCH  /api/projects/<id>         update name/description (owner only)
  DELETE /api/projects/<id>         delete project + cascade (owner only)

  POST   /api/projects/<id>/share           share with another user by email
  GET    /api/projects/<id>/shares          list shares
  DELETE /api/projects/<id>/shares/<email>  remove a share

  POST   /api/projects/<id>/keys    generate new API key
  DELETE /api/projects/<id>/keys    revoke all API keys
  GET    /api/projects/<id>/keys    list key metadata (never raw)
"""

from flask import Blueprint, request, jsonify, g
import db
from routes.auth import require_auth, is_admin, guard_project_access

bp = Blueprint('projects', __name__, url_prefix='/api/projects')


def _conn():
    return db.get_db()


# ── Project CRUD ──────────────────────────────────────────────────────────────

@bp.route('', methods=['POST'])
@require_auth
def create_project():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    if len(name) > 120:
        return jsonify({'error': 'Project name must be 120 characters or fewer'}), 400
    description = (data.get('description') or '').strip()
    if len(description) > 2000:
        return jsonify({'error': 'Description must be 2000 characters or fewer'}), 400

    conn = _conn()
    try:
        # ── Non-admin: max 3 projects ─────────────────────────────────────────
        if g.user_email and not is_admin(g.user_email):
            existing = db.count_projects_by_owner(conn, g.user_email)
            if existing >= 3:
                return jsonify({
                    'error': 'Project limit reached. Non-admin users can create a maximum of 3 projects.'
                }), 403

        project = db.create_project(
            conn, name,
            description=description,
            created_by=g.user_email or None,
        )
    finally:
        conn.close()

    return jsonify(project), 201


@bp.route('', methods=['GET'])
@require_auth
def list_projects():
    conn = _conn()
    try:
        projects = db.list_projects(conn, user_email=g.user_email or None)
    finally:
        conn.close()
    return jsonify(projects)


@bp.route('/<project_id>', methods=['GET'])
@require_auth
def get_project(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        project = db.get_project(conn, project_id)
    finally:
        conn.close()

    if not project:
        return jsonify({'error': 'Project not found'}), 404
    return jsonify(project)


@bp.route('/<project_id>', methods=['PATCH'])
@require_auth
def update_project(project_id):
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    if name is not None and len(str(name).strip()) > 120:
        return jsonify({'error': 'Project name must be 120 characters or fewer'}), 400
    description = data.get('description')
    if description is not None and len(str(description)) > 2000:
        return jsonify({'error': 'Description must be 2000 characters or fewer'}), 400
    conn = _conn()
    try:
        project = db.get_project(conn, project_id)
        if not project:
            return jsonify({'error': 'Project not found'}), 404

        # ── Non-admin: max 2 public projects ─────────────────────────────────
        making_public = data.get('is_public')
        if making_public is True and g.user_email and not is_admin(g.user_email):
            already_public = project.get('is_public', False)
            if not already_public:
                pub_count = db.count_public_projects_by_owner(conn, g.user_email)
                if pub_count >= 2:
                    return jsonify({
                        'error': 'Public project limit reached. Non-admin users can make a maximum of 2 projects public.'
                    }), 403

        db.update_project(
            conn, project_id,
            name=data.get('name'),
            description=data.get('description'),
            is_public=data.get('is_public'),
        )
        project = db.get_project(conn, project_id)
    finally:
        conn.close()
    return jsonify(project)


@bp.route('/<project_id>', methods=['DELETE'])
@require_auth
def delete_project(project_id):
    conn = _conn()
    try:
        project = db.get_project(conn, project_id)
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        if not db.is_project_owner(conn, project_id, g.user_email):
            return jsonify({'error': 'Only the project owner can delete it'}), 403
        db.delete_project(conn, project_id)
    finally:
        conn.close()
    return jsonify({'deleted': project_id})


# ── Project sharing ───────────────────────────────────────────────────────────

@bp.route('/<project_id>/share', methods=['POST'])
@require_auth
def share_project(project_id):
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email is required'}), 400
    if len(email) > 254:
        return jsonify({'error': 'Email must be 254 characters or fewer'}), 400

    conn = _conn()
    try:
        project = db.get_project(conn, project_id)
        if not project:
            return jsonify({'error': 'Project not found'}), 404
        if not db.is_project_owner(conn, project_id, g.user_email):
            return jsonify({'error': 'Only the project owner can share it'}), 403
        if email == (g.user_email or '').lower():
            return jsonify({'error': 'You already own this project'}), 400

        # ── Non-admin recipient: max 10 shared projects (public excluded) ─────
        if not is_admin(email):
            shared_count = db.count_projects_shared_with(conn, email)
            if shared_count >= 10:
                return jsonify({
                    'error': f'{email} has reached the shared-project limit (max 10). '
                              'Ask them to remove a project first.'
                }), 403

        db.share_project(conn, project_id, email, shared_by=g.user_email)
        shares = db.get_project_shares(conn, project_id)
    finally:
        conn.close()

    return jsonify({'shared': True, 'shares': shares}), 201


@bp.route('/<project_id>/shares', methods=['GET'])
@require_auth
def list_shares(project_id):
    conn = _conn()
    try:
        if not db.get_project(conn, project_id):
            return jsonify({'error': 'Project not found'}), 404
        shares = db.get_project_shares(conn, project_id)
    finally:
        conn.close()
    return jsonify(shares)


@bp.route('/<project_id>/shares/<path:email>', methods=['DELETE'])
@require_auth
def remove_share(project_id, email):
    conn = _conn()
    try:
        if not db.is_project_owner(conn, project_id, g.user_email):
            return jsonify({'error': 'Only the project owner can remove shares'}), 403
        db.remove_project_share(conn, project_id, email)
    finally:
        conn.close()
    return jsonify({'removed': email})


# ── API Key management ────────────────────────────────────────────────────────

@bp.route('/<project_id>/keys', methods=['GET'])
@require_auth
def list_keys(project_id):
    conn = _conn()
    try:
        keys = db.list_api_keys(conn, project_id)
    finally:
        conn.close()
    return jsonify(keys)


@bp.route('/<project_id>/keys', methods=['POST'])
@require_auth
def create_key(project_id):
    conn = _conn()
    try:
        if not db.get_project(conn, project_id):
            return jsonify({'error': 'Project not found'}), 404
        raw, prefix = db.create_api_key(conn, project_id)
    finally:
        conn.close()
    # Return raw key once — caller must store it
    return jsonify({'key': raw, 'prefix': prefix}), 201


@bp.route('/<project_id>/keys', methods=['DELETE'])
@require_auth
def revoke_keys(project_id):
    conn = _conn()
    try:
        db.revoke_api_keys(conn, project_id)
    finally:
        conn.close()
    return jsonify({'revoked': True})
