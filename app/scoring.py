"""Candidate scoring/classification for LinkedIn search results.

See doc/TRD_LinkedIn_Enrichment_Pipeline.md ("Data model" -> `Row.match_confidence`,
`Row.match_status`) and doc/PLAN_LinkedIn_Enrichment_Pipeline.md unit #17 for the
design this module implements.

A candidate (`app.tavily_search.Candidate`) only carries free-text `title` and
`snippet` fields pulled from a search engine result -- there is no structured
"this is the company field" data to compare against. So instead of a naive
substring check, each selected search field's row value is fuzzy-matched
against the candidate's combined title+snippet text using
`difflib.SequenceMatcher`, via a token-level similarity (see
`_field_similarity` below): the field value is split into words and each word
is matched against its best-fitting word in the candidate text, then
averaged. A whole-string `SequenceMatcher(value, blob).ratio()` would unfairly
punish a short, exact field value purely because of its length difference
against a long free-text snippet; a naive character-level "sliding window"
partial ratio overcorrects the other way, since short strings tend to share
enough individual letters with *any* long blob of English prose to score
misleadingly high even when the words themselves are unrelated. Word-level
matching avoids both failure modes.
"""

from difflib import SequenceMatcher

from app.mapping import FieldMapping, value_for_field
from app.normalize import normalize_company, normalize_name
from app.tavily_search import Candidate

# --- Classification thresholds -------------------------------------------
# Deliberately named constants (not inline magic numbers) since the TRD notes
# these need calibration against real labeled data later -- that calibration
# is out of scope for this unit. These starting values are a defensible but
# untuned guess: comfortably high bar for auto-accept, a wide middle band
# routed to Groq verification per the TRD pipeline, and anything below that
# treated as not worth pursuing further.
VERIFIED_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.5

# --- Field weighting -------------------------------------------------------
# Name is the strongest identity signal available -- two different people
# rarely share a full name, so a name mismatch should tank the overall score
# much harder than, say, a title mismatch (job titles are noisy/free-text and
# change often). Company is the next-strongest signal when selected (fewer
# false positives than title/email, but more people share an employer than
# share a name). Title and email are supplementary/corroborating signals:
# useful when they match, but not damning on their own when they don't (a
# snippet may simply not mention the title, or use a different phrasing).
FIELD_WEIGHTS: dict[str, float] = {
    "name": 0.45,
    "company": 0.30,
    "title": 0.15,
    "email": 0.10,
}
# Weight for any selected field not covered above (e.g. a future standard
# field). Kept low/neutral since we have no basis to weight it more heavily.
DEFAULT_FIELD_WEIGHT = 0.10


def _field_similarity(value_text: str, candidate_text: str) -> float:
    """How well `value_text` (a normalized field value) appears in
    `candidate_text` (the candidate's lowercased title+snippet blob).

    An exact substring hit (e.g. a full name or company appearing verbatim
    in the snippet) scores 1.0 outright. Otherwise, each word of
    `value_text` is matched against its single best-fitting word in
    `candidate_text` via `difflib.SequenceMatcher.ratio()`, and the
    per-word scores are averaged -- so e.g. a matching last name with a
    misspelled/abbreviated first name still contributes partial credit,
    while completely unrelated text (different words throughout) stays low.
    """
    if not value_text or not candidate_text:
        return 0.0

    if value_text in candidate_text:
        return 1.0

    value_tokens = value_text.split()
    candidate_tokens = candidate_text.split()
    if not value_tokens or not candidate_tokens:
        return 0.0

    token_scores = [
        max(SequenceMatcher(None, token, candidate_token).ratio() for candidate_token in candidate_tokens)
        for token in value_tokens
    ]
    return sum(token_scores) / len(token_scores)


def _field_text(field: str, row_values: dict[str, str], mappings: list[FieldMapping]) -> str:
    raw = value_for_field(row_values, field, mappings)
    if not raw:
        return ""
    if field == "name":
        return normalize_name(raw).display_name.lower()
    if field == "company":
        return normalize_company(raw)
    return " ".join(raw.strip().lower().split())


def _candidate_text(candidate: Candidate) -> str:
    return f"{candidate.title} {candidate.snippet}".strip().lower()


def score_candidate(
    candidate: Candidate,
    row_values: dict[str, str],
    selected_fields: set[str],
    mappings: list[FieldMapping],
) -> float:
    """Score how well `candidate` matches the row, over `selected_fields` only.

    Fields the row has no value for (missing/blank) contribute a 0.0 for that
    field rather than being skipped -- an unresolvable field can't boost
    confidence, and dropping it from the weighted average would let a
    candidate score artificially high just because the row is sparse.
    """
    if not selected_fields:
        return 0.0

    candidate_text = _candidate_text(candidate)

    weighted_sum = 0.0
    total_weight = 0.0
    for field in selected_fields:
        weight = FIELD_WEIGHTS.get(field, DEFAULT_FIELD_WEIGHT)
        field_text = _field_text(field, row_values, mappings)
        field_score = _field_similarity(field_text, candidate_text) if field_text else 0.0
        weighted_sum += weight * field_score
        total_weight += weight

    if total_weight == 0.0:
        return 0.0
    return weighted_sum / total_weight


def rank_candidates(
    candidates: list[Candidate],
    row_values: dict[str, str],
    selected_fields: set[str],
    mappings: list[FieldMapping],
) -> list[tuple[Candidate, float]]:
    """Score every candidate and return them sorted highest-score-first.

    Returns `[]` for an empty `candidates` list. Callers wanting a
    `match_status` for that case should treat an empty `rank_candidates()`
    result as `"NOT_FOUND"` directly (there is no score to classify) --
    equivalently, `classify(ranked[0][1]) if ranked else "NOT_FOUND"`.
    """
    scored = [
        (candidate, score_candidate(candidate, row_values, selected_fields, mappings))
        for candidate in candidates
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def classify(score: float) -> str:
    """Classify a single score into a `Row.match_status` value.

    Operates on a score, not a candidate list -- for the no-candidates case
    (nothing to score), the caller should short-circuit to `"NOT_FOUND"`
    itself rather than call this with a fabricated score. See
    `rank_candidates` docstring for the recommended pattern.
    """
    if score >= VERIFIED_THRESHOLD:
        return "VERIFIED"
    if score >= REVIEW_THRESHOLD:
        return "REVIEW_REQUIRED"
    return "NOT_FOUND"
