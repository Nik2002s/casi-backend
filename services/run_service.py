"""
CASI — Run service.
Handles file ingestion (parse → validate → transform → DB persist → CASI compute).

No files are stored on disk beyond the ingest pipeline.
All ingest data is persisted to normalized PostgreSQL tables:
  testcase_runs, suite_runs, variances, test_records (legacy), runs.

Deleting an upload cascades to testcase_runs/suite_runs/variances, then
recompute builds a fresh compat_df from the remaining testcase_runs rows.

Limits enforced at upload time:
  MAX_TESTCASE_RUNS_PER_FILE    = 1 000
  MAX_TESTCASE_RUNS_PER_PROJECT = 10 000
  (file size is capped by Flask MAX_CONTENT_LENGTH = 25 MB)
"""

import os
import tempfile
from datetime import datetime, timezone

from casi_engine import run_casi_from_df
from ingest import read_sheets, validate, transform
from ingest.transformer import _extract_sprints, _build_compat_df, _apply_variances
import db

MAX_TC_RUNS_PER_FILE    = 1_000
MAX_TC_RUNS_PER_PROJECT = 10_000


# ── DataFrame reconstruction from testcase_runs ────────────────────────────────

def _build_active_map_from_db(conn, project_id: str) -> dict:
    """
    Build {tc_id → variance_id} for every variance that is currently active
    (i.e. variance_start <= now <= variance_end) for this project.
    """
    now = datetime.now()
    active_map: dict[str, str] = {}
    for v in db.get_variances(conn, project_id):
        tc_id  = v.get('test_case_id')
        var_id = v.get('variance_id')
        vs     = v.get('variance_start')
        ve     = v.get('variance_end')
        # Normalise timezone-aware datetimes so the comparison is always valid
        if vs and hasattr(vs, 'tzinfo') and vs.tzinfo is not None:
            vs = vs.replace(tzinfo=None)
        if ve and hasattr(ve, 'tzinfo') and ve.tzinfo is not None:
            ve = ve.replace(tzinfo=None)
        if tc_id and var_id and vs and ve and vs <= now <= ve:
            active_map[tc_id] = var_id
    return active_map


def _tc_runs_to_df(tc_run_rows: list[dict], active_map: dict | None = None):
    """
    Reconstruct the CASI-engine compat DataFrame from testcase_runs DB rows.
    Rows come directly from psycopg2 (timestamps already datetime objects).

    When active_map is supplied, effective_status is recomputed from
    original_status + current variances so that expired/removed variances no
    longer suppress FAIL entries.
    """
    if not tc_run_rows:
        raise ValueError('No test records found for this project.')

    # Reset every row to its original status, then re-apply current variances.
    # This corrects stale effective_status values left over from previous uploads
    # where a now-expired variance had flipped FAIL → PASS.
    for rec in tc_run_rows:
        orig = rec.get('original_status') or rec.get('effective_status', '')
        rec['effective_status']        = orig
        rec['active_variance_applied'] = False

    if active_map:
        _apply_variances(tc_run_rows, active_map)

    sprint_meta = _extract_sprints(tc_run_rows)
    return _build_compat_df(tc_run_rows, sprint_meta)


# ── Core operations ────────────────────────────────────────────────────────────

def process_upload(conn, project_id: str, file_storage, filename: str, user_id: str = None) -> dict:
    """
    Parse uploaded file in a temp location → validate → ingest → compute CASI.
    The file is deleted immediately after parsing; nothing is kept on disk.
    Returns the new run dict.
    """
    file_size_bytes = 0
    tmp_path = None
    try:
        # Write to a temp file so read_sheets (openpyxl) can open it by path
        with tempfile.NamedTemporaryFile(
            suffix='.xlsx', delete=False, prefix='casi_ingest_'
        ) as tmp:
            tmp_path = tmp.name
            file_storage.save(tmp_path)
            file_size_bytes = os.path.getsize(tmp_path)

        run = _process_upload_v2(
            conn, project_id, filename, tmp_path,
            user_id=user_id, file_size_bytes=file_size_bytes,
        )
    finally:
        # Always remove the temp file — success or failure
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return run


