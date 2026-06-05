"""
Unit tests for the v2 ingest pipeline.

Covers:
  - Successful ingestion (reader, validator, transformer)
  - Missing required columns
  - Invalid status values
  - Duplicate TC_RUN_ID
  - FAIL → PASS conversion due to active variance
  - FAIL remains FAIL when no active variance (expired / wrong TC)
  - Suite status: PASS only when all effective statuses are PASS
  - Suite status: FAIL when any effective status is FAIL
  - Suite status priority: EXECUTING > ERROR > BLOCKED
  - Suite start / end / duration calculation
  - Compat DataFrame shape and columns
  - ERROR remapped to ERR in compat DataFrame
  - Serialization round-trip (serialize + deserialize)
"""

import io
import os
import pytest
from datetime import datetime, timedelta, date

import pandas as pd

from ingest.reader import is_new_format, EXECUTION_SHEET, VARIANCE_SHEET
from ingest.validator import validate, ALLOWED_STATUSES
from ingest.transformer import (
    transform,
    _suite_run_status,
    _apply_variances,
    _normalize_execution,
    _parse_variances,
    serialize_for_storage,
    deserialize_from_storage,
    _build_compat_df,
    _extract_sprints,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

NOW     = datetime(2025, 3, 15, 12, 0, 0)    # fixed reference time for variance tests
PAST    = datetime(2025, 1, 1)
FUTURE  = datetime(2025, 12, 31)
FAR_PAST = datetime(2024, 1, 1)


def _make_exec_df(rows: list[dict]) -> pd.DataFrame:
    """Build a TEST EXECUTION DataFrame from a list of dicts."""
    defaults = {
        'TC_RUN_ID':        'RUN-0001',
        'SUITE_RUN_ID':     'SRUN-001',
        'TC_ID':            'TC-0001',
        'SUITE_ID':         'SUITE-001',
        'SUITE_NAME':       'Login Test Suite',
        'SPRINT':           'Sprint 1',
        'TC_NAME':          'Test case name',
        'STATUS':           'PASS',
        'EXECUTED_BY':      'user@example.com',
        'START_TIMESTAMP':  '2025-03-03 10:00:00',
        'END_TIMESTAMP':    '2025-03-03 10:01:00',
    }
    full_rows = [{**defaults, **r} for r in rows]
    return pd.DataFrame(full_rows)


def _make_var_df(rows: list[dict]) -> pd.DataFrame:
    """Build a VARIANCE SHEET DataFrame from a list of dicts."""
    defaults = {
        'TEST_CASE_ID':           'TC-0001',
        'VARIANCE_ID':            'VAR-001',
        'VARIANCE_REASON':        'Known issue',
        'VARIANCE_START':         '2025-01-01 00:00:00',
        'VARIANCE_END':           '2025-12-31 00:00:00',
        'VARIANCE_CURRENT_STATUS': 'ACTIVE',
        'DISMISSED_DATE':         None,
    }
    if not rows:
        return pd.DataFrame(columns=list(defaults.keys()))
    full_rows = [{**defaults, **r} for r in rows]
    return pd.DataFrame(full_rows)


# ── Reader ─────────────────────────────────────────────────────────────────────

class TestReader:
    def test_is_new_format_with_sample_file(self):
        sample = '/Users/abhinav/Downloads/CASI_QA_TestSuite_v2_verified_data.xlsx'
        if not os.path.exists(sample):
            pytest.skip('Sample file not found')
        assert is_new_format(sample) is True

    def test_is_new_format_returns_false_for_missing_file(self, tmp_path):
        assert is_new_format(str(tmp_path / 'nonexistent.xlsx')) is False

    def test_read_sheets_returns_two_dataframes(self):
        sample = '/Users/abhinav/Downloads/CASI_QA_TestSuite_v2_verified_data.xlsx'
        if not os.path.exists(sample):
            pytest.skip('Sample file not found')
        from ingest.reader import read_sheets
        df_exec, df_var = read_sheets(sample)
        assert isinstance(df_exec, pd.DataFrame)
        assert isinstance(df_var, pd.DataFrame)
        assert len(df_exec) == 500
        assert len(df_var) == 13   # header row excluded

    def test_read_sheets_columns_trimmed(self):
        sample = '/Users/abhinav/Downloads/CASI_QA_TestSuite_v2_verified_data.xlsx'
        if not os.path.exists(sample):
            pytest.skip('Sample file not found')
        from ingest.reader import read_sheets
        df_exec, _ = read_sheets(sample)
        for col in df_exec.columns:
            assert col == col.strip(), f'Column has leading/trailing spaces: {col!r}'


# ── Validator ──────────────────────────────────────────────────────────────────

class TestValidatorColumns:
    def test_valid_dataframes_pass(self):
        df_exec = _make_exec_df([{}])
        df_var  = _make_var_df([{}])
        result  = validate(df_exec, df_var)
        assert result.is_valid

    def test_missing_exec_column_reported(self):
        df_exec = _make_exec_df([{}]).drop(columns=['TC_RUN_ID'])
        df_var  = _make_var_df([{}])
        result  = validate(df_exec, df_var)
        assert not result.is_valid
        assert any('TC_RUN_ID' in str(e) for e in result.errors)
        assert any('TEST EXECUTION' in e.sheet for e in result.errors)

    def test_missing_multiple_exec_columns_all_reported(self):
        df_exec = _make_exec_df([{}]).drop(columns=['TC_RUN_ID', 'SUITE_ID', 'STATUS'])
        df_var  = _make_var_df([{}])
        result  = validate(df_exec, df_var)
        assert not result.is_valid
        combined = ' '.join(str(e) for e in result.errors)
        assert 'TC_RUN_ID' in combined
        assert 'SUITE_ID'  in combined
        assert 'STATUS'    in combined

    def test_missing_variance_column_reported(self):
        df_exec = _make_exec_df([{}])
        df_var  = _make_var_df([{}]).drop(columns=['VARIANCE_ID'])
        result  = validate(df_exec, df_var)
        assert not result.is_valid
        assert any('VARIANCE_ID' in str(e) for e in result.errors)
        assert any('VARIANCE SHEET' in e.sheet for e in result.errors)


class TestValidatorStatuses:
    def test_valid_statuses_pass(self):
        for status in ALLOWED_STATUSES:
            df_exec = _make_exec_df([{'STATUS': status}])
            result  = validate(df_exec, _make_var_df([]))
            assert result.is_valid, f'Status {status} should be valid'

    def test_invalid_status_reported_with_row_and_value(self):
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'STATUS': 'UNKNOWN'},
        ])
        result = validate(df_exec, _make_var_df([]))
        assert not result.is_valid
        err = result.errors[0]
        assert 'UNKNOWN' in err.message
        assert err.column == 'STATUS'
        assert err.row == 2    # header = row 1, first data row = row 2

    def test_case_insensitive_status_check(self):
        # Validation normalizes to uppercase before checking
        df_exec = _make_exec_df([{'STATUS': 'pass'}])
        result  = validate(df_exec, _make_var_df([]))
        assert result.is_valid

    def test_multiple_invalid_statuses_all_reported(self):
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'STATUS': 'BAD1'},
            {'TC_RUN_ID': 'RUN-0002', 'STATUS': 'BAD2'},
        ])
        result = validate(df_exec, _make_var_df([]))
        status_errors = [e for e in result.errors if e.column == 'STATUS']
        assert len(status_errors) == 2


