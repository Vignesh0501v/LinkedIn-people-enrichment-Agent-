from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from app.normalize import NormalizedName, normalize_company, normalize_name


@dataclass(frozen=True)
class IngestedRow:
    row_index: int
    name_raw: str
    company_raw: str
    name: NormalizedName
    company_normalized: str
    extra: dict[str, Any]


_NAME_EXACT = {"name", "full name", "contact name"}
_LAST_NAME_EXACT = {"last name", "surname"}
_FIRST_NAME_EXACT = {"first name", "given name"}
_COMPANY_EXACT = {"company", "company name", "organization", "organisation", "employer"}


def _header(column: Any) -> str:
    return str(column).strip().lower()


def _find_exact(columns, candidates: set[str]) -> Any | None:
    for column in columns:
        if _header(column) in candidates:
            return column
    return None


def _find_contains(columns, substring: str, exclude: set = frozenset()) -> Any | None:
    for column in columns:
        if column in exclude:
            continue
        if substring in _header(column):
            return column
    return None


def _resolve_columns(columns) -> tuple[Any, Any | None, Any]:
    """Returns (name_col, last_name_col_or_None, company_col).

    Real batches don't agree on headers: exact "Name"/"Company", combined
    "Full Name" / "Company Name", split "Name" + "Last Name", or a
    domain-specific label like "CSR Head Name" that just means "person's name".
    """
    company_col = _find_exact(columns, _COMPANY_EXACT) or _find_contains(columns, "company")
    if company_col is None:
        raise ValueError(f"no company column found among: {list(columns)}")

    last_name_col = _find_exact(columns, _LAST_NAME_EXACT)
    if last_name_col is not None:
        first_name_col = _find_exact(columns, _FIRST_NAME_EXACT) or _find_exact(columns, {"name"})
        if first_name_col is None:
            raise ValueError(
                f"found a last-name column but no matching first-name column among: {list(columns)}"
            )
        return first_name_col, last_name_col, company_col

    name_col = _find_exact(columns, _NAME_EXACT) or _find_contains(columns, "name", exclude={company_col})
    if name_col is None:
        raise ValueError(f"no name column found among: {list(columns)}")
    return name_col, None, company_col


def read_rows(path: str | Path) -> Iterator[IngestedRow]:
    df = pd.read_excel(path, dtype=str)
    name_col, last_name_col, company_col = _resolve_columns(df.columns)
    used_cols = {name_col, company_col} | ({last_name_col} if last_name_col else set())
    extra_cols = [column for column in df.columns if column not in used_cols]

    for row_index, row in df.iterrows():
        first_or_full_raw = row[name_col]
        company_raw = row[company_col]
        if pd.isna(first_or_full_raw) or pd.isna(company_raw):
            continue
        first_or_full_raw = str(first_or_full_raw).strip()
        company_raw = str(company_raw).strip()

        if last_name_col is not None:
            last_raw = row[last_name_col]
            last_raw = str(last_raw).strip() if pd.notna(last_raw) else ""
            name_raw = f"{first_or_full_raw} {last_raw}".strip()
        else:
            name_raw = first_or_full_raw

        extra: dict[str, Any] = {}
        for column in extra_cols:
            value = row[column]
            if pd.notna(value):
                extra[column] = value

        yield IngestedRow(
            row_index=row_index,
            name_raw=name_raw,
            company_raw=company_raw,
            name=normalize_name(name_raw),
            company_normalized=normalize_company(company_raw),
            extra=extra,
        )
