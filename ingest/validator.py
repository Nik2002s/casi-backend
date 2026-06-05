"""
CASI Ingest — Validation layer.

Validates the raw DataFrames from reader.py before transformation.
Returns a ValidationResult that is either clean or carries a list of errors.

Design: collect ALL errors in one pass so the caller can show them all at once.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
import pandas as pd

# ── Column contracts ───────────────────────────────────────────────────────────

EXECUTION_REQUIRED = [
    'TC_RUN_ID', 'SUITE_RUN_ID', 'TC_ID', 'SUITE_ID', 'SUITE_NAME',
    'SPRINT', 'TC_NAME', 'STATUS', 'START_TIMESTAMP', 'END_TIMESTAMP',
]

VARIANCE_REQUIRED = [
    'TEST_CASE_ID', 'VARIANCE_ID', 'VARIANCE_START', 'VARIANCE_END',
]

# SKIP is in real data; treat as a non-fail, non-pass neutral status.
ALLOWED_STATUSES = {'PASS', 'FAIL', 'ERROR', 'EXECUTING', 'BLOCKED', 'SKIP'}

TIMESTAMP_COLS_EXEC = ['START_TIMESTAMP', 'END_TIMESTAMP']
TIMESTAMP_COLS_VAR  = ['VARIANCE_START', 'VARIANCE_END']
OPTIONAL_TS_COLS    = ['DISMISSED_DATE']  # nullable


# ── Result types ───────────────────────────────────────────────────────────────

@dataclass
class ValidationError:
    sheet:   str
    row:     int | None   # 1-based Excel row number (header = row 1), None = file-level
    column:  str | None
    message: str

    def __str__(self):
        loc = f'Sheet "{self.sheet}"'
        if self.row is not None:
            loc += f', row {self.row}'
        if self.column:
            loc += f', column "{self.column}"'
        return f'{loc}: {self.message}'


@dataclass
class ValidationResult:
    errors:   list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, sheet, message, row=None, column=None):
        self.errors.append(ValidationError(sheet, row, column, message))

    def add_warning(self, sheet, message, row=None, column=None):
        self.warnings.append(ValidationError(sheet, row, column, message))

    def raise_if_invalid(self):
        if not self.is_valid:
            msgs = '\n'.join(str(e) for e in self.errors)
            raise ValueError(f'File validation failed:\n{msgs}')


# ── Helpers ────────────────────────────────────────────────────────────────────

_TS_FORMATS = [
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%dT%H:%M:%S',
    '%Y-%m-%d %H:%M',
    '%Y-%m-%d',
    '%d/%m/%Y %H:%M:%S',
    '%d/%m/%Y',
]


def _try_parse_ts(val: str) -> datetime | None:
    """Try multiple common timestamp formats; return None if all fail."""
    if not val or str(val).strip().lower() in ('nan', 'none', 'nat', ''):
        return None
    val = str(val).strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(val, fmt)
        except ValueError:
            pass
    return False   # sentinel: non-empty but unparseable


def _excel_row(df_idx: int) -> int:
    """Convert 0-based DataFrame index → 1-based Excel row (header = row 1)."""
    return df_idx + 2


# ── Main validator ─────────────────────────────────────────────────────────────

def validate(
    df_exec:  pd.DataFrame,
    df_var:   pd.DataFrame,
) -> ValidationResult:
    """
    Run all validation checks on both DataFrames.
    Returns a ValidationResult — check .is_valid before proceeding.
    """
    result = ValidationResult()

    _check_columns(result, df_exec, EXECUTION_REQUIRED, 'TEST EXECUTION')
    _check_columns(result, df_var,  VARIANCE_REQUIRED,  'VARIANCE SHEET')

    # Stop early if columns are wrong — row-level checks would be misleading
    if not result.is_valid:
        return result

    _check_execution_rows(result, df_exec)
    _check_variance_rows(result, df_var)

    return result


# ── Per-sheet validators ───────────────────────────────────────────────────────

def _check_columns(
    result: ValidationResult,
    df: pd.DataFrame,
    required: list[str],
    sheet: str,
) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        result.add_error(
            sheet,
            f'Missing required column(s): {missing}. '
            f'File has: {list(df.columns)}',
        )


def _check_execution_rows(result: ValidationResult, df: pd.DataFrame) -> None:
    sheet = 'TEST EXECUTION'

    seen_run_ids: dict[str, int] = {}   # run_id → first-seen excel row

    for idx, row in df.iterrows():
        xrow = _excel_row(idx)

        # ── Required fields not null ──────────────────────────────────────────
        for col in EXECUTION_REQUIRED:
            val = str(row.get(col, '')).strip()
            if not val or val.lower() in ('nan', 'none'):
                result.add_error(sheet, f'Required value is missing.', xrow, col)

        # ── TC_RUN_ID uniqueness ──────────────────────────────────────────────
        run_id = str(row.get('TC_RUN_ID', '')).strip()
        if run_id and run_id.lower() not in ('nan', 'none'):
            if run_id in seen_run_ids:
                result.add_error(
                    sheet,
                    f'Duplicate TC_RUN_ID "{run_id}" — first seen at row {seen_run_ids[run_id]}.',
                    xrow, 'TC_RUN_ID',
                )
            else:
                seen_run_ids[run_id] = xrow

        # ── STATUS validation ─────────────────────────────────────────────────
        status_raw = str(row.get('STATUS', '')).strip().upper()
        if status_raw and status_raw not in ('NAN', 'NONE'):
            if status_raw not in ALLOWED_STATUSES:
                result.add_error(
                    sheet,
                    f'Invalid STATUS "{status_raw}". '
                    f'Allowed values: {sorted(ALLOWED_STATUSES)}.',
                    xrow, 'STATUS',
                )

        # ── Timestamp parsing ─────────────────────────────────────────────────
        ts_vals: dict[str, datetime | None] = {}
        for col in TIMESTAMP_COLS_EXEC:
            parsed = _try_parse_ts(str(row.get(col, '')))
            if parsed is False:
                result.add_error(
                    sheet,
                    f'Cannot parse timestamp "{row.get(col)}".',
                    xrow, col,
                )
                ts_vals[col] = None
            else:
                ts_vals[col] = parsed

        # ── END >= START ──────────────────────────────────────────────────────
        start = ts_vals.get('START_TIMESTAMP')
        end   = ts_vals.get('END_TIMESTAMP')
        if start and end and end < start:
            result.add_error(
                sheet,
                f'END_TIMESTAMP ({end}) is before START_TIMESTAMP ({start}).',
                xrow, 'END_TIMESTAMP',
            )

    # ── SUITE_RUN_ID consistency: same SUITE_RUN_ID must have same SUITE_ID/SUITE_NAME ──
    suite_run_meta: dict[str, tuple[str, str]] = {}
    for idx, row in df.iterrows():
        xrow = _excel_row(idx)
        srid   = str(row.get('SUITE_RUN_ID', '')).strip()
        sid    = str(row.get('SUITE_ID', '')).strip()
        sname  = str(row.get('SUITE_NAME', '')).strip()
        if not srid or srid.lower() in ('nan', 'none'):
            continue
        if srid not in suite_run_meta:
            suite_run_meta[srid] = (sid, sname)
        else:
            exp_sid, exp_sname = suite_run_meta[srid]
            if sid != exp_sid or sname != exp_sname:
                result.add_warning(
                    sheet,
                    f'SUITE_RUN_ID "{srid}" has inconsistent SUITE_ID/SUITE_NAME. '
                    f'Expected ({exp_sid}, {exp_sname}), got ({sid}, {sname}).',
                    xrow, 'SUITE_RUN_ID',
                )


def _check_variance_rows(result: ValidationResult, df: pd.DataFrame) -> None:
    sheet = 'VARIANCE SHEET'

    for idx, row in df.iterrows():
        xrow = _excel_row(idx)

        # ── Required fields ───────────────────────────────────────────────────
        for col in VARIANCE_REQUIRED:
            val = str(row.get(col, '')).strip()
            if not val or val.lower() in ('nan', 'none'):
                result.add_error(sheet, 'Required value is missing.', xrow, col)

        # ── Timestamp parsing ─────────────────────────────────────────────────
        ts_vals: dict[str, datetime | None] = {}
        for col in TIMESTAMP_COLS_VAR:
            parsed = _try_parse_ts(str(row.get(col, '')))
            if parsed is False:
                result.add_error(
                    sheet,
                    f'Cannot parse timestamp "{row.get(col)}".',
                    xrow, col,
                )
                ts_vals[col] = None
            else:
                ts_vals[col] = parsed

        for col in OPTIONAL_TS_COLS:
            val = str(row.get(col, '')).strip()
            if val and val.lower() not in ('nan', 'none', 'nat'):
                if _try_parse_ts(val) is False:
                    result.add_error(
                        sheet,
                        f'Cannot parse timestamp "{val}".',
                        xrow, col,
                    )

        # ── VARIANCE_END >= VARIANCE_START ────────────────────────────────────
        vs = ts_vals.get('VARIANCE_START')
        ve = ts_vals.get('VARIANCE_END')
        if vs and ve and ve < vs:
            result.add_error(
                sheet,
                f'VARIANCE_END ({ve}) is before VARIANCE_START ({vs}).',
                xrow, 'VARIANCE_END',
            )
