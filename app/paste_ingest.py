"""Turn pasted (clipboard) tabular text into the row-dict shape app.mapping consumes.

See doc/PLAN_LinkedIn_Enrichment_Pipeline.md unit #14: tab/comma-delimited pasted
text should parse into the same kind of row data (raw column values per row) that
a `.xlsx` upload produces, so it can flow into the field-mapping system
(`app/mapping.py`, `row_values: dict[str, str]`) the same way a file's columns do.

This module deliberately does NOT normalize names/companies -- that's
app.normalize's job, invoked later in the pipeline once fields are mapped.
"""

import csv
import io


def _delimiter_for(first_line: str) -> str:
    """Pasted spreadsheet data is virtually always tab-delimited (what Excel/
    Sheets put on the clipboard). Fall back to comma otherwise (e.g. someone
    pastes a CSV they had open in a text editor)."""
    tab_count = first_line.count("\t")
    comma_count = first_line.count(",")
    return "\t" if tab_count > comma_count else ","


def parse_pasted_table(text: str) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """Parse pasted tab/comma-delimited text into (column_names, rows, warnings).

    - The first non-blank line is treated as the header row.
    - Blank/empty header cells get a placeholder name ("Column 1", "Column 2", ...).
    - Ragged data rows (too few/too many fields vs. the header) are tolerated:
      missing trailing fields become "", extra trailing fields are dropped. Any
      row this happens to is recorded in `warnings` rather than silently fixed up.
    - Empty/whitespace-only input returns ([], [], []).
    """
    if not text or not text.strip():
        return [], [], []

    # Normalize line endings so csv.reader (fed via io.StringIO) doesn't leave
    # stray "\r" characters in field values.
    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = normalized_text.split("\n")
    first_line = next((line for line in lines if line.strip() != ""), "")
    delimiter = _delimiter_for(first_line)

    reader = csv.reader(io.StringIO(normalized_text), delimiter=delimiter)
    # csv.reader yields [] for a blank source line -- drop those (paste artifacts,
    # not data).
    all_rows = [row for row in reader if row]

    if not all_rows:
        return [], [], []

    header_row, data_rows = all_rows[0], all_rows[1:]

    column_names: list[str] = []
    for position, raw_header in enumerate(header_row, start=1):
        header = raw_header.strip()
        column_names.append(header if header else f"Column {position}")

    ncols = len(column_names)
    rows: list[dict[str, str]] = []
    warnings: list[str] = []

    for data_row_index, row in enumerate(data_rows, start=1):
        if len(row) < ncols:
            warnings.append(
                f"row {data_row_index} has {len(row)} field(s), expected {ncols}; "
                "missing trailing field(s) filled with empty string"
            )
            row = row + [""] * (ncols - len(row))
        elif len(row) > ncols:
            warnings.append(
                f"row {data_row_index} has {len(row)} field(s), expected {ncols}; "
                "extra trailing field(s) dropped"
            )
            row = row[:ncols]

        rows.append({column_names[i]: row[i] for i in range(ncols)})

    return column_names, rows, warnings
