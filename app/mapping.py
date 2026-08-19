"""Field-first column mapping and search-criteria logic.

See doc/TRD_LinkedIn_Enrichment_Pipeline.md ("Data model" -> "FieldMapping",
"MappingTemplate", "SearchCriteria", "Search-criteria & mode derivation") for
the validated design this module implements.
"""

from dataclasses import dataclass, field as dataclass_field

# The fixed catalog of standard fields an operator maps their source columns to.
STANDARD_FIELDS: list[str] = [
    "first_name",
    "middle_name",
    "last_name",
    "full_name",
    "company",
    "title",
    "email",
]

# Deliberately narrow: only headers we're fully confident about get auto-suggested.
# Anything ambiguous (including any header containing "name" or "title") is left
# unmapped for the operator to confirm manually -- this mirrors the validated mock.
_ALIASES: dict[str, set[str]] = {
    "company": {"company", "company name"},
    "email": {"email", "email id", "work email"},
}

# The closed set of *logical* fields that can be selected as search criteria
# (see "Search-criteria & mode derivation" in the TRD). "name" is a logical
# grouping over the four name sub-fields in STANDARD_FIELDS, not a standard
# field itself -- see `_field_is_mapped`/`value_for_field` below. This is the
# single source of truth for "what's a valid criterion" -- app.intent_extraction
# and app.scoring both import this rather than hardcoding their own copy.
SELECTABLE_CRITERIA_FIELDS: set[str] = {"name", "company", "title", "email"}


@dataclass(frozen=True)
class FieldMapping:
    standard_field: str
    source_column: str | None = None


@dataclass(frozen=True)
class MappingTemplate:
    id: str
    source_label: str
    field_mappings: dict[str, str] = dataclass_field(default_factory=dict)


def _header(column: str) -> str:
    return column.strip().lower()


def suggest_mappings(source_columns: list[str]) -> list[FieldMapping]:
    """Best-guess FieldMapping rows for every standard field.

    Only exact, case-insensitive matches against a small safe alias set are
    auto-suggested. Everything else comes back unmapped -- never silently
    guessed.
    """
    normalized = {column: _header(column) for column in source_columns}

    mappings: list[FieldMapping] = []
    for standard_field in STANDARD_FIELDS:
        aliases = _ALIASES.get(standard_field, set())
        matched_column: str | None = None
        for column, normalized_header in normalized.items():
            if normalized_header in aliases:
                matched_column = column
                break
        mappings.append(FieldMapping(standard_field=standard_field, source_column=matched_column))
    return mappings


def apply_template(
    template: MappingTemplate, actual_columns: list[str]
) -> tuple[list[FieldMapping], list[str]]:
    """Apply a saved template to a new batch's columns.

    Matching is exact-string only (no fuzzy matching): if a template's
    expected column name isn't present verbatim in `actual_columns`, that
    standard field is reported as unmatched rather than guessed.
    """
    mappings: list[FieldMapping] = []
    unmatched: list[str] = []

    for standard_field, expected_column in template.field_mappings.items():
        if expected_column in actual_columns:
            mappings.append(FieldMapping(standard_field=standard_field, source_column=expected_column))
        else:
            mappings.append(FieldMapping(standard_field=standard_field, source_column=None))
            unmatched.append(standard_field)

    return mappings, unmatched


def derive_search_mode(selected_fields: set[str]) -> str:
    """Company Name alone -> company-page lookup; anything else -> person lookup."""
    if selected_fields == {"company"}:
        return "company"
    return "person"


def _mapping_by_field(mappings: list[FieldMapping]) -> dict[str, str | None]:
    return {mapping.standard_field: mapping.source_column for mapping in mappings}


def _field_is_mapped(field: str, mapping_by_field: dict[str, str | None]) -> bool:
    if field == "name":
        if mapping_by_field.get("full_name"):
            return True
        return any(mapping_by_field.get(sub) for sub in ("first_name", "middle_name", "last_name"))
    return bool(mapping_by_field.get(field))


def validate_criteria(selected_fields: set[str], mappings: list[FieldMapping]) -> list[str]:
    errors: list[str] = []

    if not selected_fields:
        errors.append("selected_fields must not be empty")
        return errors

    unselectable = selected_fields - SELECTABLE_CRITERIA_FIELDS
    for field in sorted(unselectable):
        errors.append(f"'{field}' is not a selectable search criterion; must be one of {sorted(SELECTABLE_CRITERIA_FIELDS)}")

    mapping_by_field = _mapping_by_field(mappings)
    for field in selected_fields & SELECTABLE_CRITERIA_FIELDS:
        if not _field_is_mapped(field, mapping_by_field):
            errors.append(f"selected field '{field}' has no mapping")

    return errors


def value_for_field(row_values: dict[str, str], field: str, mappings: list[FieldMapping]) -> str:
    mapping_by_field = _mapping_by_field(mappings)

    if field == "name":
        full_name_column = mapping_by_field.get("full_name")
        if full_name_column:
            value = row_values.get(full_name_column, "") or ""
            if value:
                return value

        parts = []
        for sub_field in ("first_name", "middle_name", "last_name"):
            column = mapping_by_field.get(sub_field)
            if not column:
                continue
            value = row_values.get(column, "") or ""
            if value:
                parts.append(value)
        return " ".join(parts)

    column = mapping_by_field.get(field)
    if not column:
        return ""
    return row_values.get(column, "") or ""


def missing_criteria_fields(
    row_values: dict[str, str], selected_fields: set[str], mappings: list[FieldMapping]
) -> list[str]:
    return [
        field
        for field in selected_fields
        if not value_for_field(row_values, field, mappings)
    ]
