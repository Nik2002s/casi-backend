"""
CASI — Test Execution blueprint.

Routes:
  GET /api/projects/<id>/test-execution/suite-runs
  GET /api/projects/<id>/test-execution/testcase-runs

Both endpoints support filtering, sorting, and pagination via query params.
"""

from flask import Blueprint, request, jsonify
from psycopg2.extras import RealDictCursor
import db
from routes.auth import require_auth, guard_project_access

bp = Blueprint('test_execution', __name__, url_prefix='/api/projects/<project_id>/test-execution')


# ── helpers ────────────────────────────────────────────────────────────────────

def _conn():
    return db.get_db()


def _int(val, default):
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return default


def _list_param(val):
    """Accept comma-separated string → list of non-empty strings."""
    if not val:
        return []
    return [v.strip() for v in val.split(',') if v.strip()]


# ── Suite runs ─────────────────────────────────────────────────────────────────

# Allowed sort columns for suite runs (maps API name → SQL expression)
_SUITE_SORT_COLS = {
    'startTimestamp':  'sr.start_timestamp',
    'endTimestamp':    'sr.end_timestamp',
    'duration':        'sr.duration_seconds',
    'status':          'sr.status',
    'suiteRunId':      'sr.suite_run_id',
    'suiteId':         'sr.suite_id',
    'suiteName':       'sr.suite_name',
    'sprint':          'sr.sprint_name',
    'totalTestcases':  'total_testcases',
    'failedTestcases': 'failed_testcases',
}


def _suite_runs_query(project_id, filters, sort_col, sort_dir, page, page_size):
    """
    Build and execute the suite runs query with counts from testcase_runs.
    Returns (rows, total_count).
    """
    params_where = []
    where_clauses = ['sr.project_id = %s']
    params_where.append(project_id)

    # Multi-value exact-match filters (dropdown selections — comma-separated)
    for api_key, col in [
        ('sprint',    'sr.sprint_name'),
        ('suiteName', 'sr.suite_name'),
        ('status',    'sr.status'),
    ]:
        vals = _list_param(filters.get(api_key, ''))
        if vals:
            where_clauses.append(f"{col} = ANY(%s)")
            params_where.append(vals)

    # Free-text partial-match filters
    for api_key, col in [
        ('suiteId',    'sr.suite_id'),
        ('suiteRunId', 'sr.suite_run_id'),
    ]:
        val = filters.get(api_key, '').strip()
        if val:
            where_clauses.append(f"{col} ILIKE %s")
            params_where.append(f'%{val}%')

    # Date range filters
    for api_key, col in [
        ('startDateFrom', 'sr.start_timestamp'),
        ('startDateTo',   'sr.start_timestamp'),
        ('endDateFrom',   'sr.end_timestamp'),
        ('endDateTo',     'sr.end_timestamp'),
    ]:
        val = filters.get(api_key, '').strip()
        if val:
            op = '>=' if api_key.endswith('From') else '<='
            where_clauses.append(f"{col} {op} %s")
            params_where.append(val)

    where_sql = 'WHERE ' + ' AND '.join(where_clauses)

    # Count query (no pagination)
    count_sql = f"""
        SELECT COUNT(DISTINCT sr.id) AS n
        FROM suite_runs sr
        {where_sql}
    """

    # Main query with testcase counts
    safe_sort = _SUITE_SORT_COLS.get(sort_col, 'sr.start_timestamp')
    safe_dir  = 'ASC' if sort_dir.upper() == 'ASC' else 'DESC'
    offset    = (page - 1) * page_size

    data_sql = f"""
        SELECT
            sr.id,
            sr.suite_run_id,
            sr.suite_id,
            sr.suite_name,
            sr.sprint_name           AS sprint,
            sr.status                AS suite_run_status,
            sr.start_timestamp,
            sr.end_timestamp,
            sr.duration_seconds,
            COUNT(tr.id)                                                              AS total_testcases,
            COUNT(tr.id) FILTER (WHERE tr.effective_status = 'PASS')                AS passed_testcases,
            COUNT(tr.id) FILTER (WHERE tr.effective_status = 'FAIL')                AS failed_testcases,
            COUNT(tr.id) FILTER (WHERE tr.effective_status = 'ERROR')               AS error_testcases,
            COUNT(tr.id) FILTER (WHERE tr.effective_status = 'EXECUTING')           AS executing_testcases,
            COUNT(tr.id) FILTER (WHERE tr.effective_status = 'BLOCKED')             AS blocked_testcases
        FROM suite_runs sr
        LEFT JOIN testcase_runs tr
               ON tr.suite_run_id = sr.suite_run_id
              AND tr.project_id   = sr.project_id
        {where_sql}
        GROUP BY sr.id, sr.suite_run_id, sr.suite_id, sr.suite_name,
                 sr.sprint_name, sr.status, sr.start_timestamp, sr.end_timestamp,
                 sr.duration_seconds
        ORDER BY {safe_sort} {safe_dir} NULLS LAST
        LIMIT %s OFFSET %s
    """

    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(count_sql, params_where)
            total = cur.fetchone()['n']

            cur.execute(data_sql, params_where + [page_size, offset])
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # Serialize datetime / Decimal fields
    for row in rows:
        for k, v in row.items():
            if hasattr(v, 'isoformat'):
                row[k] = v.isoformat()
            elif v is not None and not isinstance(v, (str, int, float, bool)):
                row[k] = str(v)

    return rows, total


