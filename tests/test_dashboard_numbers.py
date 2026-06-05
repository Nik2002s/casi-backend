"""
Dashboard-numbers test suite.

These tests verify the numbers a user actually sees after clicking a project
card — scores, gate, components, sprint history, module health, open failures
— across six different synthetic project scenarios.

Scenarios
---------
P1  Perfect health     all TCs pass every sprint, variances = 0
P2  Total failure      all TCs fail every sprint, variances = 0
P3  Mixed health       ~half pass, ~half fail across two modules
P4  Variance-covered   all failures have accepted variances → F-component high
P5  Single sprint      only one sprint of data (warm-up boundary)
P6  Multi-sprint trend gradual recovery over 6 sprints (warm-up completes)

Every scenario runs the engine directly (no DB / HTTP) to stay fast and
deterministic, and then validates what the UI would display.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime

from ingest.transformer import _build_compat_df
from casi_engine import run_casi_from_df, traffic_light


# ── helpers ───────────────────────────────────────────────────────────────────

def _date(y, m, d):
    return date(y, m, d)


def _row(tc_id, suite, sprint_start, sprint_end, status):
    """Build one testcase_run dict matching what _build_compat_df expects."""
    return {
        'tc_run_id':        f'RUN-{tc_id}-{sprint_start}',
        'suite_run_id':     f'SR-{suite}-{sprint_start}',
        'tc_id':            tc_id,
        'suite_id':         f'SUITE_{suite}',
        'suite_name':       suite,
        'sprint_name':      f'SPRINT_{sprint_start}',
        'tc_name':          f'Test {tc_id}',
        'effective_status': status,
        'executed_by':      'tester',
        'start_timestamp':  datetime(int(str(sprint_start)[:4]), int(str(sprint_start)[4:6]), int(str(sprint_start)[6:]), 9, 0, 0),
        'end_timestamp':    datetime(int(str(sprint_end)[:4]), int(str(sprint_end)[4:6]), int(str(sprint_end)[6:]), 17, 0, 0),
        'variance_id':      None,
    }


def _sprint_dict(sprint_label, start_dt, end_dt):
    """sprint_start/sprint_end must be datetime objects (transformer calls .date() on them)."""
    return {'sprint_name': sprint_label, 'sprint_start': start_dt, 'sprint_end': end_dt}


def _make_rows_and_sprints(spec):
    """
    spec: list of (tc_id, suite, sprint_start_yyyymmdd, sprint_end_yyyymmdd, status)
    Returns (exec_rows, sprints_list) ready for _build_compat_df.
    """
    rows = [_row(tc, suite, s_start, s_end, status) for tc, suite, s_start, s_end, status in spec]

    # Build sprints list (deduplicated by sprint label)
    seen = {}
    for _, _, s_start, s_end, _ in spec:
        label = f'SPRINT_{s_start}'
        if label not in seen:
            y,  m,  d  = int(str(s_start)[:4]), int(str(s_start)[4:6]), int(str(s_start)[6:])
            y2, m2, d2 = int(str(s_end)[:4]),   int(str(s_end)[4:6]),   int(str(s_end)[6:])
            seen[label] = _sprint_dict(
                label,
                datetime(y,  m,  d,  0, 0, 0),
                datetime(y2, m2, d2, 23, 59, 59),
            )

    return rows, list(seen.values())


def _run(spec, accepted_vars=0):
    rows, sprints = _make_rows_and_sprints(spec)
    df = _build_compat_df(rows, sprints)
    return run_casi_from_df(df, accepted_vars)


# ── shared sprint date pairs (start_yyyymmdd, end_yyyymmdd) ──────────────────

S1 = (20250101, 20250114)
S2 = (20250201, 20250214)
S3 = (20250301, 20250314)
S4 = (20250401, 20250414)
S5 = (20250501, 20250514)
S6 = (20250601, 20250614)

SUITES = ['Auth', 'Payments']


# ═══════════════════════════════════════════════════════════════════════════════
# P1 — Perfect health (all PASS, no variances)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope='module')
def p1_result():
    # Use suite-prefixed IDs so (tc_id, sprint_name) keys never collide
    spec = [
        (f'TC-{suite[:1]}{i:03d}', suite, *S1, 'PASS')
        for i in range(1, 6) for suite in SUITES
    ] + [
        (f'TC-{suite[:1]}{i:03d}', suite, *S2, 'PASS')
        for i in range(1, 6) for suite in SUITES
    ]
    return _run(spec)


class TestP1_PerfectHealth:
    """All tests pass every sprint. Dashboard should show max-health numbers."""

    def test_gate_is_green(self, p1_result):
        assert p1_result['scores']['casi_gate'] == 'Green'
        assert p1_result['scores']['asi_gate'] == 'Green'

    def test_casi_score_in_valid_range(self, p1_result):
        s = p1_result['scores']['casi_score']
        assert 0 <= s <= 999, f'CASI out of range: {s}'

    def test_asi_score_in_valid_range(self, p1_result):
        s = p1_result['scores']['asi_score']
        assert 0 <= s <= 999

    def test_no_open_failures(self, p1_result):
        assert p1_result['open_failures'] == []

    def test_no_critical_failures(self, p1_result):
        crits = [f for f in p1_result['open_failures'] if f['priority'] == 'Critical']
        assert crits == []

    def test_sprint_history_has_correct_count(self, p1_result):
        assert len(p1_result['sprint_history']) == 2

    def test_sprint_scores_in_range(self, p1_result):
        for sprint in p1_result['sprint_history']:
            assert 0 <= sprint['casi_score'] <= 999
            assert 0 <= sprint['asi_score'] <= 999

    def test_dataset_tc_count(self, p1_result):
        # 5 unique TCs × 2 suites = 10 unique TC IDs (each appears in 2 sprints)
        assert p1_result['dataset']['tc_count'] == 10

    def test_dataset_sprint_count(self, p1_result):
        assert p1_result['dataset']['sprint_count'] == 2

    def test_dataset_modules_present(self, p1_result):
        modules = p1_result['dataset']['modules']
        for suite in SUITES:
            assert suite in modules

    def test_components_all_present(self, p1_result):
        for k in ('A', 'B', 'C', 'D', 'E', 'F'):
            assert k in p1_result['components'], f'Missing component {k}'

    def test_component_normalized_in_range(self, p1_result):
        for k, c in p1_result['components'].items():
            assert 0 <= c['normalized'] <= 100, f'{k}_norm={c["normalized"]} out of range'

    def test_d_fail_ratio_zero(self, p1_result):
        """No failures → Fail Ratio raw value is 0."""
        assert p1_result['components']['D']['raw'] == 0.0

    def test_diagnostic_not_triggered(self, p1_result):
        assert p1_result['diagnostic_triggered'] is False

    def test_weights_sum_to_one(self, p1_result):
        total = sum(c['weight_adapted'] for c in p1_result['components'].values())
        assert abs(total - 1.0) < 0.01, f'Weights sum to {total}'


# ═══════════════════════════════════════════════════════════════════════════════
# P2 — Total failure (all FAIL, no variances)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope='module')
def p2_result():
    # Use suite-prefixed IDs so (tc_id, sprint_name) keys never collide
    spec = [
        (f'TC-{suite[:1]}{i:03d}', suite, *S1, 'FAIL')
        for i in range(1, 6) for suite in SUITES
    ] + [
        (f'TC-{suite[:1]}{i:03d}', suite, *S2, 'FAIL')
        for i in range(1, 6) for suite in SUITES
    ]
    return _run(spec)


class TestP2_TotalFailure:
    """All tests fail. Dashboard should show worst-case numbers."""

    def test_gate_is_not_green(self, p2_result):
        assert p2_result['scores']['casi_gate'] != 'Green'

    def test_casi_score_lower_than_green_threshold(self, p2_result):
        assert p2_result['scores']['casi_score'] < 700

    def test_open_failures_all_tcs(self, p2_result):
        """Every unique failing TC should appear exactly once."""
        failure_ids = {f['tc_id'] for f in p2_result['open_failures']}
        # 5 TCs per suite × 2 suites = 10 unique failing TCs
        assert len(failure_ids) == 10

    def test_all_failures_have_priority(self, p2_result):
        for f in p2_result['open_failures']:
            assert f['priority'] in ('Critical', 'High', 'Medium')

    def test_failures_sorted_by_days_open_desc(self, p2_result):
        days = [f['days_open'] for f in p2_result['open_failures']]
        assert days == sorted(days, reverse=True)

    def test_d_fail_ratio_is_100(self, p2_result):
        """Every TC fails → Fail Ratio raw = 100."""
        assert p2_result['components']['D']['raw'] == 100.0

    def test_e_suite_fail_is_100(self, p2_result):
        """Any suite failure → E raw = 100."""
        assert p2_result['components']['E']['raw'] == 100.0

    def test_diagnostic_triggered(self, p2_result):
        assert p2_result['diagnostic_triggered'] is True

    def test_sprint_gates_not_green(self, p2_result):
        for sprint in p2_result['sprint_history']:
            assert sprint['casi_gate'] != 'Green'


# ═══════════════════════════════════════════════════════════════════════════════
# P3 — Mixed health (~50% pass, ~50% fail, two modules)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope='module')
def p3_result():
    spec = []
    for sprint in (S1, S2):
        for i in range(1, 6):
            status = 'PASS' if i <= 3 else 'FAIL'
            spec.append((f'TC-{i:03d}', 'Auth', *sprint, status))
        for i in range(1, 6):
            status = 'PASS' if i <= 2 else 'FAIL'
            spec.append((f'TC-P{i:03d}', 'Payments', *sprint, status))
    return _run(spec)


class TestP3_MixedHealth:
    """Mixed pass/fail. Both modules on the dashboard. Gate should be Red/Yellow."""

    def test_both_modules_in_result(self, p3_result):
        modules = p3_result['dataset']['modules']
        assert 'Auth' in modules
        assert 'Payments' in modules

    def test_some_open_failures(self, p3_result):
        assert len(p3_result['open_failures']) > 0

    def test_not_all_tcs_failing(self, p3_result):
        # 3 pass + 2 fail in Auth; 2 pass + 3 fail in Payments → 5 unique failing TCs
        failure_ids = {f['tc_id'] for f in p3_result['open_failures']}
        assert len(failure_ids) == 5

    def test_failure_modules_are_correct(self, p3_result):
        modules_with_failures = {f['module'] for f in p3_result['open_failures']}
        assert 'Payments' in modules_with_failures   # 3/5 fail

    def test_scores_in_range(self, p3_result):
        s = p3_result['scores']
        assert 0 <= s['casi_score'] <= 999
        assert 0 <= s['asi_score'] <= 999

    def test_d_fail_ratio_between_0_and_100(self, p3_result):
        d = p3_result['components']['D']['raw']
        assert 0 < d < 100, f'Expected partial fail ratio, got {d}'

    def test_sprint_history_length(self, p3_result):
        assert len(p3_result['sprint_history']) == 2

    def test_sprint_modules_both_present(self, p3_result):
        for sprint in p3_result['sprint_history']:
            assert 'Auth' in sprint['modules']
            assert 'Payments' in sprint['modules']

    def test_sprint_module_tc_counts(self, p3_result):
        last = p3_result['sprint_history'][-1]
        assert last['modules']['Auth']['tc_count'] == 5
        assert last['modules']['Payments']['tc_count'] == 5

    def test_sprint_module_norm_scores_in_range(self, p3_result):
        for sprint in p3_result['sprint_history']:
            for mod, mdata in sprint['modules'].items():
                for comp, val in mdata['norm_scores'].items():
                    assert 0 <= val <= 100, f'{sprint["sprint_start"]} {mod} {comp}={val}'


# ═══════════════════════════════════════════════════════════════════════════════
# P4 — Variance-covered failures (F-component test)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope='module')
def p4_result():
    """All failures are variance-covered → accepted_vars = n_fail."""
    spec = [
        (f'TC-{i:03d}', 'Auth', *S1, 'PASS' if i <= 2 else 'FAIL')
        for i in range(1, 6)
    ] + [
        (f'TC-{i:03d}', 'Auth', *S2, 'PASS' if i <= 2 else 'FAIL')
        for i in range(1, 6)
    ]
    # 3 failing TCs per sprint → 3 accepted variances
    return _run(spec, accepted_vars=3)


@pytest.fixture(scope='module')
def p4_no_variance_result():
    """Same data but no variances accepted — for comparison."""
    spec = [
        (f'TC-{i:03d}', 'Auth', *S1, 'PASS' if i <= 2 else 'FAIL')
        for i in range(1, 6)
    ] + [
        (f'TC-{i:03d}', 'Auth', *S2, 'PASS' if i <= 2 else 'FAIL')
        for i in range(1, 6)
    ]
    return _run(spec, accepted_vars=0)


class TestP4_VarianceCoveredFailures:

    def test_f_component_higher_with_variances(self, p4_result, p4_no_variance_result):
        """F raw value should be higher when failures are variance-covered."""
        f_with    = p4_result['components']['F']['raw']
        f_without = p4_no_variance_result['components']['F']['raw']
        assert f_with > f_without, f'Expected F({f_with}) > F({f_without})'

    def test_casi_score_higher_with_variances(self, p4_result, p4_no_variance_result):
        """Accepting variances should raise or maintain CASI score."""
        s_with    = p4_result['scores']['casi_score']
        s_without = p4_no_variance_result['scores']['casi_score']
        assert s_with >= s_without, f'CASI {s_with} should be >= {s_without}'

    def test_f_normalized_in_range(self, p4_result):
        assert 0 <= p4_result['components']['F']['normalized'] <= 100

    def test_open_failures_still_listed(self, p4_result):
        """Variances reduce score penalty but don't hide failures from the UI."""
        assert len(p4_result['open_failures']) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# P5 — Single sprint (edge case: warm-up > data)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope='module')
