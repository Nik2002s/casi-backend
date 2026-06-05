"""
CASI Engine — ported from casi_pipeline.py
Computes all 6 CASI components, normalises them, and returns
a complete result object matching the CASI result schema.

Key design choices:
- Dashboard score uses ALL TCs per module (current status), ref_date = dataset end
- Sprint history uses TCs active per sprint, ref_date = sprint_start
- Open failures = all currently failing TCs
- Score formula: 9.99 * dot(weights, normalised_0_to_100) → [0, 999]
"""

import numpy as np
import pandas as pd
from datetime import datetime, date

FAIL_STATES = {'FAIL', 'ERR'}
WEIGHTS_DELPHI = np.array([0.22, 0.18, 0.17, 0.16, 0.14, 0.13])
WEIGHTS_DELPHI_NORM = WEIGHTS_DELPHI / WEIGHTS_DELPHI.sum()

# Human-readable labels for each component — single source of truth.
# The frontend reads these from result.components[k].label instead of
# maintaining its own hardcoded map.
COMPONENT_LABELS = {
    'A': 'Broken Index',
    'B': 'Avg Fix Time',
    'C': 'Downtime',
    'D': 'Fail Ratio',
    'E': 'Suite Fail',
    'F': 'Variances',
}


def is_fail(s):
    return str(s).strip().upper() in FAIL_STATES


def parse_sprints(val):
    if pd.isna(val):
        return []
    result = []
    for part in str(val).split('|'):
        part = part.strip()
        if not part.startswith('Sprint'):
            continue
        dr = part.replace('Sprint', '').strip()
        try:
            s, e = dr.split('-', 1)
            result.append((
                datetime.strptime(s.strip(), '%y.%m.%d').date(),
                datetime.strptime(e.strip(), '%y.%m.%d').date(),
            ))
        except ValueError:
            continue
    return sorted(result, key=lambda x: x[0])


def load_dataset(filepath):
    test_sheets = ['🔐 Login', '🖱 UI Controls', '📝 Forms', '🔗 API', '🔒 Security']
    all_tcs = []
    for name in test_sheets:
        try:
            raw = pd.read_excel(filepath, sheet_name=name, header=None)
        except Exception:
            continue
        hdr = None
        for i, row in raw.iterrows():
            if any('TC ID' == str(v).strip() for v in row.values):
                hdr = i
                break
        if hdr is None:
            continue
        df = pd.read_excel(filepath, sheet_name=name, header=hdr)
        df = df[df.iloc[:, 0].astype(str).str.match(r'TC-')]
        df.columns = [str(c).strip() for c in df.columns]
        df['Sheet'] = name.split(' ', 1)[1] if ' ' in name else name
        all_tcs.append(df)

    if not all_tcs:
        raise ValueError('No test-case sheets found in Excel file.')

    df_all = pd.concat(all_tcs, ignore_index=True)

    sprint_col = next((c for c in df_all.columns if 'Sprint' in c), None)
    if sprint_col:
        df_all['sprints'] = df_all[sprint_col].apply(parse_sprints)
    else:
        df_all['sprints'] = [[] for _ in range(len(df_all))]

    df_all['Status'] = df_all['Status'].astype(str).str.strip().str.upper()
    return df_all


def load_variances(filepath):
    try:
        raw_v = pd.read_excel(filepath, sheet_name='⚠️ Variances', header=None)
        hdr_v = None
        for i, row in raw_v.iterrows():
            if any('Variance ID' == str(v).strip() for v in row.values):
                hdr_v = i
                break
        if hdr_v is None:
            return 0
        df_var = pd.read_excel(filepath, sheet_name='⚠️ Variances', header=hdr_v)
        df_var = df_var[df_var.iloc[:, 0].astype(str).str.match(r'VAR-')]
        vcol = next((c for c in df_var.columns if 'Status' in str(c)), None)
        if vcol is None:
            return 0
        return int((df_var[vcol].astype(str).str.strip() == 'Accepted').sum())
    except Exception:
        return 0


