from app.normalize import normalize_company, normalize_name


def test_last_first_and_first_last_produce_same_canonical_form():
    from_last_first = normalize_name("Doe, John")
    from_first_last = normalize_name("John Doe")

    assert from_last_first.first_name == from_first_last.first_name == "John"
    assert from_last_first.last_name == from_first_last.last_name == "Doe"
    assert from_last_first.display_name == from_first_last.display_name == "John Doe"


def test_normalize_name_collapses_extra_whitespace():
    result = normalize_name("  John    Doe  ")
    assert result.display_name == "John Doe"


def test_normalize_name_handles_single_token():
    result = normalize_name("Madonna")
    assert result.first_name == "Madonna"
    assert result.last_name == ""


def test_company_suffix_stripping_reconciles_variants():
    with_suffix = normalize_company("Tata Consultancy Services Pvt Ltd")
    without_suffix = normalize_company("Tata Consultancy Services")

    assert with_suffix == without_suffix == "tata consultancy services"


def test_company_normalization_strips_various_legal_suffixes():
    assert normalize_company("Acme Corp") == "acme"
    assert normalize_company("Acme, Inc.") == "acme"
    assert normalize_company("Acme LLC") == "acme"
    assert normalize_company("Acme Corporation") == "acme"
    assert normalize_company("  Acme   Co  ") == "acme"


def test_company_normalization_lowercases_and_trims_whitespace():
    assert normalize_company("  Globex   International  ") == "globex international"