def p5_result():
    spec = [
        (f'TC-{i:03d}', 'Auth', *S1, 'PASS' if i <= 3 else 'FAIL')
        for i in range(1, 6)
    ]
    return _run(spec)


class TestP5_SingleSprint:
    """One sprint of data. Engine should not crash and scores must be valid."""

    def test_produces_result(self, p5_result):
        assert p5_result is not None

    def test_sprint_history_has_one_entry(self, p5_result):
        assert len(p5_result['sprint_history']) == 1

    def test_sprint_is_not_adapted(self, p5_result):
        """Sprint 1 is inside warm-up window → is_adapted must be False."""
        assert p5_result['sprint_history'][0]['is_adapted'] is False

    def test_casi_score_valid(self, p5_result):
        s = p5_result['scores']['casi_score']
        assert 0 <= s <= 999

    def test_asi_matches_sprint_asi(self, p5_result):
        """With 1 sprint, headline ASI == that sprint's ASI."""
        assert p5_result['scores']['asi_score'] == p5_result['sprint_history'][0]['asi_score']

    def test_gate_is_valid_string(self, p5_result):
        assert p5_result['scores']['casi_gate'] in ('Green', 'Yellow', 'Red')

    def test_dataset_sprint_count_is_one(self, p5_result):
        assert p5_result['dataset']['sprint_count'] == 1

    def test_weights_delphi_used_within_warmup(self, p5_result):
        """Before warm-up ends, adapted weights should equal Delphi weights."""
        from casi_engine import WEIGHTS_DELPHI_NORM
        adapted = [c['weight_adapted'] for c in p5_result['components'].values()]
        delphi  = WEIGHTS_DELPHI_NORM.tolist()
        for a, d in zip(adapted, delphi):
            assert abs(a - d) < 0.01, f'Within warm-up, adapted({a}) should ≈ delphi({d})'


