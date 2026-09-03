"""FastAPI app — the session/turn-oriented HTTP contract from
doc/TRD_LinkedIn_Enrichment_Pipeline.md ("API/contract shape").

This module wires together six already-built modules (`app.session`,
`app.mapping`, `app.template_store`, `app.intent_extraction`, `app.ingest`
[not directly -- see note below], `app.paste_ingest`, `app.tavily_search`,
`app.scoring`) plus the new `app.batch` module into a working HTTP API. See
doc/PLAN_LinkedIn_Enrichment_Pipeline.md unit #16.

Judgment call -- file-upload column/sample-row detection: `app.ingest.read_rows`
is a *normalizing* reader that collapses first/last-name columns into a
single derived name and requires a company column to exist, discarding the
original per-column headers/values this endpoint needs for a generic,
Groq-proposed `FieldMapping` over *all* `STANDARD_FIELDS` (including
`title`/`email`, which `read_rows` has no notion of). So `.xlsx` column/row
detection here reads the workbook directly via `pandas` (the same library
`read_rows` itself uses), exactly mirroring the `(columns, rows)` shape
`app.paste_ingest.parse_pasted_table` already returns for pasted text. This
keeps `app.ingest.py` (owned by another unit) untouched and reserves its
name/company normalization for wherever the pipeline actually needs it --
not column detection. `app.paste_ingest.parse_pasted_table` is used as-is
for the paste case, per the brief.

Judgment call -- background execution + dependency injection: batch
execution is kicked off via FastAPI's `BackgroundTasks`, closing over the
same `BatchStore`/`TavilyClient` instances the app was constructed with (see
`create_app`), so tests can inject fakes/mocks and observe their effects
directly (`TestClient` runs background tasks synchronously before returning
the response, so this doesn't need any extra synchronization in tests).

Judgment call -- single-lookup turns: a turn carrying only free-text
`instructions_text` (no file, no pasted text) is accepted per the TRD's
three input kinds, and produces a `row_count=1` `"single_lookup"` batch --
but since there is no tabular data to detect columns from, its column list
is empty and Groq's mapping proposal will come back with everything
unmapped (nothing to map field values to). Richer single-lookup column
synthesis from free text (e.g. "find Jane Doe at Acme") is
doc/PLAN_LinkedIn_Enrichment_Pipeline.md unit #15, not yet started and out
of this unit's scope -- this endpoint accepts the input shape without
building that synthesis logic.

Phase 4 additions (Plan units #23/#25/#26): `GET /sessions` lists past
sessions for the chat sidebar; the `mapping_proposal` turn now carries the
batch's `columns`/`sample_rows` so a reopened session can rebuild the
mapping editor rather than showing dead text; a turn Groq classifies as a
greeting short-circuits before any Batch exists; each batch records an
`output_format` (`"table"`/`"excel"`); and the Excel export gained a
Summary sheet. Note that the Groq calls now raise `GROQ_TRANSPORT_ERRORS`
(the `groq` SDK's own exception family) rather than `httpx.HTTPError` on
infra failure -- see app/intent_extraction.py's header.
"""

import io
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.batch import Batch, BatchStore, Row, VALID_MATCH_STATUSES, run_batch
from app.intent_extraction import (
    GROQ_TRANSPORT_ERRORS,
    GroqClient,
    MappingProposal,
    extract_single_lookup,
    propose_mapping,
)
from app.mapping import (
    STANDARD_FIELDS,
    FieldMapping,
    MappingTemplate,
    apply_template,
    derive_search_mode,
    validate_criteria,
    value_for_field,
)
from app.paste_ingest import parse_pasted_table
from app.session import SessionStore, Turn
from app.tavily_search import TavilyClient
from app.template_store import TemplateStore

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Unit #25. Above this many rows, an inline chat table stops being readable
# (and stops being scrollable on a phone), so the .xlsx download is forced
# no matter how the user phrased the request -- NFR-04 sizes real batches at
# 200-1,500 rows, so this is the common case, not an edge case. Ten rows is
# roughly one screen of chat.
MAX_INLINE_TABLE_ROWS = 10

# Unit #25. How many prior turns of the session go to Groq as conversational
# context. Bounded deliberately: enough for a short follow-up ("actually
# search at Acme instead") to resolve, small enough that prompt size stays
# flat as a session grows.
CONTEXT_WINDOW_TURNS = 5


class FieldMappingIn(BaseModel):
    standard_field: str
    source_column: str | None = None


class CriteriaIn(BaseModel):
    selected_fields: list[str]


