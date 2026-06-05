"""
CASI — New-format ingestion package (v2).

Pipeline:
    reader      → read_sheets(filepath) → (df_execution, df_variance)
    validator   → validate(df_execution, df_variance) → ValidationResult
    transformer → transform(df_execution, df_variance) → IngestResult

The IngestResult exposes:
  .testcase_runs   list[dict]   — one per TC run row (with effective_status)
  .suite_runs      list[dict]   — one per SUITE_RUN_ID (aggregated)
  .variances       list[dict]   — one per variance row
  .sprints         list[dict]   — one per unique sprint name
  .test_suites     list[dict]   — one per unique suite
  .testcases       list[dict]   — one per unique TC_ID
  .compat_df       pd.DataFrame — CASI-engine-compatible DataFrame
  .accepted_vars   int          — count of variance-applied FAIL→PASS conversions
"""

from .reader import read_sheets, is_new_format
from .validator import validate, ValidationResult, ValidationError
from .transformer import transform, IngestResult

__all__ = [
    'read_sheets',
    'is_new_format',
    'validate',
    'ValidationResult',
    'ValidationError',
    'transform',
    'IngestResult',
]
