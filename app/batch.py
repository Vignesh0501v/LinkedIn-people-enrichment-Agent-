"""Batch/Row persistence and batch-execution orchestration.

See doc/TRD_LinkedIn_Enrichment_Pipeline.md ("Data model" -> "Batch", "Row")
for the design this module implements, and
doc/PLAN_LinkedIn_Enrichment_Pipeline.md unit #16 for the integration this
module is part of.

Storage mirrors `app/session.py`/`app/template_store.py`'s pattern: an
injectable Redis client (so tests don't need a live server), plain JSON
blobs, no custom encoding. Key scheme (all scoped by `batch_id`):

- `batch:{batch_id}` -> JSON-encoded `Batch`.
- `batch_columns:{batch_id}` -> JSON list of the batch's source column names
  (as detected from the uploaded file / pasted text / synthesized for a
  single lookup) -- used to validate a confirmed `FieldMapping`'s
  `source_column` actually exists.
- `batch_raw_rows:{batch_id}` -> JSON list of the batch's raw per-row
  `dict[str, str]` (source column name -> raw cell value), i.e. exactly the
  `rows` shape `app.paste_ingest.parse_pasted_table` / the file-upload
  equivalent produce. This is what batch execution reads to build each
  row's search-field values via `app.mapping.value_for_field`.
- `batch_mapping:{batch_id}` -> JSON list of confirmed `FieldMapping` dicts.
- `batch_criteria:{batch_id}` -> JSON `{"selected_fields": [...]}`.
- `batch_rows:{batch_id}` -> a Redis *hash*, field name = `str(row_index)`,
  value = JSON-encoded `Row`. A hash (not a list) is used deliberately so a
  row's outcome can be written independently and out of order as the
  background batch-execution task works through the batch, without needing
  a separate progress counter: "rows completed so far" is simply `hlen` on
  this key, and a row only exists in the hash once its outcome (including
  `MISSING_SEARCH_FIELD`) has been determined -- there is no `PENDING`
  `match_status` written up front. See `count_rows`/`get_rows`.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any

import redis

from app.config import load_config
from app.intent_extraction import GroqClient
from app.mapping import FieldMapping, missing_criteria_fields, value_for_field
from app.scoring import classify, rank_candidates
from app.tavily_search import Candidate, TavilyClient
from app.verification import verify_candidate

BATCH_KEY_PREFIX = "batch:"
COLUMNS_KEY_PREFIX = "batch_columns:"
RAW_ROWS_KEY_PREFIX = "batch_raw_rows:"
MAPPING_KEY_PREFIX = "batch_mapping:"
CRITERIA_KEY_PREFIX = "batch_criteria:"
ROWS_KEY_PREFIX = "batch_rows:"

VALID_INPUT_KINDS: set[str] = {"file", "paste", "single_lookup"}
VALID_BATCH_STATUSES: set[str] = {
    "pending",
    "mapping_proposed",
    "mapping_confirmed",
    "running",
    "completed",
    "failed",
}
VALID_MATCH_STATUSES: set[str] = {
    "VERIFIED",
    "REVIEW_REQUIRED",
    "NOT_FOUND",
    "MISSING_SEARCH_FIELD",
}
# How the operator asked for their results back (unit #25): rendered inline
# in the chat, or offered as an .xlsx download.
VALID_OUTPUT_FORMATS: set[str] = {"table", "excel"}


@dataclass(frozen=True)
class Batch:
    id: str
    session_id: str
    input_kind: str
    uploaded_at: datetime
    status: str
    row_count: int
    search_mode: str | None = None
    output_format: str = "table"


@dataclass(frozen=True)
class Row:
    batch_id: str
    row_index: int
    field_values: dict[str, str]
    linkedin_url: str | None
    city: str | None
    match_confidence: float | None
    match_status: str
    source_reason: str | None
    query_variant_used: str | None


def _batch_key(batch_id: str) -> str:
    return f"{BATCH_KEY_PREFIX}{batch_id}"


def _columns_key(batch_id: str) -> str:
    return f"{COLUMNS_KEY_PREFIX}{batch_id}"


def _raw_rows_key(batch_id: str) -> str:
    return f"{RAW_ROWS_KEY_PREFIX}{batch_id}"


def _mapping_key(batch_id: str) -> str:
    return f"{MAPPING_KEY_PREFIX}{batch_id}"


def _criteria_key(batch_id: str) -> str:
    return f"{CRITERIA_KEY_PREFIX}{batch_id}"


def _rows_key(batch_id: str) -> str:
    return f"{ROWS_KEY_PREFIX}{batch_id}"


def _batch_to_json(batch: Batch) -> str:
    return json.dumps(
        {
            "id": batch.id,
            "session_id": batch.session_id,
            "input_kind": batch.input_kind,
            "uploaded_at": batch.uploaded_at.isoformat(),
            "status": batch.status,
            "row_count": batch.row_count,
            "search_mode": batch.search_mode,
            "output_format": batch.output_format,
        }
    )


def _batch_from_json(raw: str | bytes) -> Batch:
    data = json.loads(raw)
    return Batch(
        id=data["id"],
        session_id=data["session_id"],
        input_kind=data["input_kind"],
        uploaded_at=datetime.fromisoformat(data["uploaded_at"]),
        status=data["status"],
        row_count=data["row_count"],
        search_mode=data.get("search_mode"),
        # Defaulted rather than required: batches written before unit #25
        # have no output_format key and must still deserialize.
        output_format=data.get("output_format") or "table",
    )


def _row_to_json(row: Row) -> str:
    return json.dumps(asdict(row))


def _row_from_json(raw: str | bytes) -> Row:
    data = json.loads(raw)
    return Row(
        batch_id=data["batch_id"],
        row_index=data["row_index"],
        field_values=data["field_values"],
        linkedin_url=data.get("linkedin_url"),
        city=data.get("city"),
        match_confidence=data.get("match_confidence"),
        match_status=data["match_status"],
        source_reason=data.get("source_reason"),
        query_variant_used=data.get("query_variant_used"),
    )


class BatchStore:
    def __init__(self, client: "redis.Redis | None" = None):
        self._client = client if client is not None else redis.Redis.from_url(load_config().redis_url)

    # -- Batch -----------------------------------------------------------

    def create_batch(
        self, session_id: str, input_kind: str, row_count: int, output_format: str = "table"
    ) -> Batch:
        if input_kind not in VALID_INPUT_KINDS:
            raise ValueError(f"invalid input_kind '{input_kind}'; must be one of {sorted(VALID_INPUT_KINDS)}")
        if output_format not in VALID_OUTPUT_FORMATS:
            raise ValueError(
                f"invalid output_format '{output_format}'; must be one of {sorted(VALID_OUTPUT_FORMATS)}"
            )

        import uuid

        batch = Batch(
            id=str(uuid.uuid4()),
            session_id=session_id,
            input_kind=input_kind,
            uploaded_at=datetime.now(timezone.utc),
            status="pending",
            row_count=row_count,
            output_format=output_format,
        )
        self._client.set(_batch_key(batch.id), _batch_to_json(batch))
        return batch

    def get_batch(self, batch_id: str) -> Batch | None:
        raw = self._client.get(_batch_key(batch_id))
        if raw is None:
            return None
        return _batch_from_json(raw)

    def save_batch(self, batch: Batch) -> None:
        if batch.status not in VALID_BATCH_STATUSES:
            raise ValueError(f"invalid status '{batch.status}'; must be one of {sorted(VALID_BATCH_STATUSES)}")
        if batch.output_format not in VALID_OUTPUT_FORMATS:
            raise ValueError(
                f"invalid output_format '{batch.output_format}'; must be one of {sorted(VALID_OUTPUT_FORMATS)}"
            )
        self._client.set(_batch_key(batch.id), _batch_to_json(batch))

    # -- Source columns / raw rows ---------------------------------------

    def save_columns(self, batch_id: str, columns: list[str]) -> None:
        self._client.set(_columns_key(batch_id), json.dumps(columns))

    def get_columns(self, batch_id: str) -> list[str]:
        raw = self._client.get(_columns_key(batch_id))
        if raw is None:
            return []
        return json.loads(raw)

    def save_raw_rows(self, batch_id: str, rows: list[dict[str, str]]) -> None:
        self._client.set(_raw_rows_key(batch_id), json.dumps(rows))

    def get_raw_rows(self, batch_id: str) -> list[dict[str, str]]:
        raw = self._client.get(_raw_rows_key(batch_id))
        if raw is None:
            return []
        return json.loads(raw)

    # -- Mapping / criteria ------------------------------------------------

    def save_mapping(self, batch_id: str, mappings: list[FieldMapping]) -> None:
        payload = [{"standard_field": m.standard_field, "source_column": m.source_column} for m in mappings]
        self._client.set(_mapping_key(batch_id), json.dumps(payload))

    def get_mapping(self, batch_id: str) -> list[FieldMapping] | None:
        raw = self._client.get(_mapping_key(batch_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return [FieldMapping(standard_field=e["standard_field"], source_column=e["source_column"]) for e in data]

    def save_criteria(self, batch_id: str, selected_fields: set[str]) -> None:
        self._client.set(_criteria_key(batch_id), json.dumps(sorted(selected_fields)))

    def get_criteria(self, batch_id: str) -> set[str] | None:
        raw = self._client.get(_criteria_key(batch_id))
        if raw is None:
            return None
        return set(json.loads(raw))

    # -- Rows --------------------------------------------------------------

    def save_row(self, row: Row) -> None:
        if row.match_status not in VALID_MATCH_STATUSES:
            raise ValueError(
                f"invalid match_status '{row.match_status}'; must be one of {sorted(VALID_MATCH_STATUSES)}"
            )
        self._client.hset(_rows_key(row.batch_id), str(row.row_index), _row_to_json(row))

    def get_row(self, batch_id: str, row_index: int) -> Row | None:
        raw = self._client.hget(_rows_key(batch_id), str(row_index))
        if raw is None:
            return None
        return _row_from_json(raw)

    def get_rows(self, batch_id: str, status: str | None = None) -> list[Row]:
        """All rows stored so far for `batch_id`, sorted by `row_index`.

        Only rows whose outcome has already been determined are present
        (see module docstring) -- during a `"running"` batch this is a
        partial, growing list, which is the intended semantics for the
        status/progress endpoint.
        """
        raw_rows = self._client.hgetall(_rows_key(batch_id))
        rows = [_row_from_json(raw) for raw in raw_rows.values()]
        if status is not None:
            rows = [row for row in rows if row.match_status == status]
        rows.sort(key=lambda row: row.row_index)
        return rows

    def count_rows(self, batch_id: str) -> int:
        return self._client.hlen(_rows_key(batch_id))


# Bounded concurrency for Tavily/Groq calls, per the TRD's NFR-03 (3-5
# concurrent calls). The search and verification calls (network I/O, the
# slow part) run in a thread pool; every Redis write happens back on the
# main thread afterward, so there's no concurrent access to
# BatchStore/redis at all -- only the read-only Tavily/Groq calls run in
# parallel.
MAX_CONCURRENT_SEARCHES = 4

# How confident Groq's own verification judgment needs to be to accept a
# borderline candidate as VERIFIED. Separate from scoring.py's
# VERIFIED_THRESHOLD/REVIEW_THRESHOLD (those score deterministic text
# similarity; this scores Groq's self-reported confidence in its yes/no
# judgment) -- named and easy to recalibrate, like the scoring thresholds.
VERIFICATION_CONFIDENCE_THRESHOLD = 0.5


def _search_and_score_row(
    row_index: int,
    row_values: dict[str, str],
    mappings: list[FieldMapping],
    selected_fields: set[str],
    search_mode: str | None,
    tavily_client: TavilyClient,
    groq_client: GroqClient | None,
) -> Row:
    """Build one row's outcome: `MISSING_SEARCH_FIELD` before any search call
    (FR-07), otherwise search, mode-filter, score, and classify -- then, for
    a borderline (`REVIEW_REQUIRED`) top candidate, ask Groq to verify it
    (job 2 from the TRD) and route the final status from that judgment.
    Pure/no I/O side effects beyond the Tavily/Groq calls themselves -- safe
    to run concurrently across rows since nothing here touches shared/
    mutable state."""
    field_values = _display_field_values(row_values, mappings)

    missing = missing_criteria_fields(row_values, selected_fields, mappings)
    if missing:
        return Row(
            batch_id="",  # filled in by the caller, which knows batch_id
            row_index=row_index,
            field_values=field_values,
            linkedin_url=None,
            city=None,
            match_confidence=None,
            match_status="MISSING_SEARCH_FIELD",
            source_reason=f"missing value(s) for: {', '.join(sorted(missing))}",
            query_variant_used=None,
        )

    search_fields = {field: value_for_field(row_values, field, mappings) for field in selected_fields}
    search_result = tavily_client.find_linkedin_candidates(search_fields)
    mode_matched_candidates = _filter_candidates_by_mode(search_result.candidates, search_mode)
    ranked = rank_candidates(mode_matched_candidates, row_values, selected_fields, mappings)

    match_status = classify(ranked[0][1]) if ranked else "NOT_FOUND"
    linkedin_url = ranked[0][0].url if ranked else None
    match_confidence = ranked[0][1] if ranked else None
    city = None
    source_reason = search_result.reason

    if match_status == "REVIEW_REQUIRED" and groq_client is not None:
        top_candidate = ranked[0][0]
        try:
            verification = verify_candidate(
                top_candidate, row_values, selected_fields, mappings, client=groq_client
            )
        except Exception as exc:  # noqa: BLE001 - a transport/HTTP failure on one row
            # shouldn't fail the whole batch (verify_candidate only raises on
            # genuine infra errors, not malformed LLM output -- those already
            # fail safe inside verify_candidate itself). Leave this one row
            # REVIEW_REQUIRED rather than losing the rest of the batch.
            source_reason = f"deterministic score borderline; verification call failed ({exc})"
        else:
            if not verification.resolved:
                # Groq's output stayed malformed after its retry -- we
                # genuinely don't know, so stay REVIEW_REQUIRED for a human
                # rather than guessing either way.
                source_reason = f"deterministic score borderline; verification unresolved ({verification.reason})"
            elif verification.is_match and verification.confidence >= VERIFICATION_CONFIDENCE_THRESHOLD:
                match_status = "VERIFIED"
                city = verification.city
                source_reason = f"verified by Groq (confidence {verification.confidence:.2f}): {verification.reason}"
            else:
                match_status = "NOT_FOUND"
                source_reason = (
                    f"rejected by Groq verification (confidence {verification.confidence:.2f}): {verification.reason}"
                )

    return Row(
        batch_id="",
        row_index=row_index,
        field_values=field_values,
        linkedin_url=linkedin_url,
        city=city,
        match_confidence=match_confidence,
        match_status=match_status,
        source_reason=source_reason,
        query_variant_used=search_result.query_used,
    )


def run_batch(
    batch_id: str,
    batch_store: BatchStore,
    tavily_client: TavilyClient,
    groq_client: GroqClient | None = None,
) -> None:
    """Execute a confirmed batch: for every raw row, either flag it
    `MISSING_SEARCH_FIELD` (before any search call, per FR-07) or search,
    score, and classify it. Tavily/Groq calls run with bounded concurrency
    (`MAX_CONCURRENT_SEARCHES`, per NFR-03) -- row order in the batch has no
    bearing on correctness (each row is independent), and every row's
    outcome is written to Redis sequentially on the main thread once
    computed, so there's no concurrent-write exposure.

    `groq_client` is optional: if `None`, verification is skipped entirely
    and `REVIEW_REQUIRED` rows are left exactly as scoring produced them
    (the pre-unit-19 behavior) -- this keeps `run_batch` usable without a
    Groq dependency (e.g. in a test) rather than forcing one everywhere.
    When provided, borderline candidates get a real verification judgment
    (job 2 from the TRD) and `city` gets populated from it -- verification
    is the only source for `city` in this pipeline.

    Sets `Batch.status` to `"running"` then `"completed"`, or `"failed"` if
    anything throws unexpectedly (the batch's rows processed so far remain
    queryable either way).
    """
    batch = batch_store.get_batch(batch_id)
    if batch is None:
        return

    batch_store.save_batch(replace(batch, status="running"))

    try:
        mappings = batch_store.get_mapping(batch_id) or []
        selected_fields = batch_store.get_criteria(batch_id) or set()
        raw_rows = batch_store.get_raw_rows(batch_id)

        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SEARCHES) as executor:
            futures = [
                executor.submit(
                    _search_and_score_row,
                    row_index,
                    row_values,
                    mappings,
                    selected_fields,
                    batch.search_mode,
                    tavily_client,
                    groq_client,
                )
                for row_index, row_values in enumerate(raw_rows)
            ]
            for future in futures:
                row = future.result()
                batch_store.save_row(replace(row, batch_id=batch_id))

        batch = batch_store.get_batch(batch_id) or batch
        batch_store.save_batch(replace(batch, status="completed"))
    except Exception:
        batch = batch_store.get_batch(batch_id)
        if batch is not None:
            batch_store.save_batch(replace(batch, status="failed"))
        raise


def _filter_candidates_by_mode(candidates: list[Candidate], search_mode: str | None) -> list[Candidate]:
    """Keep only candidates whose URL shape matches the batch's derived mode.

    `search_mode` ("person"/"company", from `derive_search_mode`) previously
    had no effect on search behavior -- `TavilyClient` has no person/company
    branching, so a company-only batch was scored against the exact same
    unfiltered candidate set as a person batch. This filters LinkedIn's own
    URL convention (`/in/...` for people, `/company/...` for companies) so a
    company-mode batch never accepts a personal-profile URL as its match, and
    vice versa. A `None` mode (shouldn't happen for a batch that reached
    `run_batch`, but defensive) passes every candidate through unfiltered.
    """
    if search_mode == "company":
        return [c for c in candidates if "linkedin.com/company/" in c.url.lower()]
    if search_mode == "person":
        return [c for c in candidates if "linkedin.com/in/" in c.url.lower()]
    return candidates


def _display_field_values(row_values: dict[str, str], mappings: list[FieldMapping]) -> dict[str, str]:
    """Every *mapped* standard field's raw value for this row (not just the
    fields selected as search criteria) -- this is what gets stored on the
    `Row` for display/audit, per the TRD's "the mapped-field raw values for
    that row". Search-field values (keyed by criteria names like `"name"`,
    which can combine multiple standard fields) are computed separately in
    `run_batch` for the actual Tavily call.
    """
    values: dict[str, str] = {}
    for mapping in mappings:
        if not mapping.source_column:
            continue
        value = value_for_field(row_values, mapping.standard_field, mappings)
        if value:
            values[mapping.standard_field] = value
    return values
