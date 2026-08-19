import fnmatch
import json

import httpx

from app.batch import BatchStore, _filter_candidates_by_mode, run_batch
from app.intent_extraction import GroqClient
from app.mapping import FieldMapping
from app.tavily_search import Candidate, SearchResult


class FakeRedis:
    """Same in-memory stand-in pattern as tests/test_template_store.py's
    FakeRedis, extended with the hash methods app.batch.BatchStore needs."""

    def __init__(self):
        self._strings: dict[str, bytes] = {}
        self._hashes: dict[str, dict[str, bytes]] = {}

    def set(self, key: str, value: str) -> None:
        self._strings[key] = value.encode("utf-8")

    def get(self, key: str) -> bytes | None:
        return self._strings.get(key)

    def scan_iter(self, match: str | None = None):
        for key in list(self._strings.keys()):
            if match is None or fnmatch.fnmatch(key, match):
                yield key

    def hset(self, key: str, field: str, value: str) -> int:
        bucket = self._hashes.setdefault(key, {})
        is_new = field not in bucket
        bucket[field] = value.encode("utf-8")
        return 1 if is_new else 0

    def hget(self, key: str, field: str) -> bytes | None:
        return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, bytes]:
        return dict(self._hashes.get(key, {}))

    def hlen(self, key: str) -> int:
        return len(self._hashes.get(key, {}))


class FakeTavilyClient:
    """Returns a fixed set of candidates for every row, regardless of the
    query -- enough to test run_batch's own logic (mode filtering,
    concurrency, MISSING_SEARCH_FIELD gating) without a real/mocked HTTP
    client."""

    def __init__(self, candidates: list[Candidate]):
        self._candidates = candidates
        self.call_count = 0

    def find_linkedin_candidates(self, fields: dict[str, str]) -> SearchResult:
        self.call_count += 1
        return SearchResult(candidates=self._candidates, query_used="fake query", reason=None)


PERSON_CANDIDATE = Candidate(
    url="https://linkedin.com/in/janedoe",
    title="Jane Doe - Acme Corp",
    snippet="Jane Doe works at Acme Corp.",
)
COMPANY_CANDIDATE = Candidate(
    url="https://linkedin.com/company/acme",
    title="Acme Corp",
    snippet="Acme Corp is a widget maker.",
)


def test_filter_candidates_by_mode_company_keeps_only_company_urls():
    filtered = _filter_candidates_by_mode([PERSON_CANDIDATE, COMPANY_CANDIDATE], "company")

    assert filtered == [COMPANY_CANDIDATE]


def test_filter_candidates_by_mode_person_keeps_only_in_urls():
    filtered = _filter_candidates_by_mode([PERSON_CANDIDATE, COMPANY_CANDIDATE], "person")

    assert filtered == [PERSON_CANDIDATE]


def test_filter_candidates_by_mode_none_passes_through_unfiltered():
    filtered = _filter_candidates_by_mode([PERSON_CANDIDATE, COMPANY_CANDIDATE], None)

    assert filtered == [PERSON_CANDIDATE, COMPANY_CANDIDATE]


def _setup_batch(redis_client, mappings, selected_fields, search_mode, raw_rows):
    store = BatchStore(client=redis_client)
    batch = store.create_batch(session_id="s1", input_kind="paste", row_count=len(raw_rows))
    store.save_columns(batch.id, list(raw_rows[0].keys()) if raw_rows else [])
    store.save_raw_rows(batch.id, raw_rows)
    store.save_mapping(batch.id, mappings)
    store.save_criteria(batch.id, selected_fields)
    from dataclasses import replace

    batch = replace(batch, search_mode=search_mode)
    store.save_batch(batch)
    return store, batch


def test_run_batch_company_mode_only_accepts_company_urls():
    redis_client = FakeRedis()
    mappings = [FieldMapping(standard_field="company", source_column="Company")]
    store, batch = _setup_batch(
        redis_client, mappings, {"company"}, "company", [{"Company": "Acme"}]
    )
    tavily = FakeTavilyClient([PERSON_CANDIDATE, COMPANY_CANDIDATE])

    run_batch(batch.id, store, tavily)

    rows = store.get_rows(batch.id)
    assert len(rows) == 1
    assert rows[0].linkedin_url == "https://linkedin.com/company/acme"


