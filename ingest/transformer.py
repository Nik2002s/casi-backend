"""
CASI Ingest — Transformation layer.

Takes validated raw DataFrames and produces:
  1. Normalized entity dicts for DB persistence.
  2. A CASI-engine-compatible DataFrame (compat_df) so the existing
     scoring logic runs with zero changes.

CASI engine compatibility mapping:
  TC_ID       → first column (engine uses iloc[:,0] and matches 'TC-' prefix)
  EFFECTIVE_STATUS → Status column  (engine's FAIL_STATES = {'FAIL','ERR'})
                     ERROR is remapped to ERR for the engine
  SUITE_NAME  → Sheet column
  sprints     → [(sprint_start_date, sprint_end_date)]  per TC per sprint
                sprint dates are derived from min/max timestamps in that sprint group
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Any

import pandas as pd
import numpy as np

# ── Status constants ───────────────────────────────────────────────────────────

FAIL_STATUS   = 'FAIL'
PASS_STATUS   = 'PASS'
ERROR_STATUS  = 'ERROR'

# What the CASI engine expects for "error" failures
_ENGINE_ERROR = 'ERR'

# Statuses that count as "failing" in suite-run status logic
_SUITE_FAIL_PRIORITY = ['FAIL', 'EXECUTING', 'ERROR', 'BLOCKED']


# ── Result dataclass ───────────────────────────────────────────────────────────

@dataclass
class IngestResult:
    sprints:       list[dict] = field(default_factory=list)
    test_suites:   list[dict] = field(default_factory=list)
    testcases:     list[dict] = field(default_factory=list)
    testcase_runs: list[dict] = field(default_factory=list)
    suite_runs:    list[dict] = field(default_factory=list)
    variances:     list[dict] = field(default_factory=list)
    compat_df:     Any        = None   # pd.DataFrame
    accepted_vars: int        = 0      # FAIL→PASS conversions (for CASI F component)
    rejected_rows: list[dict] = field(default_factory=list)  # [{row, reason}] for rows skipped during ingest


# ── Public entry point ─────────────────────────────────────────────────────────

def transform(
    df_exec: pd.DataFrame,
    df_var:  pd.DataFrame,
    reference_dt: datetime | None = None,
) -> IngestResult:
    """
    Full transformation pipeline.

    Args:
        df_exec:       Raw TEST EXECUTION DataFrame from reader.
        df_var:        Raw VARIANCE SHEET DataFrame from reader.
        reference_dt:  Datetime used for active-variance evaluation.
                       Defaults to now (UTC). Pass a fixed value in tests.

    Returns:
        IngestResult with all normalized entities and the compat DataFrame.
    """
    result = IngestResult()

    # ── Step 1: normalize execution sheet ─────────────────────────────────────
    exec_rows, result.rejected_rows = _normalize_execution(df_exec)

    if not exec_rows:
        # All rows were rejected — surface a clear error so the user knows
        reasons_preview = '; '.join(
            f'row {r["row"]}: {r["reasons"][0]}'
            for r in result.rejected_rows[:3]
        )
        raise ValueError(
            f'No valid rows could be ingested — all {len(result.rejected_rows)} '
            f'row(s) were rejected. First issues: {reasons_preview}'
        )

    if reference_dt is None:
        # Use the latest end_timestamp in the execution data so that variances
        # are evaluated relative to when the tests actually ran, not wall-clock
        # time (which would make all historical variances appear expired).
        ts_values = [r['end_timestamp'] for r in exec_rows if r.get('end_timestamp')]
        reference_dt = max(ts_values) if ts_values else datetime.now(timezone.utc).replace(tzinfo=None)

    # ── Step 2: parse variances ────────────────────────────────────────────────
    var_rows, active_variance_map = _parse_variances(df_var, reference_dt)
    result.variances = var_rows

    # ── Step 3: apply variance logic → effective_status ───────────────────────
    exec_rows, accepted_count = _apply_variances(exec_rows, active_variance_map)
    result.accepted_vars = accepted_count

    # ── Step 4: extract dimension entities ────────────────────────────────────
    result.sprints     = _extract_sprints(exec_rows)
    result.test_suites = _extract_test_suites(exec_rows)
    result.testcases   = _extract_testcases(exec_rows)

    # ── Step 5: build testcase_runs ───────────────────────────────────────────
    result.testcase_runs = exec_rows

    # ── Step 6: aggregate suite runs ──────────────────────────────────────────
    result.suite_runs = _aggregate_suite_runs(exec_rows)

    # ── Step 7: build CASI-engine compat DataFrame ────────────────────────────
    result.compat_df = _build_compat_df(exec_rows, result.sprints)

    return result


# ── Step 1: normalize execution rows ──────────────────────────────────────────

# Statuses the engine recognises as valid
_VALID_STATUSES = {'PASS', 'FAIL', 'ERROR', 'ERR', 'BLOCKED', 'EXECUTING', 'SKIP', 'SKIPPED', 'N/A', 'NOT RUN'}

# Required columns and their human-readable label for error messages
_REQUIRED_FIELDS = [
    ('TC_RUN_ID',    'TC_RUN_ID'),
    ('TC_ID',        'TC_ID'),
    ('SUITE_RUN_ID', 'SUITE_RUN_ID'),
    ('SUITE_ID',     'SUITE_ID'),
    ('SUITE_NAME',   'SUITE_NAME'),
    ('SPRINT',       'SPRINT'),
    ('STATUS',       'STATUS'),
]


def _parse_ts(val: Any) -> datetime | None:
    """Parse a timestamp value to datetime.  Returns None if blank or unrecognised."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() in ('nan', 'none', 'nat'):
        return None

    fmts = [
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%d/%m/%Y',
        '%m/%d/%Y %H:%M:%S',
        '%m/%d/%Y %H:%M',
        '%m/%d/%Y',
        '%m/%d/%y %H:%M:%S',
        '%m/%d/%y %H:%M',
        '%m/%d/%y',
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None   # unrecognised — caller decides whether to reject the row


def _str_or_none(val: Any) -> str | None:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    s = str(val).strip()
    return s if s and s.lower() not in ('nan', 'none') else None


def _normalize_execution(df: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """
    Parse and normalize every row in the TEST EXECUTION sheet.

    Any row with a missing required field, an unrecognised STATUS value, or an
    unparseable timestamp is rejected — it is NOT silently ingested with assumed
    values.  Rejection reasons are collected per-row and returned so the caller
    can surface them to the user.

    Returns:
        (valid_rows, rejected_rows)
        valid_rows:    rows that passed all checks, ready for downstream steps
        rejected_rows: list of {row, tc_run_id, tc_id, reasons: [str]}
    """
    valid: list[dict]    = []
    rejected: list[dict] = []

    for row_idx, row in df.iterrows():
        row_num = int(row_idx) + 2  # +1 for 0-based, +1 for header row
        reasons: list[str] = []

        # ── Required field presence ───────────────────────────────────────────
        for col, label in _REQUIRED_FIELDS:
            v = row.get(col)
            if v is None or str(v).strip() in ('', 'nan', 'NaN', 'None'):
                reasons.append(f'{label} is missing or empty')

        # ── STATUS value ──────────────────────────────────────────────────────
        status_raw = str(row.get('STATUS', '')).strip().upper()
        if status_raw and status_raw not in _VALID_STATUSES:
            reasons.append(
                f'STATUS "{status_raw}" is not recognised — '
                f'must be one of: {", ".join(sorted(_VALID_STATUSES))}'
            )

        # ── Timestamps ────────────────────────────────────────────────────────
        start_raw = row.get('START_TIMESTAMP')
        end_raw   = row.get('END_TIMESTAMP')
        start_ts  = _parse_ts(start_raw)
        end_ts    = _parse_ts(end_raw)

        start_blank = start_raw is None or str(start_raw).strip() in ('', 'nan', 'NaN', 'None')
        end_blank   = end_raw   is None or str(end_raw).strip()   in ('', 'nan', 'NaN', 'None')

        if not start_blank and start_ts is None:
            reasons.append(
                f'START_TIMESTAMP "{start_raw}" could not be parsed — '
                 'use YYYY-MM-DD HH:MM:SS'
            )
        if not end_blank and end_ts is None:
            reasons.append(
                f'END_TIMESTAMP "{end_raw}" could not be parsed — '
                 'use YYYY-MM-DD HH:MM:SS'
            )

        tc_run_id = str(row.get('TC_RUN_ID', '')).strip()
        tc_id     = str(row.get('TC_ID',     '')).strip()

        if reasons:
            rejected.append({
                'row':       row_num,
                'tc_run_id': tc_run_id or '—',
                'tc_id':     tc_id     or '—',
                'reasons':   reasons,
            })
            continue

        duration = (
            (end_ts - start_ts).total_seconds()
            if start_ts and end_ts else None
        )

        valid.append({
            'tc_run_id':    tc_run_id,
            'suite_run_id': str(row['SUITE_RUN_ID']).strip(),
            'tc_id':        tc_id,
            'suite_id':     str(row['SUITE_ID']).strip(),
            'suite_name':   str(row['SUITE_NAME']).strip(),
            'sprint_name':  str(row['SPRINT']).strip(),
            'tc_name':      _str_or_none(row.get('TC_NAME')),
            'original_status':   status_raw,
            'effective_status':  status_raw,   # may be overridden in Step 3
            'executed_by':       _str_or_none(row.get('EXECUTED_BY')),
            'start_timestamp':   start_ts,
            'end_timestamp':     end_ts,
            'duration_seconds':  duration,
            'active_variance_applied': False,
            'variance_id':       None,
        })

    return valid, rejected


# ── Step 2: parse variances ────────────────────────────────────────────────────

def _parse_variances(
    df: pd.DataFrame,
    reference_dt: datetime,
) -> tuple[list[dict], dict[str, str]]:
    """
    Parse the VARIANCE SHEET and determine which variances are currently active.

    Returns:
        (variance_rows, active_map)
        active_map: {tc_id → variance_id} for every active variance
    """
    var_rows   = []
    active_map: dict[str, str] = {}   # tc_id → variance_id

    for _, row in df.iterrows():
        tc_id      = _str_or_none(row.get('TEST_CASE_ID'))
        var_id     = _str_or_none(row.get('VARIANCE_ID'))
        var_start  = _parse_ts(row.get('VARIANCE_START'))
        var_end    = _parse_ts(row.get('VARIANCE_END'))

        # Active: reference_dt falls within [VARIANCE_START, VARIANCE_END]
        is_active = bool(
            var_start and var_end
            and var_start <= reference_dt <= var_end
        )

        var_rows.append({
            'variance_id':             var_id,
            'test_case_id':            tc_id,
            'variance_reason':         _str_or_none(row.get('VARIANCE_REASON')),
            'variance_start':          var_start,
            'variance_end':            var_end,
            'variance_current_status': _str_or_none(row.get('VARIANCE_CURRENT_STATUS')),
            'dismissed_date':          _parse_ts(row.get('DISMISSED_DATE')),
            'is_active':               is_active,
        })

        # Only map active variances; if TC has multiple active variances, last wins
        if is_active and tc_id and var_id:
            active_map[tc_id] = var_id

    return var_rows, active_map


# ── Step 3: apply variance logic ──────────────────────────────────────────────

def _apply_variances(
    exec_rows: list[dict],
    active_map: dict[str, str],
) -> tuple[list[dict], int]:
    """
    For each FAIL testcase run: if its TC_ID has an active variance,
    set effective_status = PASS and flag active_variance_applied = True.

    Returns (updated_exec_rows, accepted_count).
    """
    accepted = 0
    for rec in exec_rows:
        if rec['original_status'] == FAIL_STATUS and rec['tc_id'] in active_map:
            rec['effective_status']        = PASS_STATUS
            rec['active_variance_applied'] = True
            rec['variance_id']             = active_map[rec['tc_id']]
            accepted += 1
    return exec_rows, accepted


# ── Step 4: dimension extractors ──────────────────────────────────────────────

def _extract_sprints(exec_rows: list[dict]) -> list[dict]:
    """
    One row per unique sprint name.
    sprint_start = min START_TIMESTAMP in that sprint.
    sprint_end   = max END_TIMESTAMP in that sprint.
    """
    sprints: dict[str, dict] = {}
    for rec in exec_rows:
        name = rec['sprint_name']
        ts_s = rec['start_timestamp']
        ts_e = rec['end_timestamp']
        if name not in sprints:
            sprints[name] = {'sprint_name': name, 'sprint_start': ts_s, 'sprint_end': ts_e}
        else:
            if ts_s and (sprints[name]['sprint_start'] is None or ts_s < sprints[name]['sprint_start']):
                sprints[name]['sprint_start'] = ts_s
            if ts_e and (sprints[name]['sprint_end'] is None or ts_e > sprints[name]['sprint_end']):
                sprints[name]['sprint_end'] = ts_e
    return list(sprints.values())


def _extract_test_suites(exec_rows: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for rec in exec_rows:
        key = rec['suite_id']
        if key not in seen:
            seen[key] = {
                'suite_id':    rec['suite_id'],
                'suite_name':  rec['suite_name'],
                'sprint_name': rec['sprint_name'],
            }
    return list(seen.values())


def _extract_testcases(exec_rows: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for rec in exec_rows:
        key = rec['tc_id']
        if key not in seen:
            seen[key] = {
                'tc_id':    rec['tc_id'],
                'tc_name':  rec['tc_name'],
                'suite_id': rec['suite_id'],
            }
    return list(seen.values())


# ── Step 6: suite run aggregation ─────────────────────────────────────────────

def _suite_run_status(effective_statuses: list[str]) -> str:
    """
    Determine suite run status from effective statuses of its TC runs.

    Priority:
      1. Any FAIL          → FAIL
      2. All PASS          → PASS
      3. Any EXECUTING     → EXECUTING
      4. Any ERROR         → ERROR
      5. Any BLOCKED       → BLOCKED
      6. Fallback          → first status seen
    """
    statuses = set(effective_statuses)
    if FAIL_STATUS in statuses:
        return FAIL_STATUS
    if all(s == PASS_STATUS for s in effective_statuses):
        return PASS_STATUS
    if 'EXECUTING' in statuses:
        return 'EXECUTING'
    if ERROR_STATUS in statuses:
        return ERROR_STATUS
    if 'BLOCKED' in statuses:
        return 'BLOCKED'
    return effective_statuses[0] if effective_statuses else PASS_STATUS


def _aggregate_suite_runs(exec_rows: list[dict]) -> list[dict]:
    """
    Group testcase runs by SUITE_RUN_ID and compute aggregate suite run fields.
    """
    groups: dict[str, dict] = {}

    for rec in exec_rows:
        srid = rec['suite_run_id']
        if srid not in groups:
            groups[srid] = {
                'suite_run_id':    srid,
                'suite_id':        rec['suite_id'],
                'suite_name':      rec['suite_name'],
                'sprint_name':     rec['sprint_name'],
                'start_timestamp': rec['start_timestamp'],
                'end_timestamp':   rec['end_timestamp'],
                '_statuses':       [rec['effective_status']],
            }
        else:
            g = groups[srid]
            # Expand time window
            if rec['start_timestamp'] and (
                g['start_timestamp'] is None
                or rec['start_timestamp'] < g['start_timestamp']
            ):
                g['start_timestamp'] = rec['start_timestamp']
            if rec['end_timestamp'] and (
                g['end_timestamp'] is None
                or rec['end_timestamp'] > g['end_timestamp']
            ):
                g['end_timestamp'] = rec['end_timestamp']
            g['_statuses'].append(rec['effective_status'])

    suite_runs = []
    for srid, g in groups.items():
        statuses = g.pop('_statuses')
        ts_s = g['start_timestamp']
        ts_e = g['end_timestamp']
        g['status']           = _suite_run_status(statuses)
        g['duration_seconds'] = (ts_e - ts_s).total_seconds() if ts_s and ts_e else None
        suite_runs.append(g)

    return suite_runs


# ── Step 7: CASI-engine compat DataFrame ──────────────────────────────────────

def _build_compat_df(
    exec_rows: list[dict],
    sprint_meta: list[dict],
) -> pd.DataFrame:
    """
    Build a DataFrame that the existing run_casi_from_df() can consume directly.

    Mapping:
      TC_ID            → first column ('TC ID')   — matches engine's r'TC-' filter
      EFFECTIVE_STATUS → 'Status'                 — ERROR remapped to ERR
      SUITE_NAME       → 'Sheet'
      sprints          → [(sprint_start_date, sprint_end_date)]

    One row per (TC_ID, sprint_name) — latest run per TC per sprint.
    This mirrors the old format's one-TC-per-sprint-per-module structure.
    """
    # Build sprint_name → (start_date, end_date) lookup
    sprint_dates: dict[str, tuple[date, date]] = {}
    for sp in sprint_meta:
        ts_s = sp['sprint_start']
        ts_e = sp['sprint_end']
        sprint_dates[sp['sprint_name']] = (
            ts_s.date() if ts_s else date.today(),
            ts_e.date() if ts_e else date.today(),
        )

    # Deduplicate: one row per (TC_ID, sprint_name) — keep latest end_timestamp
    dedup: dict[tuple[str, str], dict] = {}
    for rec in exec_rows:
        key = (rec['tc_id'], rec['sprint_name'])
        prev = dedup.get(key)
        if prev is None:
            dedup[key] = rec
        else:
            # Keep the most recent run
            if rec['end_timestamp'] and (
                prev['end_timestamp'] is None
                or rec['end_timestamp'] > prev['end_timestamp']
            ):
                dedup[key] = rec

    compat_rows = []
    for rec in dedup.values():
        sp_name = rec['sprint_name']
        sp_dates = sprint_dates.get(sp_name, (date.today(), date.today()))

        # Remap ERROR → ERR so the CASI engine's FAIL_STATES = {'FAIL','ERR'} picks it up
        engine_status = rec['effective_status']
        if engine_status == ERROR_STATUS:
            engine_status = _ENGINE_ERROR

        compat_rows.append({
            'TC ID':   rec['tc_id'],      # engine uses iloc[:,0]
            'TC Name': rec.get('tc_name') or rec['tc_id'],
            'Status':  engine_status,
            'Sheet':   rec['suite_name'],
            'sprints': [sp_dates],        # list of (start_date, end_date) tuples
        })

    if not compat_rows:
        return pd.DataFrame(columns=['TC ID', 'TC Name', 'Status', 'Sheet', 'sprints'])

    return pd.DataFrame(compat_rows)


# ── Serialization helpers (used by run_service) ────────────────────────────────

def serialize_for_storage(exec_rows: list[dict]) -> list[dict]:
    """
    Convert list of testcase_run dicts to JSON-safe dicts for test_records storage.
    Datetimes are ISO-formatted strings; None values are preserved.
    """
    out = []
    for rec in exec_rows:
        row = dict(rec)
        for key, val in row.items():
            if isinstance(val, datetime):
                row[key] = val.isoformat()
            elif isinstance(val, date):
                row[key] = val.isoformat()
        out.append(row)
    return out


def deserialize_from_storage(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Reconstruct testcase_run dicts from JSON storage (test_records.data).
    Returns (exec_rows, sprint_meta) so build_compat_df can be called again.

    Also re-derives sprint_meta (sprint dates) from the loaded rows.
    """
    ts_cols = ['start_timestamp', 'end_timestamp']
    rows = []
    for rec in records:
        row = dict(rec)
        for col in ts_cols:
            val = row.get(col)
            if val and isinstance(val, str):
                try:
                    row[col] = datetime.fromisoformat(val)
                except ValueError:
                    row[col] = None
        rows.append(row)

    sprint_meta = _extract_sprints(rows)
    return rows, sprint_meta
