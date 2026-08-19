from app.mapping import (
    FieldMapping,
    MappingTemplate,
    apply_template,
    derive_search_mode,
    missing_criteria_fields,
    suggest_mappings,
    validate_criteria,
    value_for_field,
)


def _mapped(mappings: list[FieldMapping]) -> dict[str, str | None]:
    return {mapping.standard_field: mapping.source_column for mapping in mappings}


def test_suggest_mappings_only_trusts_safe_aliases():
    mappings = _mapped(suggest_mappings(["Employer", "Company Name", "Contact", "Email"]))

    # "Employer" is not in the safe alias set -- must stay unmapped.
    assert mappings["company"] == "Company Name"
    # Ambiguous header stays unmapped by suggestion.
    assert mappings["full_name"] is None
    assert mappings["first_name"] is None
    assert mappings["last_name"] is None
    assert mappings["email"] == "Email"


def test_suggest_mappings_ambiguous_contact_header_stays_unmapped():
    mappings = _mapped(suggest_mappings(["Contact", "Company Name"]))

    assert mappings["full_name"] is None
    assert mappings["first_name"] is None
    assert mappings["company"] == "Company Name"


def test_apply_template_reports_matches_and_misses():
    template = MappingTemplate(
        id="tmpl-1",
        source_label="HR weekly export",
        field_mappings={"company": "Company Name", "email": "Work Email"},
    )

    mappings, unmatched = apply_template(template, ["Company Name", "Full Name"])

    mapped = _mapped(mappings)
    assert mapped["company"] == "Company Name"
    assert mapped["email"] is None
    assert unmatched == ["email"]


def test_derive_search_mode_company_only():
    assert derive_search_mode({"company"}) == "company"


def test_derive_search_mode_company_plus_other_is_person():
    assert derive_search_mode({"company", "title"}) == "person"


def test_derive_search_mode_name_only_is_person():
    assert derive_search_mode({"name"}) == "person"


def test_validate_criteria_rejects_empty_selection():
    errors = validate_criteria(set(), [])

    assert errors
    assert any("empty" in error for error in errors)


def test_validate_criteria_rejects_unmapped_selected_field():
    mappings = [FieldMapping(standard_field="company", source_column=None)]

    errors = validate_criteria({"company"}, mappings)

    assert errors
    assert any("company" in error for error in errors)


def test_validate_criteria_passes_when_all_selected_fields_mapped():
    mappings = [
        FieldMapping(standard_field="company", source_column="Company Name"),
        FieldMapping(standard_field="title", source_column="Title"),
    ]

    errors = validate_criteria({"company", "title"}, mappings)

    assert errors == []


def test_validate_criteria_rejects_field_outside_selectable_set():
    # "first_name" is a real STANDARD_FIELDS entry and can be mapped, but it
    # isn't a selectable *criterion* (SELECTABLE_CRITERIA_FIELDS is the
    # logical name/company/title/email vocabulary) -- selecting it directly,
    # bypassing Groq's own proposal, must be rejected rather than silently
    # accepted with degraded fallback behavior downstream.
    mappings = [FieldMapping(standard_field="first_name", source_column="First")]

    errors = validate_criteria({"first_name"}, mappings)

    assert errors
    assert any("first_name" in error for error in errors)


def test_value_for_field_name_prefers_full_name_when_populated():
    mappings = [
        FieldMapping(standard_field="full_name", source_column="Full Name"),
        FieldMapping(standard_field="first_name", source_column="First"),
        FieldMapping(standard_field="last_name", source_column="Last"),
    ]
    row_values = {"Full Name": "Jane Doe", "First": "Jane", "Last": "Doe"}

    assert value_for_field(row_values, "name", mappings) == "Jane Doe"


def test_value_for_field_name_joins_first_and_last_when_full_name_unmapped():
    mappings = [
        FieldMapping(standard_field="first_name", source_column="First"),
        FieldMapping(standard_field="last_name", source_column="Last"),
    ]
    row_values = {"First": "Jane", "Last": "Doe"}

    assert value_for_field(row_values, "name", mappings) == "Jane Doe"


def test_value_for_field_name_empty_when_entirely_absent():
    mappings = [FieldMapping(standard_field="company", source_column="Company Name")]
    row_values = {"Company Name": "Acme"}

    assert value_for_field(row_values, "name", mappings) == ""


def test_missing_criteria_fields_flags_only_the_empty_ones():
    mappings = [
        FieldMapping(standard_field="company", source_column="Company Name"),
        FieldMapping(standard_field="title", source_column="Title"),
        FieldMapping(standard_field="email", source_column="Email"),
    ]
    row_values = {"Company Name": "Acme", "Title": "", "Email": "jane@acme.com"}

    missing = missing_criteria_fields(row_values, {"company", "title", "email"}, mappings)

    assert missing == ["title"]