class TestValidatorDuplicates:
    def test_duplicate_tc_run_id_reported(self):
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001'},
            {'TC_RUN_ID': 'RUN-0001'},   # duplicate
        ])
        result = validate(df_exec, _make_var_df([]))
        assert not result.is_valid
        dup_errors = [e for e in result.errors if 'Duplicate' in e.message]
        assert len(dup_errors) == 1
        assert 'RUN-0001' in dup_errors[0].message

    def test_unique_tc_run_ids_pass(self):
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001'},
            {'TC_RUN_ID': 'RUN-0002'},
        ])
        result = validate(df_exec, _make_var_df([]))
        assert result.is_valid


class TestValidatorTimestamps:
    def test_invalid_start_timestamp_reported(self):
        df_exec = _make_exec_df([{'START_TIMESTAMP': 'not-a-date'}])
        result  = validate(df_exec, _make_var_df([]))
        assert not result.is_valid
        ts_errors = [e for e in result.errors if e.column == 'START_TIMESTAMP']
        assert len(ts_errors) == 1

    def test_end_before_start_reported(self):
        df_exec = _make_exec_df([{
            'START_TIMESTAMP': '2025-03-03 10:00:00',
            'END_TIMESTAMP':   '2025-03-03 09:00:00',   # before start
        }])
        result = validate(df_exec, _make_var_df([]))
        assert not result.is_valid
        assert any('END_TIMESTAMP' in (e.column or '') for e in result.errors)

    def test_variance_end_before_start_reported(self):
        df_var = _make_var_df([{
            'VARIANCE_START': '2025-06-01 00:00:00',
            'VARIANCE_END':   '2025-01-01 00:00:00',
        }])
        result = validate(_make_exec_df([{}]), df_var)
        assert not result.is_valid
        assert any('VARIANCE_END' in (e.column or '') for e in result.errors)