# ═══════════════════════════════════════════════════════════════════════════════
# P6 — Multi-sprint trend (6 sprints, gradual recovery)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope='module')
def p6_result():
    """
    Sprint 1-3: all fail.  Sprint 4-6: all pass.
    Expect CASI to trend upward; is_adapted flips True after sprint 5.
    """
    all_sprints = [S1, S2, S3, S4, S5, S6]
    spec = []
    for idx, sprint in enumerate(all_sprints):
        status = 'FAIL' if idx < 3 else 'PASS'
        for i in range(1, 6):
            spec.append((f'TC-{i:03d}', 'Auth', *sprint, status))
    return _run(spec)


class TestP6_MultiSprintTrend:
    """6 sprints: bad → good. Engine warms up and adapts weights."""

    def test_six_sprints_in_history(self, p6_result):
        assert len(p6_result['sprint_history']) == 6

    def test_later_sprints_higher_casi_than_early(self, p6_result):
        first_half = [s['casi_score'] for s in p6_result['sprint_history'][:3]]
        second_half = [s['casi_score'] for s in p6_result['sprint_history'][3:]]
        assert np.mean(second_half) > np.mean(first_half), (
            f'Expected CASI to improve: early={first_half}, late={second_half}')

    def test_sprint_6_gate_is_green(self, p6_result):
        assert p6_result['sprint_history'][-1]['casi_gate'] == 'Green'

    def test_sprint_1_gate_is_not_green(self, p6_result):
        assert p6_result['sprint_history'][0]['casi_gate'] != 'Green'

    def test_warm_up_sprints_not_adapted(self, p6_result):
        """Sprints 1-5 are within warm_up=5 → is_adapted must be False."""
        for sprint in p6_result['sprint_history'][:5]:
            assert sprint['is_adapted'] is False, (
                f"Sprint {sprint['sprint_start']} should not be adapted yet")

    def test_sprint_6_is_adapted(self, p6_result):
        assert p6_result['sprint_history'][5]['is_adapted'] is True

    def test_adapted_weights_sum_to_one(self, p6_result):
        """After warm-up, adapted weights must still sum to 1."""
        total = sum(c['weight_adapted'] for c in p6_result['components'].values())
        assert abs(total - 1.0) < 0.01, f'Adapted weights sum to {total}'

    def test_latest_headline_equals_last_sprint(self, p6_result):
        """Headline score must match the last sprint in the history."""
        last = p6_result['sprint_history'][-1]
        assert p6_result['scores']['casi_score'] == last['casi_score']
        assert p6_result['scores']['asi_score']  == last['asi_score']

    def test_last_sprint_has_no_failures(self, p6_result):
        """The most recent sprint (all-pass) should show zero failures."""
        assert p6_result['sprint_history'][-1]['n_fail'] == 0

    def test_diagnostic_not_triggered_after_recovery(self, p6_result):
        assert p6_result['diagnostic_triggered'] is False

    def test_sprint_module_data_all_sprints(self, p6_result):
        for sprint in p6_result['sprint_history']:
            assert 'Auth' in sprint['modules']
            assert sprint['modules']['Auth']['tc_count'] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-scenario: traffic_light boundary values
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrafficLightBoundaries:
    """Verify the gate label boundaries shown in the GateStatusPage banner."""

    @pytest.mark.parametrize('score,expected', [
        (0,   'Red'),
        (399, 'Red'),
        (400, 'Yellow'),
        (699, 'Yellow'),
        (700, 'Green'),
        (999, 'Green'),
    ])
    def test_gate_boundary(self, score, expected):
        assert traffic_light(score) == expected


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-scenario: result schema completeness
# ═══════════════════════════════════════════════════════════════════════════════

