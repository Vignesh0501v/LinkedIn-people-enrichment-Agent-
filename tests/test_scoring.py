from app.mapping import FieldMapping
from app.scoring import (
    REVIEW_THRESHOLD,
    VERIFIED_THRESHOLD,
    classify,
    rank_candidates,
    score_candidate,
)
from app.tavily_search import Candidate

MAPPINGS = [
    FieldMapping(standard_field="full_name", source_column="Full Name"),
    FieldMapping(standard_field="company", source_column="Company Name"),
    FieldMapping(standard_field="title", source_column="Title"),
]

ROW = {"Full Name": "Jane Doe", "Company Name": "Acme Corp", "Title": "VP of Sales"}

STRONG_MATCH = Candidate(
    url="https://linkedin.com/in/janedoe",
    title="Jane Doe - VP of Sales at Acme Corp | LinkedIn",
    snippet=(
        "Jane Doe is VP of Sales at Acme Corp, based in New York. "
        "View Jane Doe's profile on LinkedIn."
    ),
)

CLEAR_MISMATCH = Candidate(
    url="https://linkedin.com/in/johnsmith",
    title="John Smith - Software Engineer at Globex Inc | LinkedIn",
    snippet="John Smith works as a Software Engineer at Globex Inc in Chicago.",
)

PARTIAL_MATCH = Candidate(
    url="https://linkedin.com/in/jdoe",
    title="Jane D. - Regional Manager | LinkedIn",
    snippet="Jane D. works in operations at a large technology company in the Midwest.",
)


def test_strong_match_scores_high_and_is_verified():
    score = score_candidate(STRONG_MATCH, ROW, {"name", "company"}, MAPPINGS)

    assert score >= VERIFIED_THRESHOLD
    assert classify(score) == "VERIFIED"


def test_clear_mismatch_scores_low_and_is_not_found():
    score = score_candidate(CLEAR_MISMATCH, ROW, {"name", "company"}, MAPPINGS)

    assert score < REVIEW_THRESHOLD
    assert classify(score) == "NOT_FOUND"


def test_partial_match_lands_in_review_required_band():
    score = score_candidate(PARTIAL_MATCH, ROW, {"name", "company"}, MAPPINGS)

    assert REVIEW_THRESHOLD <= score < VERIFIED_THRESHOLD
    assert classify(score) == "REVIEW_REQUIRED"


def test_company_only_selected_field_still_scores_sensibly():
    strong_company = Candidate(
        url="https://linkedin.com/company/acme",
        title="Acme Corp | LinkedIn",
        snippet="Acme Corp is a leading provider of widgets, based in New York.",
    )
    mismatched_company = Candidate(
        url="https://linkedin.com/company/globex",
        title="Globex Inc | LinkedIn",
        snippet="Globex Inc is a manufacturing company based in Chicago.",
    )

    strong_score = score_candidate(strong_company, ROW, {"company"}, MAPPINGS)
    mismatch_score = score_candidate(mismatched_company, ROW, {"company"}, MAPPINGS)

    assert strong_score > mismatch_score
    assert classify(strong_score) == "VERIFIED"


def test_score_candidate_ignores_fields_outside_selected_fields():
    # Only "company" is selected -- a wildly wrong name in the row should not
    # affect the score at all since "name" isn't part of the search criteria.
    row_with_bad_name = {**ROW, "Full Name": "Someone Else Entirely"}

    strong_company = Candidate(
        url="https://linkedin.com/company/acme",
        title="Acme Corp | LinkedIn",
        snippet="Acme Corp is a leading provider of widgets, based in New York.",
    )

    score = score_candidate(strong_company, row_with_bad_name, {"company"}, MAPPINGS)

    assert score >= VERIFIED_THRESHOLD


def test_rank_candidates_sorts_highest_score_first():
    ranked = rank_candidates(
        [CLEAR_MISMATCH, STRONG_MATCH, PARTIAL_MATCH], ROW, {"name", "company"}, MAPPINGS
    )

    ranked_candidates = [candidate for candidate, _ in ranked]
    scores = [score for _, score in ranked]

    assert ranked_candidates == [STRONG_MATCH, PARTIAL_MATCH, CLEAR_MISMATCH]
    assert scores == sorted(scores, reverse=True)


def test_rank_candidates_empty_list_returns_empty_list():
    ranked = rank_candidates([], ROW, {"name", "company"}, MAPPINGS)

    assert ranked == []
    # No score to classify -- caller treats an empty ranked list as NOT_FOUND
    # directly, per rank_candidates' docstring.


def test_classify_boundaries():
    assert classify(VERIFIED_THRESHOLD) == "VERIFIED"
    assert classify(REVIEW_THRESHOLD) == "REVIEW_REQUIRED"
    assert classify(0.0) == "NOT_FOUND"
    assert classify(1.0) == "VERIFIED"