# ── Transformer: variance logic ────────────────────────────────────────────────

class TestVarianceLogic:
    def test_fail_with_active_variance_becomes_pass(self):
        exec_rows, _ = _normalize_execution(_make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'STATUS': 'FAIL'},
        ]))
        _, active_map = _parse_variances(_make_var_df([{
            'TEST_CASE_ID': 'TC-0001',
            'VARIANCE_ID':  'VAR-001',
            'VARIANCE_START': PAST.strftime('%Y-%m-%d %H:%M:%S'),
            'VARIANCE_END':   FUTURE.strftime('%Y-%m-%d %H:%M:%S'),
        }]), reference_dt=NOW)

        rows, accepted = _apply_variances(exec_rows, active_map)

        assert rows[0]['effective_status'] == 'PASS'
        assert rows[0]['active_variance_applied'] is True
        assert rows[0]['variance_id'] == 'VAR-001'
        assert accepted == 1

    def test_fail_without_active_variance_stays_fail(self):
        exec_rows, _ = _normalize_execution(_make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'STATUS': 'FAIL'},
        ]))
        rows, accepted = _apply_variances(exec_rows, active_map={})

        assert rows[0]['effective_status'] == 'FAIL'
        assert rows[0]['active_variance_applied'] is False
        assert accepted == 0

    def test_fail_with_expired_variance_stays_fail(self):
        exec_rows, _ = _normalize_execution(_make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'STATUS': 'FAIL'},
        ]))
        _, active_map = _parse_variances(_make_var_df([{
            'TEST_CASE_ID': 'TC-0001',
            'VARIANCE_ID':  'VAR-001',
            'VARIANCE_START': FAR_PAST.strftime('%Y-%m-%d %H:%M:%S'),
            'VARIANCE_END':   PAST.strftime('%Y-%m-%d %H:%M:%S'),  # already expired
        }]), reference_dt=NOW)

        rows, accepted = _apply_variances(exec_rows, active_map)

        assert rows[0]['effective_status'] == 'FAIL'
        assert accepted == 0

    def test_variance_only_applies_to_matching_tc(self):
        exec_rows, _ = _normalize_execution(_make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'STATUS': 'FAIL'},
            {'TC_RUN_ID': 'RUN-0002', 'TC_ID': 'TC-0002', 'STATUS': 'FAIL'},
        ]))
        _, active_map = _parse_variances(_make_var_df([{
            'TEST_CASE_ID': 'TC-0001',   # only covers TC-0001
            'VARIANCE_ID':  'VAR-001',
            'VARIANCE_START': PAST.strftime('%Y-%m-%d %H:%M:%S'),
            'VARIANCE_END':   FUTURE.strftime('%Y-%m-%d %H:%M:%S'),
        }]), reference_dt=NOW)

        rows, accepted = _apply_variances(exec_rows, active_map)

        assert rows[0]['effective_status'] == 'PASS'   # TC-0001: variance applied
        assert rows[1]['effective_status'] == 'FAIL'   # TC-0002: no variance
        assert accepted == 1

    def test_pass_status_unaffected_by_variance(self):
        exec_rows, _ = _normalize_execution(_make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'STATUS': 'PASS'},
        ]))
        _, active_map = _parse_variances(_make_var_df([{
            'TEST_CASE_ID': 'TC-0001',
            'VARIANCE_ID':  'VAR-001',
            'VARIANCE_START': PAST.strftime('%Y-%m-%d %H:%M:%S'),
            'VARIANCE_END':   FUTURE.strftime('%Y-%m-%d %H:%M:%S'),
        }]), reference_dt=NOW)

        rows, accepted = _apply_variances(exec_rows, active_map)

        assert rows[0]['effective_status'] == 'PASS'
        assert rows[0]['active_variance_applied'] is False
        assert accepted == 0