def _compute_components(tcs, ref_date, accepted_vars):
    """
    Core component computation for a slice of test cases.

    ref_date: the date used to compute 'days broken' (sprint_start for
              history entries, dataset end-date for the global view).
    """
    if len(tcs) < 2:
        return None

    st = tcs['Status'].tolist()
    n = len(st)
    fn = sum(is_fail(s) for s in st)

    # A — Broken Index: consecutive pass→fail transitions
    A = sum(
        1 for i in range(1, n) if not is_fail(st[i - 1]) and is_fail(st[i])
    ) / max(n - 1, 1)

    # B — Avg Fix Time: mean days each failing TC has been open
    days_list = []
    for _, row in tcs.iterrows():
        if is_fail(row['Status']) and row['sprints']:
            days_list.append((ref_date - row['sprints'][0][0]).days)
    B = float(np.mean(days_list)) if days_list else 0.0

    # C — Downtime: total failing-days / TC count
    C = sum(days_list) / max(n, 1)

    # D — Failed TC Ratio (%)
    D = fn / n * 100

    # E — Failed Suite Ratio (0 or 100)
    E = 100.0 if fn > 0 else 0.0

    # F — Variances Taken (%): share of failures that have been formally
    # accepted as variances. When there are no failures at all, the metric
    # is undefined — treat it as fully healthy (100) instead of 0, so a
    # clean suite is not penalised as if zero variances had been taken.
    F = (accepted_vars / fn * 100) if fn > 0 else 100.0

    return {'n_tcs': n, 'n_fail': fn,
            'A': A, 'B': B, 'C': C, 'D': D, 'E': E, 'F': F}


# Absolute bounds for each component.
# Using relative min-max (observed range only) causes all scores to collapse
# to 500 whenever sprint data is homogeneous (e.g. same 2 failures every sprint),
# because min == max → the engine defaults to 50 for every component.
# Absolute bounds give each component a meaningful scale independent of whether
# the data varies across sprints.
_COMPONENT_BOUNDS = {
    'A': (0.0, 1.0),      # broken-index ratio  (0 = no transitions, 1 = all transitions)
    'B': (0.0, 180.0),    # avg fix days        (0 = instant fix, 180 d = very stale)
    'C': (0.0, 180.0),    # downtime days / TC  (0 = none, 180 = severe)
    'D': (0.0, 100.0),    # fail ratio %        (0 = all pass, 100 = all fail)
    'E': (0.0, 100.0),    # suite fail flag     (0 = clean suite, 100 = any failure)
    'F': (0.0, 100.0),    # variances accepted% (0 = none accepted, 100 = all)
}


def _normalize(records):
    """Normalise components to 0-100 health (higher = healthier).

    Uses absolute per-component bounds instead of observed min-max so that
    scores stay meaningful even when all records have identical values (e.g.
    the same 2 failures appear in every sprint).  Relative min-max collapses
    to 50 in that case; absolute bounds reflect the true severity of the raw
    component values.

    Values are clipped to [0, 100] after scaling, so components that exceed
    the bound ceiling (e.g. a failure open longer than 180 d) are floored at
    0 health rather than going negative.
    """
    if not records:
        return records
    df = pd.DataFrame(records)

    for comp in ['A', 'B', 'C', 'D', 'E']:   # lower raw value → better health → invert
        lo, hi = _COMPONENT_BOUNDS[comp]
        vals = df[comp].values.astype(float)
        df[f'{comp}_norm'] = np.clip((hi - vals) / (hi - lo) * 100, 0, 100)

    for comp in ['F']:                          # higher raw value → better health
        lo, hi = _COMPONENT_BOUNDS[comp]
        vals = df[comp].values.astype(float)
        df[f'{comp}_norm'] = np.clip((vals - lo) / (hi - lo) * 100, 0, 100)

    return df.to_dict('records')


def _score(rec, weights):
    """CASI score in [0, 999] from a normalised record + weight vector."""
    v = np.array([rec['A_norm'], rec['B_norm'], rec['C_norm'],
                  rec['D_norm'], rec['E_norm'], rec['F_norm']])
    return float(np.clip(9.99 * np.dot(weights, v), 0, 999))


def traffic_light(score):
    if score >= 700:
        return 'Green'
    if score >= 400:
        return 'Yellow'
    return 'Red'


