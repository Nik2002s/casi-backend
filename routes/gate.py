"""
CASI — CI/CD Quality Gate blueprint.

POST /api/projects/<id>/gate

Called by GitHub Actions / Jenkins / GitLab CI at the end of a test run.
The caller uploads an Excel file (or references an existing run) and gets
back a machine-readable pass/fail verdict.

Request (multipart OR JSON):
  - file         : xlsx upload (optional if run_id given)
  - run_id       : UUID of an existing run (optional if file given)
  - threshold    : minimum CASI score to pass (default 700)
  - use_asi      : bool — use ASI instead of CASI score (default false)
  - fail_on_gate : "Red" | "Yellow" — fail if gate is this colour or worse
                   overrides threshold when set

Response 200:
  {
    "pass": true | false,
    "score": 712,
    "gate": "Green",
    "threshold": 700,
    "run_id": "...",
    "details": { ...scores, sprint_count, tc_count }
  }

The HTTP status is always 200 so CI tools read the JSON body.
Use "pass": false to trigger a pipeline failure in your CI script:
  if [ "$(curl ... | jq '.pass')" = "false" ]; then exit 1; fi
"""

from flask import Blueprint, request, jsonify, g, current_app

import db
from routes.auth import require_api_key
from services.run_service import process_upload

bp = Blueprint('gate', __name__, url_prefix='/api/projects')

GATE_RANK = {'Green': 2, 'Yellow': 1, 'Red': 0}


def _gate_passes(score, gate, threshold, use_asi, fail_on_gate):
    """
    Determine pass/fail.
    Priority: fail_on_gate (colour-based) > threshold (numeric).
    """
    if fail_on_gate:
        fail_on_gate = fail_on_gate.capitalize()
        if fail_on_gate not in GATE_RANK:
            fail_on_gate = 'Red'
        return GATE_RANK.get(gate, 0) > GATE_RANK.get(fail_on_gate, 0)
    return score >= threshold


@bp.route('/<project_id>/gate', methods=['POST'])
@require_api_key
def quality_gate(project_id):
    if g.project_id != project_id:
        return jsonify({'error': 'API key does not belong to this project'}), 403

    # Parse params from form data or JSON
    if request.content_type and 'multipart' in request.content_type:
        params = request.form
        file_obj = request.files.get('file')
    else:
        params = request.get_json(silent=True) or {}
        file_obj = None

    run_id     = params.get('run_id')
    threshold  = int(params.get('threshold', 700))
    use_asi    = str(params.get('use_asi', 'false')).lower() == 'true'
    fail_on_gate = params.get('fail_on_gate')  # optional colour override

    conn = db.get_db()
    try:
        # ── Resolve run ───────────────────────────────────────────────────────
        if file_obj and file_obj.filename.lower().endswith('.xlsx'):
            # Upload + compute on the fly
            try:
                run = process_upload(conn, project_id, file_obj, file_obj.filename)
            except ValueError as exc:
                # User-facing validation errors are safe to surface
                return jsonify({'error': str(exc)}), 400
            except Exception:
                current_app.logger.exception('Gate upload failed project=%s', project_id)
                return jsonify({'error': 'File processing failed. Please try again.'}), 500

        elif run_id:
            run = db.get_run(conn, run_id)
            if not run or str(run.get('project_id')) != project_id:
                return jsonify({'error': 'Run not found'}), 404

        else:
            # Default: use latest run
            run = db.get_latest_run(conn, project_id)
            if not run:
                return jsonify({
                    'pass': False,
                    'score': 0,
                    'gate': 'Red',
                    'threshold': threshold,
                    'run_id': None,
                    'error': 'No runs found — upload a test report first',
                })

        result  = run.get('result') or {}
        scores  = result.get('scores', {})
        dataset = result.get('dataset', {})
        history = result.get('sprint_history', [])

        score = scores.get('asi_score' if use_asi else 'casi_score', 0)
        gate  = scores.get('asi_gate'  if use_asi else 'casi_gate',  'Red')

        passed = _gate_passes(score, gate, threshold, use_asi, fail_on_gate)

        return jsonify({
            'pass':      passed,
            'score':     round(score, 2),
            'gate':      gate,
            'threshold': threshold,
            'run_id':    str(run['id']),
            'metric':    'ASI' if use_asi else 'CASI',
            'details': {
                'casi_score':   scores.get('casi_score'),
                'asi_score':    scores.get('asi_score'),
                'casi_gate':    scores.get('casi_gate'),
                'asi_gate':     scores.get('asi_gate'),
                'tc_count':     dataset.get('tc_count'),
                'sprint_count': dataset.get('sprint_count'),
                'sprints':      len(history),
                'n_fail':       (history[-1].get('n_fail', 0) if history else 0),
            },
        })

    finally:
        conn.close()