# ── Transformer: suite run status ─────────────────────────────────────────────

class TestSuiteRunStatus:
    def test_all_pass_returns_pass(self):
        assert _suite_run_status(['PASS', 'PASS', 'PASS']) == 'PASS'

    def test_any_fail_returns_fail(self):
        assert _suite_run_status(['PASS', 'FAIL', 'PASS']) == 'FAIL'

    def test_fail_beats_all_others(self):
        assert _suite_run_status(['EXECUTING', 'FAIL', 'ERROR', 'BLOCKED']) == 'FAIL'

    def test_executing_beats_error_and_blocked(self):
        assert _suite_run_status(['PASS', 'EXECUTING', 'ERROR', 'BLOCKED']) == 'EXECUTING'

    def test_error_beats_blocked(self):
        assert _suite_run_status(['PASS', 'ERROR', 'BLOCKED']) == 'ERROR'

    def test_blocked_when_no_higher_priority(self):
        assert _suite_run_status(['PASS', 'BLOCKED']) == 'BLOCKED'

    def test_mixed_with_variance_corrected_fail(self):
        # After variance: FAIL → PASS; suite sees only PASS values
        assert _suite_run_status(['PASS', 'PASS', 'PASS']) == 'PASS'

    def test_single_fail_makes_suite_fail(self):
        assert _suite_run_status(['FAIL']) == 'FAIL'

    def test_single_pass_returns_pass(self):
        assert _suite_run_status(['PASS']) == 'PASS'


# ── Transformer: suite run aggregation ────────────────────────────────────────