def run_casi_from_df(df_all, accepted_vars=0):
    """
    Compute the full CASI result object from a pre-loaded DataFrame.

    df_all must have columns: TC ID (first col), Status, Sheet, sprints
    (list of (date, date) tuples — already parsed).

    accepted_vars: count of accepted variances from the Variances sheet.
    """
    from adaptive_weights import AdaptiveWeightEngine

    sheets = df_all['Sheet'].unique().tolist()

    # All unique sprint start dates
    all_sprints = sorted({
        s[0] for sprints in df_all['sprints'] for s in sprints
    })
    if not all_sprints:
        raise ValueError('No sprint dates found in the dataset.')

    # Dataset end-date: max sprint end across all TCs
    all_ends = [
        s[1] for sprints in df_all['sprints'] for s in sprints
    ]
    ref_date = max(all_ends) if all_ends else all_sprints[-1]

    # ── Sprint history (per sprint × module, then averaged across modules) ──
    sprint_raw = []
    for sprint_start in all_sprints:
        for sheet in sheets:
            tcs = df_all[
                (df_all['Sheet'] == sheet) &
                (df_all['sprints'].apply(lambda sl: any(s[0] == sprint_start for s in sl)))
            ]
            rec = _compute_components(tcs, sprint_start, accepted_vars)
            if rec:
                rec['sprint_start'] = sprint_start
                rec['sheet'] = sheet
                sprint_raw.append(rec)

    if not sprint_raw:
        raise ValueError('No valid sprint × module data found.')

    sprint_norm = _normalize(sprint_raw)

    # Feed sprints to adaptive engine in chronological order.
    # IMPORTANT: update() is called ONCE PER SPRINT (not once per sprint×module)
    # so that warm_up=5 means "5 sprint columns" as intended.  Previously it was
    # called once per record, meaning 5 modules × 1 sprint burned through the
    # entire warm-up in the very first sprint column.
    engine = AdaptiveWeightEngine()

    # Group normalised records by sprint for the per-sprint weight update
    sprint_norm_by_start = {}
    for rec in sprint_norm:
        sprint_norm_by_start.setdefault(rec['sprint_start'], []).append(rec)

    for sprint_start in all_sprints:
        recs = sprint_norm_by_start.get(sprint_start, [])
        if not recs:
            continue
        # Average component health across modules → single signal for this sprint
        avg_comps = [
            float(np.mean([r[f'{c}_norm'] for r in recs]))
            for c in ['A', 'B', 'C', 'D', 'E', 'F']
        ]
        total_fails = sum(r['n_fail'] for r in recs)
        adapted_w = engine.update(avg_comps, total_fails)
        # Apply the same adapted weights to every module in this sprint
        for rec in recs:
            rec['asi_score'] = round(_score(rec, WEIGHTS_DELPHI_NORM))
            rec['casi_score'] = round(_score(rec, adapted_w))
            rec['weights_adapted'] = adapted_w.tolist()

    sprint_history = []
    for sprint_start in all_sprints:
        recs = [r for r in sprint_norm if r['sprint_start'] == sprint_start]
        if not recs:
            continue
        # Sprint end from the dataset
        sprint_end = next(
            (s[1] for row_sprints in df_all['sprints'] for s in row_sprints
             if s[0] == sprint_start),
            None,
        )
        avg_asi = float(np.mean([r['asi_score'] for r in recs]))
        avg_casi = float(np.mean([r['casi_score'] for r in recs]))
        # Build per-module score dict for heatmap
        sprint_idx_1based = all_sprints.index(sprint_start) + 1
        is_adapted = sprint_idx_1based > engine.warm_up

        modules_dict = {
            r['sheet']: {
                'casi':            round(r['casi_score']),
                'asi':             round(r['asi_score']),
                'tc_count':        int(r.get('n_tcs', 0) or 0),
                'n_fail':          int(r.get('n_fail', 0) or 0),
                'is_adapted':      is_adapted,
                'weights_adapted': r.get('weights_adapted'),   # list[6] or None
                'norm_scores': {   # normalised 0-100 health per component
                    c: round(float(r.get(f'{c}_norm', 50)), 1)
                    for c in ['A', 'B', 'C', 'D', 'E', 'F']
                },
            }
            for r in recs
        }
        sprint_history.append({
            'sprint_start': sprint_start.isoformat(),
            'sprint_end':   sprint_end.isoformat() if sprint_end else None,
            'asi_score':    round(avg_asi),
            'casi_score':   round(avg_casi),
            'asi_gate':     traffic_light(avg_asi),
            'casi_gate':    traffic_light(avg_casi),
            'n_fail':       sum(r['n_fail'] for r in recs),
            'is_adapted':   is_adapted,
            'modules':      modules_dict,
        })

    # ── Current-status score — one row per TC at its latest-sprint status ──────
    # Deduplicate df_all to one row per TC (keep the row from the latest sprint)
    # so that a TC failing in sprint N but not re-run in sprint N+1 still
    # penalises the headline score.  This is also shared with open_failures below.
    final_adapted_w = engine.weights

    latest_rows: dict = {}   # tc_id → (sprint_start, row)
    for _, row in df_all.iterrows():
        tc_id    = str(row.iloc[0])
        sp_start = row['sprints'][0][0] if row['sprints'] else None
        prev     = latest_rows.get(tc_id)
        if prev is None or (sp_start and (prev[0] is None or sp_start > prev[0])):
            latest_rows[tc_id] = (sp_start, row)

    df_current = (
        pd.DataFrame([row for _, row in latest_rows.values()]).reset_index(drop=True)
        if latest_rows else df_all.copy()
    )

    current_raw = []
    for sheet in sheets:
        tcs = df_current[df_current['Sheet'] == sheet]
        rec = _compute_components(tcs, ref_date, accepted_vars)
        if rec:
            rec['sheet'] = sheet
            current_raw.append(rec)

    current_norm = _normalize(current_raw) if current_raw else []
    for rec in current_norm:
        rec['asi_score']  = round(_score(rec, WEIGHTS_DELPHI_NORM))
        rec['casi_score'] = round(_score(rec, final_adapted_w))

    def avg_current(key):
        return float(np.mean([r[key] for r in current_norm])) if current_norm else 50.0

    last_norm = {k: avg_current(k) for k in ['A_norm', 'B_norm', 'C_norm', 'D_norm', 'E_norm', 'F_norm']}
    last_raw  = {c: avg_current(c) for c in ['A', 'B', 'C', 'D', 'E', 'F']}

    # ── Headline scores ────────────────────────────────────────────────────────
    # Derived from the current-status (latest-row-per-TC) view so that:
    #   (a) every TC at its latest status contributes, even if not re-run recently
    #   (b) a score of 999 is impossible while any TC has an open FAIL/ERR
    if current_norm:
        casi_score = round(float(np.mean([r['casi_score'] for r in current_norm])))
        asi_score  = round(float(np.mean([r['asi_score']  for r in current_norm])))
    elif sprint_history:
        casi_score = sprint_history[-1]['casi_score']
        asi_score  = sprint_history[-1]['asi_score']
    else:
        casi_score = round(_score(last_norm, final_adapted_w))
        asi_score  = round(_score(last_norm, WEIGHTS_DELPHI_NORM))

    # ── Open failures (TCs whose LATEST-sprint status is FAIL/ERR) ────────────
    # Each TC can appear in multiple rows (one per sprint it participated in).
    # We must only look at the LATEST sprint row per TC to determine current
    # status — a TC that was FAIL in an old sprint but PASS in a newer sprint
    # is NOT an open failure.
    name_col = next(
        (c for c in df_all.columns
         if c.lower() in ['name', 'test name', 'tc name', 'title',
                          'description', 'test case name']),
        None,
    )
    priority_col = next(
        (c for c in df_all.columns if 'priority' in c.lower()), None
    )

    # Reuse the dedup dict already built for the headline score
    latest_row_for_tc = latest_rows

    open_failures = []
    for tc_id, (_, row) in latest_row_for_tc.items():
        if not is_fail(row['Status']):
            continue
        name         = str(row[name_col]) if name_col else tc_id
        first_sprint = row['sprints'][0][0] if row['sprints'] else ref_date
        days_open    = (ref_date - first_sprint).days
        if priority_col:
            priority = str(row[priority_col])
        elif days_open > 30:
            priority = 'Critical'
        elif days_open > 7:
            priority = 'High'
        else:
            priority = 'Medium'
        open_failures.append({
            'tc_id': tc_id,
            'name': name,
            'priority': priority,
            'days_open': days_open,
            'module': row['Sheet'],
        })
    open_failures.sort(key=lambda x: x['days_open'], reverse=True)

    # Date range
    min_date = min(all_sprints).isoformat()
    max_date = ref_date.isoformat()

    return {
        'dataset': {
            'tc_count': int(df_all.iloc[:, 0].nunique()),
            'sprint_count': len(all_sprints),
            'modules': sheets,
            'date_range': f'{min_date} to {max_date}',
        },
        'components': {
            k: {
                'label': COMPONENT_LABELS[k],
                'raw': round(last_raw[k], 4 if k == 'A' else 1),
                'normalized': round(last_norm[f'{k}_norm'], 1),
                'weight_delphi': round(float(WEIGHTS_DELPHI_NORM[i]), 3),
                'weight_adapted': round(float(final_adapted_w[i]), 3),
            }
            for i, k in enumerate(['A', 'B', 'C', 'D', 'E', 'F'])
        },
        'scores': {
            'asi_score': asi_score,
            'casi_score': casi_score,
            'asi_gate': traffic_light(asi_score),
            'casi_gate': traffic_light(casi_score),
        },
        'sprint_history': sprint_history,
        'open_failures': open_failures,
        'diagnostic_triggered': casi_score < 700,
    }


def run_casi(filepath):
    """Compute the full CASI result object from an Excel test-suite file."""
    df_all = load_dataset(filepath)
    accepted_vars = load_variances(filepath)
    return run_casi_from_df(df_all, accepted_vars)
