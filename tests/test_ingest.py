import pandas as pd

from app.ingest import read_rows


def test_reads_exact_name_and_company_headers(tmp_path):
    path = tmp_path / "exact.xlsx"
    pd.DataFrame([{"Name": "Jane Doe", "Company": "Acme Inc"}]).to_excel(path, index=False)

    rows = list(read_rows(path))

    assert len(rows) == 1
    assert rows[0].name.display_name == "Jane Doe"
    assert rows[0].company_normalized == "acme"


def test_reads_full_name_and_company_name_headers(tmp_path):
    path = tmp_path / "full_name.xlsx"
    pd.DataFrame([{"Full Name": "Jane Doe", "Company Name": "Acme Inc"}]).to_excel(path, index=False)

    rows = list(read_rows(path))

    assert len(rows) == 1
    assert rows[0].name.display_name == "Jane Doe"
    assert rows[0].company_normalized == "acme"


def test_reads_split_first_and_last_name_columns(tmp_path):
    path = tmp_path / "split_name.xlsx"
    pd.DataFrame([{"Name": "Jane", "Last Name": "Doe", "Company": "Acme Inc"}]).to_excel(path, index=False)

    rows = list(read_rows(path))

    assert len(rows) == 1
    assert rows[0].name.first_name == "Jane"
    assert rows[0].name.last_name == "Doe"


def test_reads_domain_specific_name_header(tmp_path):
    path = tmp_path / "domain_specific.xlsx"
    pd.DataFrame([{"CSR Head Name": "Jane Doe", "Company Name": "Acme Inc"}]).to_excel(path, index=False)

    rows = list(read_rows(path))

    assert len(rows) == 1
    assert rows[0].name.display_name == "Jane Doe"


def test_skips_rows_missing_name_or_company(tmp_path):
    path = tmp_path / "sparse.xlsx"
    pd.DataFrame(
        [
            {"Name": "Jane Doe", "Company": "Acme Inc"},
            {"Name": None, "Company": "Acme Inc"},
            {"Name": "John Roe", "Company": None},
        ]
    ).to_excel(path, index=False)

    rows = list(read_rows(path))

    assert len(rows) == 1
    assert rows[0].name.display_name == "Jane Doe"


def test_extra_columns_preserved_and_used_columns_excluded(tmp_path):
    path = tmp_path / "extra.xlsx"
    pd.DataFrame(
        [{"Name": "Jane", "Last Name": "Doe", "Company": "Acme Inc", "Title": "CTO"}]
    ).to_excel(path, index=False)

    rows = list(read_rows(path))

    assert rows[0].extra == {"Title": "CTO"}