def _process_upload_v2(
    conn, project_id: str, filename: str, file_path: str,
    user_id: str = None, file_size_bytes: int = 0,
) -> dict:
    """
    v2 ingestion path (TEST EXECUTION + VARIANCE SHEET format).

    Steps:
      1. Read both sheets.
      2. Validate — raise ValueError with all errors if invalid.
      3. Transform — apply variance logic, aggregate suite runs, build compat_df.
      3b. Enforce row limits (per-file and per-project).
      4. Create upload record (file_path=None — file is already deleted by caller).
      5. Persist normalized entities (testcase_runs, suite_runs, variances, …).
      6. Run CASI engine on compat_df.
      7. Store run result; trim run history to last 3.
    """
    # ── 1. Read ───────────────────────────────────────────────────────────────
    df_exec, df_var = read_sheets(file_path)

    # ── 2. Validate ───────────────────────────────────────────────────────────
    validation = validate(df_exec, df_var)
    validation.raise_if_invalid()

    # ── 3. Transform ──────────────────────────────────────────────────────────
    ingest = transform(df_exec, df_var)

    if ingest.compat_df is None or ingest.compat_df.empty:
        raise ValueError('No valid test execution data found in the file.')

    # ── 3b. Row limits ────────────────────────────────────────────────────────
    file_tc_count = len(ingest.testcase_runs)
    if file_tc_count > MAX_TC_RUNS_PER_FILE:
        raise ValueError(
            f'File contains {file_tc_count:,} test case runs but the limit is '
            f'{MAX_TC_RUNS_PER_FILE:,} per file. Split the data across multiple uploads.'
        )

    project_tc_count = db.count_project_testcase_runs(conn, project_id)
    if project_tc_count + file_tc_count > MAX_TC_RUNS_PER_PROJECT:
        raise ValueError(
            f'This upload would bring the project total to '
            f'{project_tc_count + file_tc_count:,} test case runs, '
            f'exceeding the {MAX_TC_RUNS_PER_PROJECT:,} per-project limit. '
            f'Delete older uploads first.'
        )

    # ── 4. Create upload record (file_path=None — file is not kept on disk) ──
    upload = db.create_upload(
        conn, project_id, filename, None, file_tc_count,
        user_id=user_id, accepted_vars=ingest.accepted_vars,
        file_size_bytes=file_size_bytes,
    )
    upload_id = upload['id']

    # ── 5. Persist normalized entities ───────────────────────────────────────
    db.upsert_sprints(conn, project_id, ingest.sprints)
    db.upsert_test_suites(conn, project_id, ingest.test_suites)
    db.upsert_testcases(conn, project_id, ingest.testcases)
    db.insert_testcase_runs(conn, project_id, upload_id, ingest.testcase_runs)
    db.insert_suite_runs(conn, project_id, upload_id, ingest.suite_runs)
    db.insert_variances(conn, project_id, upload_id, ingest.variances)

    # ── 6. Run CASI engine on ALL project data (old + new) ─────────────────────
    # IMPORTANT: do NOT use ingest.compat_df here — it only contains the rows
    # from this single file.  Rebuilding from the DB gives us every testcase_run
    # that has ever been uploaded for this project, so upload and recompute are
    # always consistent.
    tc_run_rows   = db.get_testcase_runs(conn, project_id)
    active_map    = _build_active_map_from_db(conn, project_id)
    df_all        = _tc_runs_to_df(tc_run_rows, active_map=active_map)
    accepted_vars = db.sum_project_accepted_vars(conn, project_id)
    result = run_casi_from_df(df_all, accepted_vars=accepted_vars)
    run    = db.create_run(conn, project_id, filename, None, result, upload_id=upload_id)

    # ── 7. Trim run history to last 3 ─────────────────────────────────────────
    db.trim_old_runs(conn, project_id, keep=3)

    # ── 8. Attach rejected rows to the response so the UI can surface them ────
    if ingest.rejected_rows:
        run['rejected_rows'] = ingest.rejected_rows

    return run


def recompute_project(conn, project_id: str,
                      upload_id: str | None = None,
                      filename: str | None = None,
                      accepted_vars: int | None = None) -> dict:
    """
    Rebuild a compat DataFrame from all testcase_runs for the project,
    run CASI, and store a new run.

    accepted_vars: explicit count to use. If None, sum across all uploads
                   in the project.
    """
    tc_run_rows = db.get_testcase_runs(conn, project_id)
    active_map  = _build_active_map_from_db(conn, project_id)
    df_all      = _tc_runs_to_df(tc_run_rows, active_map=active_map)

    if accepted_vars is None:
        accepted_vars = db.sum_project_accepted_vars(conn, project_id)

    result = run_casi_from_df(df_all, accepted_vars)

    run_filename = filename or f'project_{project_id[:8]}_aggregated'
    run = db.create_run(conn, project_id, run_filename, None, result, upload_id=upload_id)

    # Trim run history to last 3 after each recompute as well
    db.trim_old_runs(conn, project_id, keep=3)

    return run


def get_run_result(conn, run_id: str) -> dict | None:
    """Fetch full run including parsed result JSONB."""
    return db.get_run(conn, run_id)


def delete_run_with_file(conn, run_id: str):
    """Delete run from DB. (Files are no longer stored on disk.)"""
    run = db.get_run(conn, run_id)
    if not run:
        return
    db.delete_run(conn, run_id)


def delete_upload_with_recompute(conn, upload_id: str, project_id: str) -> dict:
    """
    Delete an upload (cascades to testcase_runs/suite_runs/variances),
    then recompute CASI from remaining testcase_runs.

    Returns {'deleted': upload_id, 'recomputed': bool, 'new_run': run|None}
    """
    upload = db.get_upload(conn, upload_id)
    if not upload or str(upload.get('project_id')) != project_id:
        raise ValueError('Upload not found or access denied.')

    # Cascade delete: testcase_runs/suite_runs/variances are removed automatically
    db.delete_upload(conn, upload_id)

    # Recompute if any testcase_run rows remain
    has_records = db.has_project_records(conn, project_id)
    new_run = None
    if has_records:
        try:
            new_run = recompute_project(conn, project_id)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('Recompute after delete failed project=%s', project_id)

    return {
        'deleted': upload_id,
        'recomputed': has_records,
        'new_run': new_run,
    }
