"""
Unit tests for casi_engine.py — score math, normalization, traffic light.
No DB or Flask required; pure Python.
"""

import os
import pytest
import numpy as np

from casi_engine import (
    traffic_light,
    _normalize,
    _score,
)
from tests.conftest import FIXTURE_XLSX

DELPHI = np.array([0.22, 0.18, 0.17, 0.16, 0.14, 0.13])
DELPHI = DELPHI / DELPHI.sum()


# ── traffic_light ─────────────────────────────────────────────────────────────

class TestTrafficLight:
    def test_green_at_700(self):
        assert traffic_light(700) == 'Green'

    def test_green_above_700(self):
        assert traffic_light(999) == 'Green'

    def test_yellow_at_400(self):
        assert traffic_light(400) == 'Yellow'

    def test_yellow_between_400_and_700(self):
        assert traffic_light(550) == 'Yellow'

    def test_yellow_at_699(self):
        assert traffic_light(699) == 'Yellow'

    def test_red_below_400(self):
        assert traffic_light(399) == 'Red'

    def test_red_at_zero(self):
        assert traffic_light(0) == 'Red'


# ── _score ────────────────────────────────────────────────────────────────────

class TestScoreFormula:
    def test_max_score_is_999(self):
        rec = {
            'A_norm': 100, 'B_norm': 100, 'C_norm': 100,
            'D_norm': 100, 'E_norm': 100, 'F_norm': 100,
        }
        result = _score(rec, DELPHI)
        assert abs(result - 999) < 1.0

    def test_min_score_is_zero(self):
        rec = {
            'A_norm': 0, 'B_norm': 0, 'C_norm': 0,
            'D_norm': 0, 'E_norm': 0, 'F_norm': 0,
        }
        result = _score(rec, DELPHI)
        assert result == 0.0

    def test_score_between_0_and_999(self):
        rec = {
            'A_norm': 60, 'B_norm': 75, 'C_norm': 50,
            'D_norm': 80, 'E_norm': 45, 'F_norm': 90,
        }
        result = _score(rec, DELPHI)
        assert 0 <= result <= 999

    def test_score_increases_with_better_components(self):
        low = {k: 30 for k in ('A_norm', 'B_norm', 'C_norm', 'D_norm', 'E_norm', 'F_norm')}
        high = {k: 80 for k in ('A_norm', 'B_norm', 'C_norm', 'D_norm', 'E_norm', 'F_norm')}
        assert _score(high, DELPHI) > _score(low, DELPHI)

    def test_adaptive_weights_still_valid(self):
        """Any weight vector that sums to 1 should produce a score in [0, 999]."""
        w = np.array([0.30, 0.20, 0.20, 0.15, 0.10, 0.05])
        rec = {
            'A_norm': 55, 'B_norm': 65, 'C_norm': 45,
            'D_norm': 70, 'E_norm': 60, 'F_norm': 80,
        }
        result = _score(rec, w)
        assert 0 <= result <= 999


# ── _normalize ────────────────────────────────────────────────────────────────
# _normalize uses ABSOLUTE per-component bounds (not relative min-max).
# A-E are lower-is-better (inverted), F is higher-is-better.
# Absolute bounds for reference:
#   A (broken-index ratio): 0.0 – 1.0
#   B (avg fix days): 0.0 – 180.0
#   D (fail ratio %): 0.0 – 100.0
#   F (variance %): 0.0 – 100.0

def _make_records(a=0, b=0, c=0, d=0, e=0, f=100):
    """Helper: build a minimal record list for _normalize."""
    return [{'A': a, 'B': b, 'C': c, 'D': d, 'E': e, 'F': f}]


class TestNormalize:
    def test_all_same_lower_better_midpoint_gives_50(self):
        """A=0.5 is exactly mid-range on the (0,1) absolute scale → norm = 50."""
        rows = [
            {'A': 0.5, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 100},
            {'A': 0.5, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 100},
        ]
        result = _normalize(rows)
        for r in result:
            assert r['A_norm'] == 50.0

    def test_lower_is_better_max_raw_gives_zero(self):
        """Highest A value (worst) should map to 0."""
        rows = [
            {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 100},
            {'A': 10, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 100},
        ]
        result = _normalize(rows)
        worst = max(result, key=lambda r: r['A'])
        assert worst['A_norm'] == 0.0

    def test_lower_is_better_min_raw_gives_100(self):
        """Lowest A value (best) should map to 100."""
        rows = [
            {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 100},
            {'A': 10, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 100},
        ]
        result = _normalize(rows)
        best = min(result, key=lambda r: r['A'])
        assert best['A_norm'] == 100.0

    def test_higher_is_better_f_max_gives_100(self):
        """F=100 (full coverage) maps to F_norm=100 on the absolute (0,100) scale."""
        rows = [
            {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 0},
            {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 100},
        ]
        result = _normalize(rows)
        best = max(result, key=lambda r: r['F'])
        assert best['F_norm'] == 100.0

    def test_norm_values_between_0_and_100(self):
        rows = [
            {'A': i, 'B': 0, 'C': 0, 'D': 0, 'E': 0, 'F': 100}
            for i in range(6)
        ]
        result = _normalize(rows)
        for r in result:
            assert 0 <= r['A_norm'] <= 100


# ── Full engine integration (v2 format via ingest pipeline) ──────────────────

def _run_from_fixture():
    """Load FIXTURE_XLSX via the v2 ingest pipeline and run the engine."""
    from ingest import read_sheets, validate, transform
    from casi_engine import run_casi_from_df
    exec_df, var_df = read_sheets(FIXTURE_XLSX)
    result = transform(exec_df, var_df)
    return run_casi_from_df(result.compat_df, result.accepted_vars)


class TestRunCasi:
    def setup_method(self):
        if not os.path.exists(FIXTURE_XLSX):
            pytest.skip(f'Fixture not found: {FIXTURE_XLSX}')

    def test_run_casi_returns_scores(self):
        result = _run_from_fixture()
        assert 'scores' in result
        assert 0 <= result['scores']['casi_score'] <= 999
        assert 0 <= result['scores']['asi_score'] <= 999

    def test_run_casi_gate_is_string(self):
        result = _run_from_fixture()
        assert result['scores']['casi_gate'] in ('Green', 'Yellow', 'Red')

    def test_run_casi_has_sprint_history(self):
        result = _run_from_fixture()
        assert len(result.get('sprint_history', [])) > 0

    def test_run_casi_has_dataset_fields(self):
        result = _run_from_fixture()
        dataset = result['dataset']
        for field in ('tc_count', 'sprint_count', 'modules'):
            assert field in dataset, f'Missing dataset field: {field}'
        assert dataset['tc_count'] > 0

    def test_run_casi_has_components(self):
        result = _run_from_fixture()
        components = result.get('components', {})
        assert len(components) > 0, 'Expected at least one component'

    def test_run_casi_result_is_deterministic(self):
        """Same file twice must produce the same scores."""
        r1 = _run_from_fixture()
        r2 = _run_from_fixture()
        assert round(r1['scores']['casi_score'], 2) == round(r2['scores']['casi_score'], 2)
        assert round(r1['scores']['asi_score'], 2) == round(r2['scores']['asi_score'], 2)

    def test_run_casi_nonexistent_file_raises(self):
        from ingest import read_sheets
        with pytest.raises(Exception):
            read_sheets('/tmp/does_not_exist.xlsx')