class MappingTemplateIn(BaseModel):
    id: str | None = None
    source_label: str
    field_mappings: dict[str, str]


def _read_excel_bytes(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Detect columns + raw per-row values from an uploaded `.xlsx`, in the
    same `(columns, rows)` shape `parse_pasted_table` returns for text --
    see module docstring for why this reads the workbook directly rather
    than going through `app.ingest.read_rows`.

    Rows that are entirely blank across every column (spacer/trailing rows,
    common in real spreadsheets) are dropped -- `read_rows` had an equivalent
    guard (skipping rows with a blank name/company); reading the workbook
    directly for the generic field-mapping catalog shouldn't mean losing that
    invariant, even though the specific "name and company must be present"
    shape doesn't apply here (any subset of fields can be mapped now)."""
    df = pd.read_excel(io.BytesIO(content), dtype=str)
    df.columns = [str(column) for column in df.columns]
    df = df.fillna("")
    df = df[(df.astype(str).apply(lambda row: row.str.strip()).ne("")).any(axis=1)]
    columns = list(df.columns)
    rows: list[dict[str, str]] = df.astype(str).to_dict(orient="records")
    return columns, rows


_NAME_STANDARD_FIELDS = {"full_name", "first_name", "middle_name", "last_name"}


def _identity_proposal_for_single_lookup(columns: list[str]) -> MappingProposal:
    """Build a mapping proposal for a single_lookup batch directly from its
    (already-standard-field-named) columns -- since `columns` here came from
    `extract_single_lookup_fields`, each one already *is* a `STANDARD_FIELDS`
    name, so the mapping is just the identity (`source_column == standard_field`)
    rather than something Groq needs to propose a second time. Selected
    criteria default to whichever logical fields (name/company/title/email)
    got a value extracted, mirroring how a name/company pair is the common
    default for the file/paste flow's own Groq-proposed criteria."""
    mapped = set(columns)
    field_mappings = [
        FieldMapping(standard_field=field, source_column=field if field in mapped else None)
        for field in STANDARD_FIELDS
    ]
    selected_fields: set[str] = set()
    if mapped & _NAME_STANDARD_FIELDS:
        selected_fields.add("name")
    for logical in ("company", "title", "email"):
        if logical in mapped:
            selected_fields.add(logical)
    return MappingProposal(field_mappings=field_mappings, selected_fields=selected_fields)


def _mapping_to_payload(mappings: list[FieldMapping]) -> list[dict[str, Any]]:
    return [{"standard_field": m.standard_field, "source_column": m.source_column} for m in mappings]


def _row_to_payload(row: Row) -> dict[str, Any]:
    return {
        "batch_id": row.batch_id,
        "row_index": row.row_index,
        "field_values": row.field_values,
        "linkedin_url": row.linkedin_url,
        "city": row.city,
        "match_confidence": row.match_confidence,
        "match_status": row.match_status,
        "source_reason": row.source_reason,
        "query_variant_used": row.query_variant_used,
    }


def _batch_to_payload(batch: Batch) -> dict[str, Any]:
    return {
        "batch_id": batch.id,
        "session_id": batch.session_id,
        "input_kind": batch.input_kind,
        "uploaded_at": batch.uploaded_at.isoformat(),
        "status": batch.status,
        "row_count": batch.row_count,
        "search_mode": batch.search_mode,
        # Unit #25 -- tells the frontend whether to render results inline or
        # only offer the download.
        "output_format": batch.output_format,
    }


def _turn_summary(turn: Turn) -> str:
    """One line of plain text describing a past turn, for the Groq context
    window. Deliberately lossy: the model needs to know what was already
    asked and proposed, not to re-read whole pasted tables or full mapping
    payloads."""
    payload = turn.payload if isinstance(turn.payload, dict) else {}
    if turn.kind == "instruction_text":
        return str(payload.get("text", ""))
    if turn.kind == "greeting_reply":
        return str(payload.get("text", ""))
    if turn.kind == "file_upload":
        return f"[uploaded file {payload.get('filename')} with {payload.get('row_count', 0)} rows]"
    if turn.kind == "pasted_table":
        return f"[pasted a table of {payload.get('row_count', 0)} rows]"
    if turn.kind == "mapping_proposal":
        return f"[proposed criteria fields: {', '.join(payload.get('selected_fields') or []) or 'none'}]"
    if turn.kind == "mapping_confirmed":
        return f"[confirmed criteria fields: {', '.join(payload.get('selected_fields') or []) or 'mapping'}]"
    return f"[{turn.kind}]"


def _context_from_turns(turns: list[Turn]) -> list[dict[str, str]]:
    """The last `CONTEXT_WINDOW_TURNS` turns as `{"role", "content"}` dicts
    for `GroqClient.complete`'s context window. Kept as plain dicts (not
    LangChain messages) so this module stays free of LangChain imports --
    the client owns that conversion."""
    context: list[dict[str, str]] = []
    for turn in turns[-CONTEXT_WINDOW_TURNS:]:
        content = _turn_summary(turn)
        if content:
            context.append({"role": turn.role, "content": content})
    return context


def _looks_like_pasted_table(text: str) -> bool:
    """Cheap, deterministic table/free-text split for the merged input box:
    a real header-plus-row paste always has >=2 columns and >=1 data row
    once run through the same parser the paste flow already uses (reusing
    it rather than a second heuristic, so the two never disagree). A single
    lookup sentence -- however it's phrased or however many lines it wraps
    onto -- never produces that shape."""
    columns, rows, _ = parse_pasted_table(text)
    return len(columns) >= 2 and len(rows) >= 1


def _last_user_data_turn(turns: list[Turn]) -> Turn | None:
    for turn in reversed(turns):
        if turn.role == "user" and turn.kind in ("instruction_text", "file_upload", "pasted_table"):
            return turn
    return None


def _carry_forward_fields(turns: list[Turn]) -> dict[str, str]:
    """Field values extracted from the most recent single-lookup turn, for a
    short follow-up (e.g. "domain expert") to merge against -- but only if
    the conversation hasn't moved on to a file/paste since, which would mean
    a fresh, unrelated request rather than a refinement of the last lookup.
    """
    last = _last_user_data_turn(turns)
    if last is None or last.kind != "instruction_text":
        return {}
    payload = last.payload if isinstance(last.payload, dict) else {}
    fields = payload.get("extracted_fields")
    return dict(fields) if isinstance(fields, dict) else {}


def _is_standalone_lookup(fields: dict[str, str]) -> bool:
    """True when `fields` alone reads as a complete, independent request
    (a name, or a deliberate company-only lookup) rather than a fragment
    that only makes sense appended to whatever was just asked. Used only to
    decide *whether* to attempt a carry-forward merge -- a single stray
    field like a title or email is exactly the case a merge should catch."""
    if _NAME_STANDARD_FIELDS & fields.keys():
        return True
    return set(fields.keys()) == {"company"}


def _fields_are_usable(fields: dict[str, str]) -> bool:
    proposal = _identity_proposal_for_single_lookup(list(fields.keys()))
    return not validate_criteria(proposal.selected_fields, proposal.field_mappings)


def _clarifying_question_for_fields(known: dict[str, str]) -> str:
    if known:
        known_desc = ", ".join(f"{k.replace('_', ' ')} “{v}”" for k, v in known.items())
        return (
            f"I have {known_desc} so far, but that's not quite enough to search LinkedIn "
            "confidently. Could you give me a name, or a company, to search for?"
        )
    return "Who would you like me to look up? A name, and ideally a company, works best."


def _clarifying_question_for_columns(columns: list[str]) -> str:
    column_list = ", ".join(columns) if columns else "(no columns detected)"
    return (
        "I couldn't confidently tell which column holds the person's name or company. "
        f"Your columns are: {column_list}. Which column(s) should I use for name / company / "
        "title / email?"
    )


def _pending_mapping_batch(turns: list[Turn], batch_store: BatchStore) -> Batch | None:
    """The most recently proposed file/paste batch, if it's still waiting on
    a clarifying answer (Groq's proposal didn't confidently map anything to
    search by, so the batch was never auto-confirmed). `None` once that
    batch is resolved or if the most recent proposal was a single_lookup
    one (those are never left half-created -- see `create_turn`)."""
    for turn in reversed(turns):
        if turn.kind != "mapping_proposal":
            continue
        payload = turn.payload if isinstance(turn.payload, dict) else {}
        batch_id = payload.get("batch_id")
        if not batch_id:
            return None
        batch = batch_store.get_batch(batch_id)
        if batch is None or batch.input_kind == "single_lookup":
            return None
        if batch_store.get_criteria(batch_id) is None:
            return batch
        return None
    return None


def _build_summary_frame(
    batch: Batch,
    selected_fields: set[str],
    mapped_fields: list[str],
    rows: list[Row],
    total_rows: int,
) -> pd.DataFrame:
    """The Summary sheet of the export (unit #26): what was run, over how
    much, and how it came out.

    Shaped as a two-column metric/value frame rather than one wide row --
    the counts-by-status set grows with `VALID_MATCH_STATUSES` and an
    operator reads this top-to-bottom, not across.
    """
    counts_by_status: dict[str, int] = {}
    for row in rows:
        counts_by_status[row.match_status] = counts_by_status.get(row.match_status, 0) + 1

    entries: list[tuple[str, Any]] = [
        ("Batch ID", batch.id),
        ("Generated at (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        ("Input kind", batch.input_kind),
        ("Batch status", batch.status),
        ("Search mode", batch.search_mode or ""),
        ("Search criteria fields", ", ".join(sorted(selected_fields))),
        ("Mapped fields", ", ".join(mapped_fields)),
        ("Total rows", total_rows),
        ("Rows processed", len(rows)),
    ]
    # Every known status, including the zeroes -- "0 NOT_FOUND" is itself a
    # result an operator wants to see stated, not inferred from absence.
    entries += [(f"Rows {status}", counts_by_status.get(status, 0)) for status in sorted(VALID_MATCH_STATUSES)]

    return pd.DataFrame(entries, columns=["Metric", "Value"])


def _resolve_output_format(requested: str, row_count: int) -> str:
    """Honour the user's phrasing, except when the result is too big to read
    in a chat bubble -- see `MAX_INLINE_TABLE_ROWS`."""
    if row_count > MAX_INLINE_TABLE_ROWS:
        return "excel"
    return requested if requested in ("table", "excel") else "table"


def _auto_confirm_and_run(
    session_id: str,
    batch: Batch,
    mappings: list[FieldMapping],
    selected_fields: set[str],
    batch_store: BatchStore,
    session_store: SessionStore,
    tavily_client: TavilyClient,
    groq_client: GroqClient,
    background_tasks: BackgroundTasks,
) -> Batch:
    """Confirm mapping + criteria and kick off the run, without an operator
    ever seeing a mapping/criteria editor -- the conversational replacement
    for the old two-step `PUT .../mapping` + `PUT .../criteria` flow. Only
    called once `validate_criteria` has already found nothing wrong with
    `mappings`/`selected_fields`."""
    search_mode = derive_search_mode(selected_fields)
    batch_store.save_mapping(batch.id, mappings)
    batch_store.save_criteria(batch.id, selected_fields)
    batch = replace(batch, status="mapping_confirmed", search_mode=search_mode)
    batch_store.save_batch(batch)

    session_store.add_turn(
        session_id,
        role="system",
        kind="mapping_confirmed",
        payload={
            "batch_id": batch.id,
            "field_mappings": _mapping_to_payload(mappings),
            "selected_fields": sorted(selected_fields),
            "search_mode": search_mode,
            "auto_confirmed": True,
        },
    )
    background_tasks.add_task(run_batch, batch.id, batch_store, tavily_client, groq_client)
    return batch


def create_app(
    session_store: SessionStore | None = None,
    batch_store: BatchStore | None = None,
    template_store: TemplateStore | None = None,
    tavily_client: TavilyClient | None = None,
    groq_client: GroqClient | None = None,
) -> FastAPI:
    """Build the FastAPI app. Every collaborator is injectable so tests can
    pass in-memory/fake stores and mocked HTTP transports instead of hitting
    Postgres/Redis/Tavily/Groq for real -- `SessionStore`'s SQLAlchemy
    `engine` (an in-memory SQLite one in tests), `TemplateStore`/
    `BatchStore`'s Redis client, `TavilyClient`'s `httpx` transport, and
    `GroqClient`'s `http_client`."""
    session_store = session_store if session_store is not None else SessionStore()
    batch_store = batch_store if batch_store is not None else BatchStore()
    template_store = template_store if template_store is not None else TemplateStore()
    tavily_client = tavily_client if tavily_client is not None else TavilyClient()
    # Resolved once, same as tavily_client, rather than left None -- run_batch
    # needs a real client to actually perform verification (unit #19); before
    # this, an unset groq_client meant propose_mapping/extract_single_lookup_fields
    # each opened and closed their own ad-hoc client per call instead of
    # reusing one, which this also cleans up.
    groq_client = groq_client if groq_client is not None else GroqClient()

    app = FastAPI(title="LinkedIn Enrichment Pipeline API")
    # Phase 1 has no auth and the chat frontend (unit #12) is served by a
    # separate dev server (Vite) on a different origin/port -- open CORS is
    # fine here since there's no session/cookie-based auth to leak.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _get_session_or_404(session_id: str):
        session = session_store.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    def _get_batch_or_404(session_id: str, batch_id: str) -> Batch:
        _get_session_or_404(session_id)
        batch = batch_store.get_batch(batch_id)
        if batch is None or batch.session_id != session_id:
            raise HTTPException(status_code=404, detail="batch not found")
        return batch

    @app.post("/sessions")
    def create_session():
        session = session_store.create_session()
        return {"session_id": session.id, "created_at": session.created_at.isoformat()}

    @app.get("/sessions")
    def list_sessions(limit: int | None = None):
        """Every past session, most recently active first, with enough of a
        preview for a sidebar row (unit #23). Deliberately does *not* include
        turn payloads -- `GET /sessions/{id}` is what reopens a conversation."""
        summaries = session_store.list_sessions(limit=limit)
        return {
            "sessions": [
                {
                    "session_id": summary.id,
                    "created_at": summary.created_at.isoformat(),
                    "last_active_at": summary.last_active_at.isoformat(),
                    "first_turn_preview": summary.first_turn_preview,
                    "turn_count": summary.turn_count,
                }
                for summary in summaries
            ]
        }

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str):
        session = _get_session_or_404(session_id)
        turns = session_store.get_turns(session_id)
        return {
            "session_id": session.id,
            "created_at": session.created_at.isoformat(),
            "last_active_at": session.last_active_at.isoformat(),
            "turns": [
                {"turn_index": t.turn_index, "role": t.role, "kind": t.kind, "payload": t.payload} for t in turns
            ],
        }

    @app.post("/sessions/{session_id}/turns")
    async def create_turn(
        session_id: str,
        background_tasks: BackgroundTasks,
        instructions_text: str | None = Form(None),
        pasted_text: str | None = Form(None),
        file: UploadFile | None = File(None),
    ):
        _get_session_or_404(session_id)

        # Unit #25: the session's recent history goes to Groq as
        # conversational context, so a short follow-up resolves against what
        # was already asked. Read before this turn is appended. The full
        # (untrimmed) list is also what the carry-forward/clarification
        # helpers below scan, since they need turns further back than
        # Groq's own context window.
        all_turns = session_store.get_turns(session_id)
        context = _context_from_turns(all_turns)

        turn_kind: str | None = None
        turn_payload: dict[str, Any] | None = None

        if file is not None:
            content = await file.read()
            input_kind = "file"
            columns, raw_rows = _read_excel_bytes(content)
            turn_kind = "file_upload"
            turn_payload = {
                "filename": file.filename,
                "row_count": len(raw_rows),
                "instructions_text": instructions_text,
            }
            requested_output_format = "table"

        elif pasted_text or (instructions_text and _looks_like_pasted_table(instructions_text)):
            # The redesigned input has one free-text box for both a pasted
            # table and a single lookup -- `pasted_text` stays accepted
            # as-is for direct API callers, but text arriving as
            # `instructions_text` that itself parses into >=2 columns and
            # >=1 row is table paste in every way that matters, so it's
            # routed the same way rather than asking the frontend to guess.
            raw_text = pasted_text if pasted_text else instructions_text
            accompanying_text = instructions_text if pasted_text else None
            input_kind = "paste"
            columns, raw_rows, warnings = parse_pasted_table(raw_text or "")
            turn_kind = "pasted_table"
            turn_payload = {
                "row_count": len(raw_rows),
                "warnings": warnings,
                "instructions_text": accompanying_text,
            }
            requested_output_format = "table"

        elif instructions_text:
            # No file/pasted table, so there are no source columns to map --
            # extract_single_lookup pulls the field *values* straight out of
            # the text instead (see app/intent_extraction.py).
            #
            # This is also the only path where greeting detection applies
            # (unit #25): a file upload or pasted table is a data request by
            # construction, whatever text accompanies it.
            #
            # Before treating this as a brand-new request, check whether an
            # earlier file/paste batch in this session is still waiting on a
            # clarifying answer about which column is which -- if so, this
            # text is that answer, not a fresh single lookup.
            pending_batch = _pending_mapping_batch(all_turns, batch_store)
            if pending_batch is not None:
                session_store.add_turn(
                    session_id, role="user", kind="instruction_text", payload={"text": instructions_text}
                )
                pending_columns = batch_store.get_columns(pending_batch.id)
                pending_sample_rows = batch_store.get_raw_rows(pending_batch.id)[:5]
                try:
                    proposal = propose_mapping(
                        instructions_text, pending_columns, pending_sample_rows,
                        client=groq_client, context=context,
                    )
                except GROQ_TRANSPORT_ERRORS as exc:
                    raise HTTPException(status_code=502, detail=f"Groq API request failed: {exc}") from exc

                if not validate_criteria(proposal.selected_fields, proposal.field_mappings):
                    resolved_batch = _auto_confirm_and_run(
                        session_id=session_id,
                        batch=pending_batch,
                        mappings=proposal.field_mappings,
                        selected_fields=proposal.selected_fields,
                        batch_store=batch_store,
                        session_store=session_store,
                        tavily_client=tavily_client,
                        groq_client=groq_client,
                        background_tasks=background_tasks,
                    )
                    return {
                        "intent": "data_request",
                        "batch_id": resolved_batch.id,
                        "status": "running",
                        "columns": pending_columns,
                        "sample_rows": pending_sample_rows,
                        "output_format": resolved_batch.output_format,
                        "field_mappings": _mapping_to_payload(proposal.field_mappings),
                        "selected_fields": sorted(proposal.selected_fields),
                        "search_mode": resolved_batch.search_mode,
                    }

                question = _clarifying_question_for_columns(pending_columns)
                session_store.add_turn(
                    session_id,
                    role="system",
                    kind="clarification_question",
                    payload={"question": question, "batch_id": pending_batch.id},
                )
                return {"intent": "clarification_needed", "batch_id": pending_batch.id, "question": question}

            input_kind = "single_lookup"
            try:
                extraction = extract_single_lookup(instructions_text, client=groq_client, context=context)
            except GROQ_TRANSPORT_ERRORS as exc:
                raise HTTPException(status_code=502, detail=f"Groq API request failed: {exc}") from exc

            if extraction.intent == "greeting":
                # Pure chitchat -- no ingest, no mapping, no Batch at all.
                session_store.add_turn(
                    session_id, role="user", kind="instruction_text", payload={"text": instructions_text}
                )
                reply_turn = session_store.add_turn(
                    session_id, role="system", kind="greeting_reply", payload={"text": extraction.reply}
                )
                return {
                    "intent": "greeting",
                    "batch_id": None,
                    "reply": extraction.reply,
                    "turn_index": reply_turn.turn_index,
                }

            fields = dict(extraction.fields)
            if not _is_standalone_lookup(fields):
                # A fragment like "domain expert" only makes sense appended
                # to whatever was just being searched for -- merge onto the
                # most recent single-lookup turn's fields (its own values
                # win) rather than searching on the fragment alone.
                carried = _carry_forward_fields(all_turns)
                if carried:
                    fields = {**carried, **fields}

            # Recorded regardless of whether this resolves -- both so a
            # reopened session shows what was extracted, and so the *next*
            # turn (however partial) has something to carry forward from.
            session_store.add_turn(
                session_id,
                role="user",
                kind="instruction_text",
                payload={"text": instructions_text, "extracted_fields": fields},
            )

            if not _fields_are_usable(fields):
                question = _clarifying_question_for_fields(fields)
                session_store.add_turn(
                    session_id,
                    role="system",
                    kind="clarification_question",
                    payload={"question": question, "known_fields": fields},
                )
                return {"intent": "clarification_needed", "batch_id": None, "question": question}

            columns = list(fields.keys())
            raw_rows = [fields]
            requested_output_format = extraction.output_format

        else:
            raise HTTPException(
                status_code=422, detail="must provide a file, pasted_text, or instructions_text"
            )

        if turn_kind is not None and turn_payload is not None:
            session_store.add_turn(session_id, role="user", kind=turn_kind, payload=turn_payload)

        sample_rows = raw_rows[:5]
        if input_kind == "single_lookup":
            proposal = _identity_proposal_for_single_lookup(columns)
        else:
            try:
                proposal = propose_mapping(
                    instructions_text or "", columns, sample_rows, client=groq_client, context=context
                )
            except GROQ_TRANSPORT_ERRORS as exc:
                raise HTTPException(status_code=502, detail=f"Groq API request failed: {exc}") from exc
            requested_output_format = proposal.output_format

        output_format = _resolve_output_format(requested_output_format, len(raw_rows))

        batch = batch_store.create_batch(session_id, input_kind, len(raw_rows), output_format=output_format)
        batch_store.save_columns(batch.id, columns)
        batch_store.save_raw_rows(batch.id, raw_rows)

        proposal_payload = {
            "batch_id": batch.id,
            "field_mappings": _mapping_to_payload(proposal.field_mappings),
            "selected_fields": sorted(proposal.selected_fields),
            # Unit #23: carried on the turn itself so reopening a session can
            # rebuild this card straight from history.
            "columns": columns,
            "sample_rows": sample_rows,
            "output_format": output_format,
        }
        session_store.add_turn(session_id, role="system", kind="mapping_proposal", payload=proposal_payload)

        if not validate_criteria(proposal.selected_fields, proposal.field_mappings):
            # Confident enough to run without ever showing a mapping/criteria
            # editor -- the conversational replacement for the old two-step
            # manual confirmation (see `_auto_confirm_and_run`).
            batch = _auto_confirm_and_run(
                session_id=session_id,
                batch=batch,
                mappings=proposal.field_mappings,
                selected_fields=proposal.selected_fields,
                batch_store=batch_store,
                session_store=session_store,
                tavily_client=tavily_client,
                groq_client=groq_client,
                background_tasks=background_tasks,
            )
            return {
                "intent": "data_request",
                "batch_id": batch.id,
                "status": "running",
                "columns": columns,
                "sample_rows": sample_rows,
                "output_format": output_format,
                "field_mappings": _mapping_to_payload(proposal.field_mappings),
                "selected_fields": sorted(proposal.selected_fields),
                "search_mode": batch.search_mode,
            }

        # Not confident enough to auto-run. Ask in-thread instead of showing
        # a manual mapping editor; the next plain-text turn is treated as
        # the answer (see the `_pending_mapping_batch` branch above).
        question = _clarifying_question_for_columns(columns)
        session_store.add_turn(
            session_id,
            role="system",
            kind="clarification_question",
            payload={"question": question, "batch_id": batch.id},
        )
        return {"intent": "clarification_needed", "batch_id": batch.id, "question": question}

    @app.put("/sessions/{session_id}/batches/{batch_id}/mapping")
    def confirm_mapping(session_id: str, batch_id: str, mappings_in: list[FieldMappingIn]):
        _get_batch_or_404(session_id, batch_id)
        columns = batch_store.get_columns(batch_id)

        errors: list[str] = []
        for entry in mappings_in:
            if entry.standard_field not in STANDARD_FIELDS:
                errors.append(f"unknown standard_field '{entry.standard_field}'")
                continue
            if entry.source_column is not None and entry.source_column not in columns:
                errors.append(
                    f"source_column '{entry.source_column}' for field "
                    f"'{entry.standard_field}' is not one of this batch's columns"
                )
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})

        mappings = [
            FieldMapping(standard_field=entry.standard_field, source_column=entry.source_column)
            for entry in mappings_in
        ]
        batch_store.save_mapping(batch_id, mappings)
        session_store.add_turn(
            session_id,
            role="user",
            kind="mapping_confirmed",
            payload={"batch_id": batch_id, "field_mappings": _mapping_to_payload(mappings)},
        )

        return {"batch_id": batch_id, "field_mappings": _mapping_to_payload(mappings)}

    @app.post("/sessions/{session_id}/batches/{batch_id}/mapping/apply-template/{template_id}")
    def apply_mapping_template(session_id: str, batch_id: str, template_id: str):
        """Apply a saved `MappingTemplate` to this batch's actual columns
        (per the TRD's API contract) -- matches by column name, exact string
        only. Fields whose expected column isn't present in this batch stay
        unmapped and are reported back so the operator can fix them by hand;
        this never guesses at a close-but-not-exact match."""
        _get_batch_or_404(session_id, batch_id)
        template = template_store.get(template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="mapping template not found")

        columns = batch_store.get_columns(batch_id)
        mappings, unmatched = apply_template(template, columns)
        batch_store.save_mapping(batch_id, mappings)

        session_store.add_turn(
            session_id,
            role="user",
            kind="mapping_confirmed",
            payload={
                "batch_id": batch_id,
                "field_mappings": _mapping_to_payload(mappings),
                "applied_template_id": template_id,
                "unmatched_fields": unmatched,
            },
        )

        return {
            "batch_id": batch_id,
            "field_mappings": _mapping_to_payload(mappings),
            "unmatched_fields": unmatched,
        }

    @app.get("/mapping-templates")
    def list_mapping_templates():
        templates = template_store.list_all()
        return {
            "templates": [
                {"id": t.id, "source_label": t.source_label, "field_mappings": t.field_mappings} for t in templates
            ]
        }

    @app.post("/mapping-templates")
    def create_mapping_template(template_in: MappingTemplateIn):
        template = MappingTemplate(
            id=template_in.id or str(uuid.uuid4()),
            source_label=template_in.source_label,
            field_mappings=template_in.field_mappings,
        )
        template_store.save(template)
        return {"id": template.id, "source_label": template.source_label, "field_mappings": template.field_mappings}

    @app.put("/sessions/{session_id}/batches/{batch_id}/criteria")
    def confirm_criteria(
        session_id: str, batch_id: str, criteria_in: CriteriaIn, background_tasks: BackgroundTasks
    ):
        batch = _get_batch_or_404(session_id, batch_id)
        mappings = batch_store.get_mapping(batch_id)
        if mappings is None:
            raise HTTPException(
                status_code=422, detail={"errors": ["mapping must be confirmed before criteria"]}
            )

        selected_fields = set(criteria_in.selected_fields)
        errors = validate_criteria(selected_fields, mappings)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})

        search_mode = derive_search_mode(selected_fields)
        batch_store.save_criteria(batch_id, selected_fields)
        batch = replace(batch, status="mapping_confirmed", search_mode=search_mode)
        batch_store.save_batch(batch)

        session_store.add_turn(
            session_id,
            role="user",
            kind="mapping_confirmed",
            payload={"batch_id": batch_id, "selected_fields": sorted(selected_fields), "search_mode": search_mode},
        )

        background_tasks.add_task(run_batch, batch_id, batch_store, tavily_client, groq_client)

        return _batch_to_payload(batch)

    @app.get("/sessions/{session_id}/batches/{batch_id}")
    def get_batch_status(session_id: str, batch_id: str):
        batch = _get_batch_or_404(session_id, batch_id)
        rows = batch_store.get_rows(batch_id)

        counts_by_status: dict[str, int] = {}
        for row in rows:
            counts_by_status[row.match_status] = counts_by_status.get(row.match_status, 0) + 1

        payload = _batch_to_payload(batch)
        payload["rows_completed"] = len(rows)
        payload["counts_by_status"] = counts_by_status
        return payload

    @app.get("/sessions/{session_id}/batches/{batch_id}/rows")
    def list_rows(session_id: str, batch_id: str, status: str | None = None):
        _get_batch_or_404(session_id, batch_id)
        if status is not None and status not in VALID_MATCH_STATUSES:
            raise HTTPException(
                status_code=422,
                detail={"errors": [f"invalid status filter '{status}'; must be one of {sorted(VALID_MATCH_STATUSES)}"]},
            )
        rows = batch_store.get_rows(batch_id, status=status)
        return {"rows": [_row_to_payload(row) for row in rows]}

    @app.get("/sessions/{session_id}/batches/{batch_id}/download")
    def download_batch(session_id: str, batch_id: str):
        batch = _get_batch_or_404(session_id, batch_id)
        mappings = batch_store.get_mapping(batch_id) or []
        # Every *mapped* field, not just the ones selected as search criteria --
        # a field like Title might be mapped for context/audit without being
        # part of the search itself, and should still show up in the export
        # (mirrors app.batch._display_field_values' own "mapped-field raw
        # values" intent, which Row.field_values already captures).
        mapped_fields = sorted(m.standard_field for m in mappings if m.source_column)
        raw_rows = batch_store.get_raw_rows(batch_id)
        rows_by_index = {row.row_index: row for row in batch_store.get_rows(batch_id)}

        records: list[dict[str, Any]] = []
        for row_index, row_values in enumerate(raw_rows):
            row = rows_by_index.get(row_index)
            if row is not None:
                record: dict[str, Any] = {field: row.field_values.get(field, "") for field in mapped_fields}
            else:
                record = {field: value_for_field(row_values, field, mappings) for field in mapped_fields}
            record["linkedin_url"] = row.linkedin_url if row else None
            record["city"] = row.city if row else None
            record["match_confidence"] = row.match_confidence if row else None
            record["match_status"] = row.match_status if row else None
            records.append(record)

        columns = mapped_fields + ["linkedin_url", "city", "match_confidence", "match_status"]
        df = pd.DataFrame.from_records(records, columns=columns)

        summary_df = _build_summary_frame(
            batch=batch,
            selected_fields=batch_store.get_criteria(batch_id) or set(),
            mapped_fields=mapped_fields,
            rows=list(rows_by_index.values()),
            total_rows=len(records),
        )

        buffer = io.BytesIO()
        # Two sheets (unit #26): the per-row export the pipeline has always
        # produced, plus a Summary an operator can read without pivoting the
        # Results sheet by hand. Results stays first so it's the sheet that
        # opens by default.
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Results")
            summary_df.to_excel(writer, index=False, sheet_name="Summary")
        buffer.seek(0)

        return StreamingResponse(
            buffer,
            media_type=XLSX_MEDIA_TYPE,
            headers={"Content-Disposition": f'attachment; filename="batch_{batch_id}.xlsx"'},
        )

    return app


app = create_app()