def test_run_batch_person_mode_only_accepts_in_urls():
    redis_client = FakeRedis()
    mappings = [
        FieldMapping(standard_field="full_name", source_column="Name"),
        FieldMapping(standard_field="company", source_column="Company"),
    ]
    store, batch = _setup_batch(
        redis_client,
        mappings,
        {"name", "company"},
        "person",
        [{"Name": "Jane Doe", "Company": "Acme"}],
    )
    tavily = FakeTavilyClient([PERSON_CANDIDATE, COMPANY_CANDIDATE])

    run_batch(batch.id, store, tavily)

    rows = store.get_rows(batch.id)
    assert len(rows) == 1
    assert rows[0].linkedin_url == "https://linkedin.com/in/janedoe"


def test_run_batch_processes_all_rows_under_bounded_concurrency():
    redis_client = FakeRedis()
    mappings = [FieldMapping(standard_field="company", source_column="Company")]
    raw_rows = [{"Company": f"Company {i}"} for i in range(10)]
    store, batch = _setup_batch(redis_client, mappings, {"company"}, "company", raw_rows)
    tavily = FakeTavilyClient([COMPANY_CANDIDATE])

    run_batch(batch.id, store, tavily)

    rows = store.get_rows(batch.id)
    assert len(rows) == 10
    # Every row landed exactly once, at its own distinct index -- the real
    # point of this test is that bounded concurrency doesn't lose, duplicate,
    # or misattribute a row's outcome; the fixed fake candidate's text won't
    # closely match every row's distinct company name, so exact match_status
    # per row isn't asserted here (that's test_scoring.py's job).
    assert {row.row_index for row in rows} == set(range(10))
    assert all(row.match_status in {"VERIFIED", "REVIEW_REQUIRED", "NOT_FOUND"} for row in rows)
    assert tavily.call_count == 10

    completed_batch = store.get_batch(batch.id)
    assert completed_batch.status == "completed"


def test_run_batch_missing_search_field_never_calls_tavily_for_that_row():
    redis_client = FakeRedis()
    mappings = [FieldMapping(standard_field="company", source_column="Company")]
    raw_rows = [{"Company": "Acme"}, {"Company": ""}]
    store, batch = _setup_batch(redis_client, mappings, {"company"}, "company", raw_rows)
    tavily = FakeTavilyClient([COMPANY_CANDIDATE])

    run_batch(batch.id, store, tavily)

    rows = {row.row_index: row for row in store.get_rows(batch.id)}
    assert rows[0].match_status == "VERIFIED"
    assert rows[1].match_status == "MISSING_SEARCH_FIELD"
    assert tavily.call_count == 1


# --- Verification wiring (unit #19) ---------------------------------------
#
# A candidate that lands in scoring's REVIEW_REQUIRED band (deterministic
# text similarity is ambiguous, not a strong or clear-cut match) -- same
# fixture shape as test_scoring.py's PARTIAL_MATCH, confirmed there to land
# in that band.
_REVIEW_MAPPINGS = [
    FieldMapping(standard_field="full_name", source_column="Name"),
    FieldMapping(standard_field="company", source_column="Company"),
]
_REVIEW_ROW = {"Name": "Jane Doe", "Company": "Acme Corp"}
_REVIEW_CANDIDATE = Candidate(
    url="https://linkedin.com/in/jdoe",
    title="Jane D. - Regional Manager",
    snippet="Jane D. works in operations at a large technology company in the Midwest.",
)