class TestSuiteRunAggregation:
    def _run_transform(self, exec_rows_data, ref_dt=NOW):
        df_exec = _make_exec_df(exec_rows_data)
        df_var  = _make_var_df([])
        return transform(df_exec, df_var, reference_dt=ref_dt)

    def test_suite_start_is_minimum_timestamp(self):
        result = self._run_transform([
            {'TC_RUN_ID': 'RUN-0001', 'SUITE_RUN_ID': 'SRUN-001',
             'START_TIMESTAMP': '2025-03-03 10:00:00', 'END_TIMESTAMP': '2025-03-03 10:01:00'},
            {'TC_RUN_ID': 'RUN-0002', 'SUITE_RUN_ID': 'SRUN-001',
             'START_TIMESTAMP': '2025-03-03 09:00:00', 'END_TIMESTAMP': '2025-03-03 09:30:00'},
        ])
        sr = result.suite_runs[0]
        assert sr['start_timestamp'] == datetime(2025, 3, 3, 9, 0, 0)

    def test_suite_end_is_maximum_timestamp(self):
        result = self._run_transform([
            {'TC_RUN_ID': 'RUN-0001', 'SUITE_RUN_ID': 'SRUN-001',
             'START_TIMESTAMP': '2025-03-03 10:00:00', 'END_TIMESTAMP': '2025-03-03 10:05:00'},
            {'TC_RUN_ID': 'RUN-0002', 'SUITE_RUN_ID': 'SRUN-001',
             'START_TIMESTAMP': '2025-03-03 10:01:00', 'END_TIMESTAMP': '2025-03-03 11:00:00'},
        ])
        sr = result.suite_runs[0]
        assert sr['end_timestamp'] == datetime(2025, 3, 3, 11, 0, 0)

    def test_suite_duration_is_end_minus_start(self):
        result = self._run_transform([
            {'TC_RUN_ID': 'RUN-0001', 'SUITE_RUN_ID': 'SRUN-001',
             'START_TIMESTAMP': '2025-03-03 10:00:00', 'END_TIMESTAMP': '2025-03-03 10:10:00'},
            {'TC_RUN_ID': 'RUN-0002', 'SUITE_RUN_ID': 'SRUN-001',
             'START_TIMESTAMP': '2025-03-03 10:05:00', 'END_TIMESTAMP': '2025-03-03 10:15:00'},
        ])
        sr = result.suite_runs[0]
        assert sr['duration_seconds'] == 15 * 60   # 15 minutes

    def test_two_separate_suite_runs(self):
        result = self._run_transform([
            {'TC_RUN_ID': 'RUN-0001', 'SUITE_RUN_ID': 'SRUN-001'},
            {'TC_RUN_ID': 'RUN-0002', 'SUITE_RUN_ID': 'SRUN-002'},
        ])
        assert len(result.suite_runs) == 2

    def test_suite_status_pass_all_pass(self):
        result = self._run_transform([
            {'TC_RUN_ID': 'RUN-0001', 'SUITE_RUN_ID': 'SRUN-001', 'STATUS': 'PASS'},
            {'TC_RUN_ID': 'RUN-0002', 'SUITE_RUN_ID': 'SRUN-001', 'STATUS': 'PASS'},
        ])
        assert result.suite_runs[0]['status'] == 'PASS'

    def test_suite_status_fail_any_fail(self):
        result = self._run_transform([
            {'TC_RUN_ID': 'RUN-0001', 'SUITE_RUN_ID': 'SRUN-001', 'STATUS': 'PASS'},
            {'TC_RUN_ID': 'RUN-0002', 'SUITE_RUN_ID': 'SRUN-001', 'STATUS': 'FAIL'},
        ])
        assert result.suite_runs[0]['status'] == 'FAIL'

    def test_suite_status_executing_beats_error(self):
        result = self._run_transform([
            {'TC_RUN_ID': 'RUN-0001', 'SUITE_RUN_ID': 'SRUN-001', 'STATUS': 'ERROR'},
            {'TC_RUN_ID': 'RUN-0002', 'SUITE_RUN_ID': 'SRUN-001', 'STATUS': 'EXECUTING'},
        ])
        assert result.suite_runs[0]['status'] == 'EXECUTING'

    def test_suite_status_error_beats_blocked(self):
        result = self._run_transform([
            {'TC_RUN_ID': 'RUN-0001', 'SUITE_RUN_ID': 'SRUN-001', 'STATUS': 'BLOCKED'},
            {'TC_RUN_ID': 'RUN-0002', 'SUITE_RUN_ID': 'SRUN-001', 'STATUS': 'ERROR'},
        ])
        assert result.suite_runs[0]['status'] == 'ERROR'


# ── Transformer: compat DataFrame ─────────────────────────────────────────────