@bp.route('/suite-runs', methods=['GET'])
@require_auth
def get_suite_runs(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
    finally:
        conn.close()

    q = request.args
    filters = {
        'sprint':        q.get('sprint', ''),
        'suiteId':       q.get('suiteId', ''),
        'suiteName':     q.get('suiteName', ''),
        'suiteRunId':    q.get('suiteRunId', ''),
        'status':        q.get('status', ''),
        'startDateFrom': q.get('startDateFrom', ''),
        'startDateTo':   q.get('startDateTo', ''),
        'endDateFrom':   q.get('endDateFrom', ''),
        'endDateTo':     q.get('endDateTo', ''),
    }
    sort_col  = q.get('sortBy', 'startTimestamp')
    sort_dir  = q.get('sortDirection', 'desc')
    page      = _int(q.get('page'), 1)
    page_size = min(_int(q.get('pageSize'), 25), 100)

    rows, total = _suite_runs_query(project_id, filters, sort_col, sort_dir, page, page_size)

    return jsonify({
        'data':     rows,
        'total':    total,
        'page':     page,
        'pageSize': page_size,
        'pages':    max(1, -(-total // page_size)),   # ceil division
    })


# ── Testcase runs ──────────────────────────────────────────────────────────────

_TC_SORT_COLS = {
    'startTimestamp':        'tr.start_timestamp',
    'endTimestamp':          'tr.end_timestamp',
    'duration':              'tr.duration_seconds',
    'originalStatus':        'tr.original_status',
    'effectiveStatus':       'tr.effective_status',
    'tcRunId':               'tr.tc_run_id',
    'suiteRunId':            'tr.suite_run_id',
    'tcId':                  'tr.tc_id',
    'tcName':                'tc.tc_name',
    'suiteId':               'tr.suite_id',
    'suiteName':             'tr.suite_name',
    'sprint':                'tr.sprint_name',
    'executedBy':            'tr.executed_by',
    'activeVarianceApplied': 'tr.active_variance_applied',
}


def _tc_runs_query(project_id, filters, sort_col, sort_dir, page, page_size):
    params_where = []
    where_clauses = ['tr.project_id = %s']
    params_where.append(project_id)

    # Multi-value exact-match filters (dropdown selections — comma-separated)
    for api_key, col in [
        ('sprint',          'tr.sprint_name'),
        ('suiteName',       'tr.suite_name'),
        ('executedBy',      'tr.executed_by'),
        ('originalStatus',  'tr.original_status'),
        ('effectiveStatus', 'tr.effective_status'),
    ]:
        vals = _list_param(filters.get(api_key, ''))
        if vals:
            where_clauses.append(f"{col} = ANY(%s)")
            params_where.append(vals)

    # Free-text partial-match filters
    for api_key, col in [
        ('suiteId',   'tr.suite_id'),
        ('suiteRunId','tr.suite_run_id'),
        ('tcId',      'tr.tc_id'),
        ('tcRunId',   'tr.tc_run_id'),
        ('tcName',    'tc.tc_name'),
        ('varianceId','tr.variance_id'),
    ]:
        val = filters.get(api_key, '').strip()
        if val:
            where_clauses.append(f"{col} ILIKE %s")
            params_where.append(f'%{val}%')

    # Boolean: activeVarianceApplied  ('' | 'true' | 'false')
    ava = filters.get('activeVarianceApplied', '').strip().lower()
    if ava == 'true':
        where_clauses.append("tr.active_variance_applied = TRUE")
    elif ava == 'false':
        where_clauses.append("tr.active_variance_applied = FALSE")

    # Date ranges
    for api_key, col in [
        ('startDateFrom', 'tr.start_timestamp'),
        ('startDateTo',   'tr.start_timestamp'),
        ('endDateFrom',   'tr.end_timestamp'),
        ('endDateTo',     'tr.end_timestamp'),
    ]:
        val = filters.get(api_key, '').strip()
        if val:
            op = '>=' if api_key.endswith('From') else '<='
            where_clauses.append(f"{col} {op} %s")
            params_where.append(val)

    where_sql = 'WHERE ' + ' AND '.join(where_clauses)

    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM testcase_runs tr
        LEFT JOIN testcases tc ON tc.tc_id = tr.tc_id AND tc.project_id = tr.project_id
        {where_sql}
    """

    safe_sort = _TC_SORT_COLS.get(sort_col, 'tr.start_timestamp')
    safe_dir  = 'ASC' if sort_dir.upper() == 'ASC' else 'DESC'
    offset    = (page - 1) * page_size

    data_sql = f"""
        SELECT
            tr.id,
            tr.tc_run_id,
            tr.suite_run_id,
            tr.tc_id,
            tc.tc_name,
            tr.suite_id,
            tr.suite_name,
            tr.sprint_name              AS sprint,
            tr.original_status,
            tr.effective_status,
            tr.executed_by,
            tr.start_timestamp,
            tr.end_timestamp,
            tr.duration_seconds,
            tr.active_variance_applied,
            tr.variance_id
        FROM testcase_runs tr
        LEFT JOIN testcases tc
               ON tc.tc_id       = tr.tc_id
              AND tc.project_id  = tr.project_id
        {where_sql}
        ORDER BY {safe_sort} {safe_dir} NULLS LAST
        LIMIT %s OFFSET %s
    """

    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(count_sql, params_where)
            total = cur.fetchone()['n']

            cur.execute(data_sql, params_where + [page_size, offset])
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    for row in rows:
        for k, v in row.items():
            if hasattr(v, 'isoformat'):
                row[k] = v.isoformat()
            elif v is not None and not isinstance(v, (str, int, float, bool)):
                row[k] = str(v)

    return rows, total


@bp.route('/testcase-runs', methods=['GET'])
@require_auth
def get_testcase_runs(project_id):
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
    finally:
        conn.close()

    q = request.args
    filters = {
        'sprint':                q.get('sprint', ''),
        'suiteId':               q.get('suiteId', ''),
        'suiteName':             q.get('suiteName', ''),
        'suiteRunId':            q.get('suiteRunId', ''),
        'tcId':                  q.get('tcId', ''),
        'tcRunId':               q.get('tcRunId', ''),
        'tcName':                q.get('tcName', ''),
        'originalStatus':        q.get('originalStatus', ''),
        'effectiveStatus':       q.get('effectiveStatus', ''),
        'executedBy':            q.get('executedBy', ''),
        'activeVarianceApplied': q.get('activeVarianceApplied', ''),
        'varianceId':            q.get('varianceId', ''),
        'startDateFrom':         q.get('startDateFrom', ''),
        'startDateTo':           q.get('startDateTo', ''),
        'endDateFrom':           q.get('endDateFrom', ''),
        'endDateTo':             q.get('endDateTo', ''),
    }
    sort_col  = q.get('sortBy', 'startTimestamp')
    sort_dir  = q.get('sortDirection', 'desc')
    page      = _int(q.get('page'), 1)
    page_size = min(_int(q.get('pageSize'), 25), 100)

    rows, total = _tc_runs_query(project_id, filters, sort_col, sort_dir, page, page_size)

    return jsonify({
        'data':     rows,
        'total':    total,
        'page':     page,
        'pageSize': page_size,
        'pages':    max(1, -(-total // page_size)),
    })


# ── Variance detail ───────────────────────────────────────────────────────────

@bp.route('/variances/<variance_id>', methods=['GET'])
@require_auth
def get_variance(project_id, variance_id):
    """Return details for a single variance ID in this project."""
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT v.*,
                          COUNT(tr.id) AS covered_runs
                   FROM variances v
                   LEFT JOIN testcase_runs tr
                          ON tr.variance_id   = v.variance_id
                         AND tr.project_id    = v.project_id
                         AND tr.active_variance_applied = TRUE
                   WHERE v.project_id = %s AND v.variance_id = %s
                   GROUP BY v.id
                   LIMIT 1""",
                (project_id, variance_id),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({'error': 'Variance not found'}), 404

    result = dict(row)
    for k, v in result.items():
        if hasattr(v, 'isoformat'):
            result[k] = v.isoformat()
        elif v is not None and not isinstance(v, (str, int, float, bool)):
            result[k] = str(v)
    return jsonify(result)


# ── Distinct filter options (sprints, suite names) ─────────────────────────────

@bp.route('/filter-options', methods=['GET'])
@require_auth
def get_filter_options(project_id):
    """Return distinct sprint names and suite names for this project's dropdown filters."""
    conn = _conn()
    try:
        guard = guard_project_access(project_id, conn)
        if guard:
            return guard
        sprints = db.rows(conn,
            "SELECT DISTINCT sprint_name FROM suite_runs WHERE project_id=%s AND sprint_name IS NOT NULL ORDER BY sprint_name",
            (project_id,)
        )
        suite_names = db.rows(conn,
            "SELECT DISTINCT suite_name FROM suite_runs WHERE project_id=%s AND suite_name IS NOT NULL ORDER BY suite_name",
            (project_id,)
        )
        executed_by = db.rows(conn,
            "SELECT DISTINCT executed_by FROM testcase_runs WHERE project_id=%s AND executed_by IS NOT NULL ORDER BY executed_by",
            (project_id,)
        )
    finally:
        conn.close()

    return jsonify({
        'sprints':    [r['sprint_name'] for r in sprints],
        'suiteNames': [r['suite_name']  for r in suite_names],
        'executedBy': [r['executed_by'] for r in executed_by],
    })
