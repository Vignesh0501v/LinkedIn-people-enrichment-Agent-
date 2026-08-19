from app.paste_ingest import parse_pasted_table


def test_tab_delimited_common_case():
    text = "Name\tCompany\tTitle\nJane Doe\tAcme Inc\tCEO\nJohn Smith\tGlobex Corp\tCTO"

    columns, rows, warnings = parse_pasted_table(text)

    assert columns == ["Name", "Company", "Title"]
    assert rows == [
        {"Name": "Jane Doe", "Company": "Acme Inc", "Title": "CEO"},
        {"Name": "John Smith", "Company": "Globex Corp", "Title": "CTO"},
    ]
    assert warnings == []


def test_comma_delimited_with_quoted_field_containing_comma():
    text = 'Name,Company,Title\n"Doe, Jane",Acme Inc,CEO\nJohn Smith,Globex Corp,CTO'

    columns, rows, warnings = parse_pasted_table(text)

    assert columns == ["Name", "Company", "Title"]
    assert rows[0] == {"Name": "Doe, Jane", "Company": "Acme Inc", "Title": "CEO"}
    assert rows[1] == {"Name": "John Smith", "Company": "Globex Corp", "Title": "CTO"}
    assert warnings == []


def test_blank_header_cell_gets_placeholder_name():
    text = "Name\t\tCompany\nJane Doe\tsomething\tAcme Inc"

    columns, rows, warnings = parse_pasted_table(text)

    assert columns == ["Name", "Column 2", "Company"]
    assert rows == [{"Name": "Jane Doe", "Column 2": "something", "Company": "Acme Inc"}]
    assert warnings == []


def test_ragged_row_with_too_few_fields_is_padded_and_warned():
    text = "Name\tCompany\tTitle\nJane Doe\tAcme Inc"

    columns, rows, warnings = parse_pasted_table(text)

    assert columns == ["Name", "Company", "Title"]
    assert rows == [{"Name": "Jane Doe", "Company": "Acme Inc", "Title": ""}]
    assert len(warnings) == 1
    assert "row 1" in warnings[0]


def test_ragged_row_with_too_many_fields_is_truncated_and_warned():
    text = "Name\tCompany\nJane Doe\tAcme Inc\tExtra Field"

    columns, rows, warnings = parse_pasted_table(text)

    assert columns == ["Name", "Company"]
    assert rows == [{"Name": "Jane Doe", "Company": "Acme Inc"}]
    assert len(warnings) == 1
    assert "row 1" in warnings[0]


def test_ragged_rows_report_correct_row_index_when_mixed_with_good_rows():
    text = "Name\tCompany\nJane Doe\tAcme Inc\nJohn Smith"

    columns, rows, warnings = parse_pasted_table(text)

    assert rows == [
        {"Name": "Jane Doe", "Company": "Acme Inc"},
        {"Name": "John Smith", "Company": ""},
    ]
    assert len(warnings) == 1
    assert "row 2" in warnings[0]


def test_empty_input_returns_empty_results():
    assert parse_pasted_table("") == ([], [], [])


def test_whitespace_only_input_returns_empty_results():
    assert parse_pasted_table("   \n\t\n   ") == ([], [], [])