class TestCompatDf:
    def test_compat_df_has_required_columns(self):
        df_exec = _make_exec_df([{'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-LGN-001'}])
        result  = transform(df_exec, _make_var_df([]), reference_dt=NOW)
        df      = result.compat_df
        assert 'TC ID'  in df.columns
        assert 'Status' in df.columns
        assert 'Sheet'  in df.columns
        assert 'sprints' in df.columns

    def test_tc_id_starts_with_tc(self):
        df_exec = _make_exec_df([{'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-LGN-001'}])
        result  = transform(df_exec, _make_var_df([]), reference_dt=NOW)
        for tc_id in result.compat_df['TC ID']:
            assert tc_id.startswith('TC-'), f'Expected TC- prefix, got {tc_id}'

    def test_error_remapped_to_err_in_compat(self):
        df_exec = _make_exec_df([{'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'STATUS': 'ERROR'}])
        result  = transform(df_exec, _make_var_df([]), reference_dt=NOW)
        assert result.compat_df.iloc[0]['Status'] == 'ERR'

    def test_sprints_column_contains_date_tuples(self):
        df_exec = _make_exec_df([{'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001'}])
        result  = transform(df_exec, _make_var_df([]), reference_dt=NOW)
        sp = result.compat_df.iloc[0]['sprints']
        assert isinstance(sp, list)
        assert len(sp) == 1
        assert isinstance(sp[0][0], date)
        assert isinstance(sp[0][1], date)

    def test_dedup_keeps_one_row_per_tc_per_sprint(self):
        # Two runs of same TC in same sprint → one row in compat_df
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'SPRINT': 'Sprint 1',
             'START_TIMESTAMP': '2025-03-03 10:00:00', 'END_TIMESTAMP': '2025-03-03 10:01:00'},
            {'TC_RUN_ID': 'RUN-0002', 'TC_ID': 'TC-0001', 'SPRINT': 'Sprint 1',
             'START_TIMESTAMP': '2025-03-03 11:00:00', 'END_TIMESTAMP': '2025-03-03 11:02:00'},
        ])
        result = transform(df_exec, _make_var_df([]), reference_dt=NOW)
        assert len(result.compat_df) == 1

    def test_different_sprints_produce_separate_rows(self):
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'SPRINT': 'Sprint 1'},
            {'TC_RUN_ID': 'RUN-0002', 'TC_ID': 'TC-0001', 'SPRINT': 'Sprint 2'},
        ])
        result = transform(df_exec, _make_var_df([]), reference_dt=NOW)
        assert len(result.compat_df) == 2


# ── Transformer: accepted_vars count ──────────────────────────────────────────

class TestAcceptedVars:
    def test_accepted_vars_count_matches_variance_applied_rows(self):
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-0001', 'STATUS': 'FAIL'},
            {'TC_RUN_ID': 'RUN-0002', 'TC_ID': 'TC-0002', 'STATUS': 'FAIL'},
            {'TC_RUN_ID': 'RUN-0003', 'TC_ID': 'TC-0003', 'STATUS': 'FAIL'},
        ])
        df_var = _make_var_df([
            {'TEST_CASE_ID': 'TC-0001', 'VARIANCE_ID': 'VAR-001',
             'VARIANCE_START': PAST.strftime('%Y-%m-%d %H:%M:%S'),
             'VARIANCE_END':   FUTURE.strftime('%Y-%m-%d %H:%M:%S')},
            {'TEST_CASE_ID': 'TC-0002', 'VARIANCE_ID': 'VAR-002',
             'VARIANCE_START': PAST.strftime('%Y-%m-%d %H:%M:%S'),
             'VARIANCE_END':   FUTURE.strftime('%Y-%m-%d %H:%M:%S')},
            # TC-0003: no variance
        ])
        result = transform(df_exec, df_var, reference_dt=NOW)
        assert result.accepted_vars == 2


# ── Serialization round-trip ───────────────────────────────────────────────────