def _groq_client_returning(body: dict) -> GroqClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": json.dumps(body)}}]},
        )

    return GroqClient(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_run_batch_verifies_review_required_and_upgrades_to_verified_with_city():
    redis_client = FakeRedis()
    store, batch = _setup_batch(
        redis_client, _REVIEW_MAPPINGS, {"name", "company"}, "person", [_REVIEW_ROW]
    )
    tavily = FakeTavilyClient([_REVIEW_CANDIDATE])
    groq = _groq_client_returning(
        {"is_match": True, "confidence": 0.9, "city": "Austin", "reason": "Matches on name and company."}
    )

    run_batch(batch.id, store, tavily, groq_client=groq)

    rows = store.get_rows(batch.id)
    assert len(rows) == 1
    assert rows[0].match_status == "VERIFIED"
    assert rows[0].city == "Austin"
    assert "verified by Groq" in rows[0].source_reason


def test_run_batch_verifies_review_required_and_downgrades_to_not_found():
    redis_client = FakeRedis()
    store, batch = _setup_batch(
        redis_client, _REVIEW_MAPPINGS, {"name", "company"}, "person", [_REVIEW_ROW]
    )
    tavily = FakeTavilyClient([_REVIEW_CANDIDATE])
    groq = _groq_client_returning(
        {"is_match": False, "confidence": 0.8, "city": None, "reason": "Different person, same first initial only."}
    )

    run_batch(batch.id, store, tavily, groq_client=groq)

    rows = store.get_rows(batch.id)
    assert len(rows) == 1
    assert rows[0].match_status == "NOT_FOUND"
    assert rows[0].city is None
    assert "rejected by Groq verification" in rows[0].source_reason


def test_run_batch_without_groq_client_leaves_review_required_as_is():
    redis_client = FakeRedis()
    store, batch = _setup_batch(
        redis_client, _REVIEW_MAPPINGS, {"name", "company"}, "person", [_REVIEW_ROW]
    )
    tavily = FakeTavilyClient([_REVIEW_CANDIDATE])

    run_batch(batch.id, store, tavily, groq_client=None)

    rows = store.get_rows(batch.id)
    assert len(rows) == 1
    assert rows[0].match_status == "REVIEW_REQUIRED"
    assert rows[0].city is None


def test_run_batch_unresolved_verification_stays_review_required():
    redis_client = FakeRedis()
    store, batch = _setup_batch(
        redis_client, _REVIEW_MAPPINGS, {"name", "company"}, "person", [_REVIEW_ROW]
    )
    tavily = FakeTavilyClient([_REVIEW_CANDIDATE])

    def malformed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "not valid json"}}]}
        )

    groq = GroqClient(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(malformed_handler))
    )

    run_batch(batch.id, store, tavily, groq_client=groq)

    rows = store.get_rows(batch.id)
    assert len(rows) == 1
    assert rows[0].match_status == "REVIEW_REQUIRED"
    assert "unresolved" in rows[0].source_reason


def test_run_batch_verification_transport_error_does_not_fail_whole_batch():
    redis_client = FakeRedis()
    store, batch = _setup_batch(
        redis_client, _REVIEW_MAPPINGS, {"name", "company"}, "person", [_REVIEW_ROW]
    )
    tavily = FakeTavilyClient([_REVIEW_CANDIDATE])

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    groq = GroqClient(
        api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(failing_handler))
    )

    run_batch(batch.id, store, tavily, groq_client=groq)

    rows = store.get_rows(batch.id)
    assert len(rows) == 1
    assert rows[0].match_status == "REVIEW_REQUIRED"
    assert "verification call failed" in rows[0].source_reason

    completed_batch = store.get_batch(batch.id)
    assert completed_batch.status == "completed"


# --- Unit #25: output_format on the Batch ---


def test_batch_output_format_defaults_to_table_and_round_trips():
    store = BatchStore(client=FakeRedis())

    default_batch = store.create_batch("s1", "paste", 3)
    excel_batch = store.create_batch("s1", "paste", 3, output_format="excel")

    assert default_batch.output_format == "table"
    assert store.get_batch(default_batch.id).output_format == "table"
    assert store.get_batch(excel_batch.id).output_format == "excel"


def test_batch_rejects_an_unknown_output_format():
    import pytest

    store = BatchStore(client=FakeRedis())

    with pytest.raises(ValueError):
        store.create_batch("s1", "paste", 1, output_format="pdf")


def test_batch_written_before_output_format_existed_still_deserializes():
    """Batches persisted before unit #25 have no output_format key -- reading
    one must not blow up mid-session."""
    redis_client = FakeRedis()
    store = BatchStore(client=redis_client)
    batch = store.create_batch("s1", "paste", 1)

    legacy = json.loads(redis_client.get(f"batch:{batch.id}").decode("utf-8"))
    legacy.pop("output_format")
    redis_client.set(f"batch:{batch.id}", json.dumps(legacy))

    assert store.get_batch(batch.id).output_format == "table"
