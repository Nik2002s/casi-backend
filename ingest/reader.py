"""
CASI Ingest — Sheet reader.

Reads the two-sheet Excel format:
  Sheet 1: TEST EXECUTION
  Sheet 2: VARIANCE SHEET

Returns raw DataFrames with column names trimmed but otherwise unmodified.
All heavy validation and transformation happens in subsequent stages.
"""

import pandas as pd

EXECUTION_SHEET = 'TEST EXECUTION'
VARIANCE_SHEET  = 'VARIANCE SHEET'


def is_new_format(filepath: str) -> bool:
    """
    Return True if the file contains the new two-sheet format.
    Used for format detection so the old parser is kept as fallback.
    """
    try:
        xl = pd.ExcelFile(filepath, engine='openpyxl')
        return EXECUTION_SHEET in xl.sheet_names
    except Exception:
        return False


def read_sheets(filepath: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Read both sheets from the new-format Excel file.

    Returns:
        (df_execution, df_variance) — raw DataFrames with trimmed column names.

    Raises:
        ValueError if a required sheet is missing.
    """
    xl = pd.ExcelFile(filepath, engine='openpyxl')

    missing = [s for s in (EXECUTION_SHEET, VARIANCE_SHEET) if s not in xl.sheet_names]
    if missing:
        raise ValueError(
            f"Missing required sheet(s): {missing}. "
            f"File contains: {xl.sheet_names}"
        )

    df_exec = xl.parse(EXECUTION_SHEET, dtype=str)
    df_var  = xl.parse(VARIANCE_SHEET,  dtype=str)

    # Trim column names
    df_exec.columns = [str(c).strip() for c in df_exec.columns]
    df_var.columns  = [str(c).strip() for c in df_var.columns]

    # Drop fully-empty rows that Excel sometimes pads at the bottom
    df_exec = df_exec.dropna(how='all').reset_index(drop=True)
    df_var  = df_var.dropna(how='all').reset_index(drop=True)

    return df_exec, df_var