class TestSerialization:
    def test_serialize_and_deserialize_roundtrip(self):
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-LGN-001', 'STATUS': 'FAIL'},
            {'TC_RUN_ID': 'RUN-0002', 'TC_ID': 'TC-LGN-002', 'STATUS': 'PASS'},
        ])
        ingest = transform(df_exec, _make_var_df([]), reference_dt=NOW)
        serialized = serialize_for_storage(ingest.testcase_runs)

        # All datetime values should be strings now
        for rec in serialized:
            for key in ('start_timestamp', 'end_timestamp'):
                val = rec.get(key)
                if val is not None:
                    assert isinstance(val, str), f'{key} should be str after serialization'

        exec_rows, sprint_meta = deserialize_from_storage(serialized)

        # Datetimes should be restored
        for rec in exec_rows:
            for key in ('start_timestamp', 'end_timestamp'):
                val = rec.get(key)
                if val is not None:
                    assert isinstance(val, datetime), f'{key} should be datetime after deserialization'

    def test_compat_df_rebuilt_from_deserialized(self):
        df_exec = _make_exec_df([
            {'TC_RUN_ID': 'RUN-0001', 'TC_ID': 'TC-LGN-001', 'STATUS': 'FAIL'},
        ])
        ingest     = transform(df_exec, _make_var_df([]), reference_dt=NOW)
        serialized = serialize_for_storage(ingest.testcase_runs)
        exec_rows, sprint_meta = deserialize_from_storage(serialized)

        from ingest.transformer import _build_compat_df
        compat = _build_compat_df(exec_rows, sprint_meta)
        assert len(compat) == 1
        assert 'TC ID' in compat.columns


# ── Full pipeline on sample file ───────────────────────────────────────────────

class TestFullPipelineOnSampleFile:
    """Integration tests using the real sample Excel file."""

    SAMPLE = '/Users/abhinav/Downloads/CASI_QA_TestSuite_v2_verified_data.xlsx'

    def _ingest(self):
        if not os.path.exists(self.SAMPLE):
            pytest.skip('Sample file not found')
        from ingest.reader import read_sheets
        df_exec, df_var = read_sheets(self.SAMPLE)
        result = validate(df_exec, df_var)
        result.raise_if_invalid()
        return transform(df_exec, df_var)

    def test_sample_file_validates_cleanly(self):
        if not os.path.exists(self.SAMPLE):
            pytest.skip('Sample file not found')
        from ingest.reader import read_sheets
        df_exec, df_var = read_sheets(self.SAMPLE)
        result = validate(df_exec, df_var)
        # SKIP is in data; validator should accept it
        assert result.is_valid, '\n'.join(str(e) for e in result.errors)

    def test_sample_produces_correct_sprint_count(self):
        ingest = self._ingest()
        assert len(ingest.sprints) == 5

    def test_sample_produces_correct_suite_count(self):
        ingest = self._ingest()
        assert len(ingest.test_suites) == 5

    def test_sample_testcase_run_count(self):
        ingest = self._ingest()
        assert len(ingest.testcase_runs) == 500

    def test_sample_all_runs_have_effective_status(self):
        ingest = self._ingest()
        for r in ingest.testcase_runs:
            assert r['effective_status'] is not None
            assert r['effective_status'] != ''

    def test_sample_variance_applied_rows_have_pass_effective(self):
        ingest = self._ingest()
        for r in ingest.testcase_runs:
            if r['active_variance_applied']:
                assert r['original_status'] == 'FAIL'
                assert r['effective_status'] == 'PASS'

    def test_sample_compat_df_passes_casi_engine(self):
        ingest = self._ingest()
        from casi_engine import run_casi_from_df
        result = run_casi_from_df(ingest.compat_df, accepted_vars=ingest.accepted_vars)
        scores = result['scores']
        assert 0 <= scores['casi_score'] <= 999
        assert 0 <= scores['asi_score']  <= 999
        assert scores['casi_gate'] in ('Green', 'Yellow', 'Red')

    def test_sample_suite_runs_count(self):
        ingest = self._ingest()
        # Each suite run ID is unique
        ids = {sr['suite_run_id'] for sr in ingest.suite_runs}
        assert len(ids) == len(ingest.suite_runs)

    def test_sample_suite_runs_have_valid_status(self):
        ingest = self._ingest()
        valid = {'PASS', 'FAIL', 'ERROR', 'EXECUTING', 'BLOCKED', 'SKIP'}
        for sr in ingest.suite_runs:
            assert sr['status'] in valid, f"Unexpected status: {sr['status']}"