class TestResultSchema:
    """Every scenario must produce the exact fields the UI consumes."""

    @pytest.mark.parametrize('fixture_name', [
        'p1_result', 'p2_result', 'p3_result',
        'p4_result', 'p5_result', 'p6_result',
    ])
    def test_top_level_keys(self, request, fixture_name):
        result = request.getfixturevalue(fixture_name)
        for key in ('dataset', 'scores', 'components', 'sprint_history',
                    'open_failures', 'diagnostic_triggered'):
            assert key in result, f'{fixture_name}: missing top-level key "{key}"'

    @pytest.mark.parametrize('fixture_name', [
        'p1_result', 'p2_result', 'p3_result',
        'p4_result', 'p5_result', 'p6_result',
    ])
    def test_scores_keys(self, request, fixture_name):
        result = request.getfixturevalue(fixture_name)
        for key in ('casi_score', 'asi_score', 'casi_gate', 'asi_gate'):
            assert key in result['scores'], f'{fixture_name}: missing scores.{key}'

    @pytest.mark.parametrize('fixture_name', [
        'p1_result', 'p2_result', 'p3_result',
        'p4_result', 'p5_result', 'p6_result',
    ])
    def test_dataset_keys(self, request, fixture_name):
        result = request.getfixturevalue(fixture_name)
        for key in ('tc_count', 'sprint_count', 'modules', 'date_range'):
            assert key in result['dataset'], f'{fixture_name}: missing dataset.{key}'

    @pytest.mark.parametrize('fixture_name', [
        'p1_result', 'p2_result', 'p3_result',
        'p4_result', 'p5_result', 'p6_result',
    ])
    def test_component_keys(self, request, fixture_name):
        result = request.getfixturevalue(fixture_name)
        for k, c in result['components'].items():
            for field in ('label', 'raw', 'normalized', 'weight_delphi', 'weight_adapted'):
                assert field in c, f'{fixture_name}: component {k} missing "{field}"'

    @pytest.mark.parametrize('fixture_name', [
        'p1_result', 'p2_result', 'p3_result',
        'p4_result', 'p5_result', 'p6_result',
    ])
    def test_sprint_entry_keys(self, request, fixture_name):
        result = request.getfixturevalue(fixture_name)
        for sprint in result['sprint_history']:
            for key in ('sprint_start', 'sprint_end', 'casi_score', 'asi_score',
                        'casi_gate', 'asi_gate', 'n_fail', 'is_adapted', 'modules'):
                assert key in sprint, f'{fixture_name}: sprint missing "{key}"'

    @pytest.mark.parametrize('fixture_name', [
        'p2_result', 'p3_result',
    ])
    def test_open_failure_keys(self, request, fixture_name):
        result = request.getfixturevalue(fixture_name)
        for f in result['open_failures']:
            for key in ('tc_id', 'name', 'priority', 'days_open', 'module'):
                assert key in f, f'{fixture_name}: open_failure missing "{key}"'
