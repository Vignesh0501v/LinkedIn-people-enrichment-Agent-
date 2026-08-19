# TRD: LinkedIn Profile & City Enrichment Pipeline

Status: Draft v3.0 — 2026-08-19 (supersedes v2.0)
Based on: `BRD_LinkedIn_Enrichment_Pipeline.docx` (Draft v3.0)

v3.0 changes from v2.0: SearXNG is fully removed (code, container, config) — Tavily is now
the only search backend, not "primary with SearXNG as fallback." The wizard UI is replaced by
a single chat-style interface (upload, paste, or a one-off single lookup, with a persistent
session/history). Groq gains one new, narrowly-scoped job — proposing a structured field
mapping from the user's input — on top of its existing Phase 3 verification role. The
deterministic core (`normalize.py`, `mapping.py`, `tavily_search.py`, scoring, verification)
is explicitly unchanged and was a deliberate decision, not an oversight: an agent that decides
*how and when to search* was considered and rejected, because it reopens the exact ToS/audit
risk the original non-agentic design existed to close (see BRD §13).

## Stack

- **Backend / pipeline:** Python — LangGraph orchestration, `pandas` / `openpyxl` for Excel, plain tab/comma parsing for pasted text.
- **Orchestration:** LangGraph state graph — nodes: ingest (file, paste, or single lookup) → intent extraction (Groq, proposes a `FieldMapping`/`SearchCriteria`) → operator confirmation → normalize → search (Tavily) → deterministic scoring → LLM verification (Groq) → confidence routing → output generation. Non-agentic: Groq is called twice, each time for a narrow, schema-validated task — never given a search tool to call on its own.
- **API layer:** FastAPI — a session/turn-oriented contract (see below) rather than the earlier batch-wizard-only endpoints; runs the pipeline as a background task per batch or single lookup.
- **Frontend:** React SPA, chat-style — one thread per session. Within it: a file-drop / paste-box / free-text input, an inline structured confirmation control for the proposed mapping (not free-text negotiation — precision matters more than chattiness here), progress and results rendered as thread messages, and a scrollable history of past requests/results within the session.
- **Search:** Tavily API — the only search backend. `include_domains=["linkedin.com"]`, two-step query (tight, then broadened). SearXNG has been fully removed from the codebase and infrastructure — there is no fallback path to it anymore.
- **LLM:** Groq API — `openai/gpt-oss-20b` (changed 2026-08-19 from the original `llama-3.1-8b-instant`, which Groq has fully removed from its model catalog), two distinct, bounded jobs (see "Groq's two jobs" below). Never given autonomous search/browse tool access.

## Groq's two jobs (and the boundary around both)

1. **Intent extraction (new in v3.0).** Input: the user's raw instructions text, the detected/pasted column headers, and a few sample row values. Output: a proposed `FieldMapping` set and a proposed `SearchCriteria` selection, as strict JSON validated against the same schema `app/mapping.py`'s `FieldMapping`/`SearchCriteria` types already define. Malformed output is retried, same discipline as job 2 below (FR-08's principle, now applied to both).
2. **Candidate verification (Phase 3, unchanged from v2.0).** Input: a borderline search candidate plus the row's data. Output: `is_match`, `confidence`, `city`, `reason` as strict JSON.

Neither job ever decides *what to search for or when* — job 1 only proposes a mapping the operator still confirms before anything runs; job 2 only judges text already retrieved by the deterministic `tavily_search.py` client. This is the boundary that keeps the system auditable: nothing Groq outputs can cause a search to happen that the deterministic pipeline didn't already construct and log.

## Data model (sketch)

- **Session** — `id`, `created_at`, `last_active_at`. The persistent "context window" — a user's thread of past uploads/pastes/lookups and their results, scrollable within the chat UI.
- **Turn** — `session_id`, `turn_index`, `role` (`user` / `system`), `kind` (`instruction_text` / `file_upload` / `pasted_table` / `mapping_proposal` / `mapping_confirmed` / `batch_result_summary`), `payload` (JSON, shape depends on `kind`).
- **Batch** — `id`, `session_id`, `input_kind` (`file` / `paste` / `single_lookup`), `uploaded_at`, `status` (`pending` / `mapping_proposed` / `mapping_confirmed` / `running` / `completed` / `failed`), `row_count`, `search_mode` (`person` / `company`, derived — unchanged from v2.0).
- **SourceColumn** — `batch_id`, `column_name`, `sample_values`. Populated from file headers, pasted-text headers (first line), or, for a `single_lookup` batch, synthesized from whatever fields the user typed (e.g. "name" and "company" if they wrote "find Jane Doe at Acme").
- **FieldMapping**, **MappingTemplate**, **SearchCriteria**, **Row** — unchanged from v2.0 (see that version's spec below for full field lists). The only difference is *how* a `FieldMapping`/`SearchCriteria` gets proposed — via Groq's intent-extraction job instead of (or in addition to) the type-ahead UI — not what it contains once confirmed.
  - `FieldMapping`: `batch_id`, `standard_field` (`first_name`/`middle_name`/`last_name`/`full_name`/`company`/`title`/`email`), `source_column` (nullable).
  - `MappingTemplate`: `id`, `source_label`, `field_mappings` (standard_field → expected column name). A template match can now also be *offered conversationally* ("this looks like your HR export — reuse that mapping?") instead of only via an explicit "apply template" UI action.
  - `SearchCriteria`: `batch_id`, `selected_fields`. Must be non-empty before a batch can run (FR-06, unchanged).
  - `Row`: `batch_id`, `row_index`, mapped-field values, `extra_fields`, `linkedin_url`, `city`, `match_confidence`, `match_status` (`VERIFIED`/`REVIEW_REQUIRED`/`NOT_FOUND`/`MISSING_SEARCH_FIELD`), `source_reason`, `query_variant_used`.
- **Cache entry** (Redis) — unchanged: keyed on normalized selected-criteria values.
- **Batch progress state** (Redis) — unchanged: per-row completion for resumability.
- **Company alias table** — unchanged, still open per BRD §15.

### Search-criteria & mode derivation — unchanged from v2.0

```
if selected_fields == {"company"}:
    mode = "company"
else:
    mode = "person"
```

## API/contract shape

Session/turn-oriented, replacing v2.0's batch-only endpoints:

- `POST /sessions` — start a session; returns `session_id`.
- `POST /sessions/{id}/turns` — post a user turn: raw instruction text, an uploaded file, or pasted tabular text (`kind` distinguishes which). Returns the detected `SourceColumn` list plus Groq's proposed `FieldMapping`/`SearchCriteria` as a `mapping_proposal` turn — this is where intent extraction runs.
- `PUT /sessions/{id}/batches/{batch_id}/mapping` — the operator's *confirmed* mapping (pre-filled from the proposal, editable via the same structured control the v2.0 wizard used — not re-typed in prose). Same validation as v2.0: can't select a criterion with no mapping.
- `GET /mapping-templates`, `POST /mapping-templates` — unchanged (FR-03).
- `PUT /sessions/{id}/batches/{batch_id}/criteria` — confirmed search criteria; derives and stores `search_mode`; rejects empty selection (FR-06).
- `GET /sessions/{id}/batches/{batch_id}` — status, progress, `search_mode`, summary counts.
- `GET /sessions/{id}/batches/{batch_id}/rows?status=...` — unchanged filter semantics, now including `MISSING_SEARCH_FIELD`.
- `GET /sessions/{id}/batches/{batch_id}/download` — enriched output Excel (single-lookup batches get a one-row version of the same schema, not a different output format).
- `GET /sessions/{id}` — full turn history, for the scrollable context window.

## Auth & access control

- **No auth for Phase 1.** Unchanged.

## External integrations

- **Tavily** — the only search backend. `search_depth: "basic"` (1 credit/request). Two-step per row, domain-restricted to `linkedin.com`. Pilot-verified: 98.6% candidate-discovery rate on the same 71-row sample that scored 5.6% under the removed SearXNG path (discovery rate, not match-accuracy — scoring/verification aren't built yet).
- **Groq API** — `openai/gpt-oss-20b`. Bounded concurrency (3–5 concurrent calls), exponential backoff on 429s, strict JSON schema validation with retry on both the intent-extraction and verification jobs.
- **Redis** — caching, resumability/dedup state, `FieldMapping`/`MappingTemplate`/`SearchCriteria` storage, and now `Session`/`Turn` history.

## Non-functional constraints

- **Scale:** 200–1,500 rows per batch, recurring — BRD NFR-04. Single-lookup requests are the same pipeline at row_count=1, not a separate code path.
- **Concurrency:** bounded (3–5 concurrent calls) against Tavily and Groq — BRD NFR-03.
- **Query cost discipline:** max two Tavily attempts per row — unchanged from v2.0.
- **Resumability:** unchanged, backed by Redis batch/row state.
- **Auditability:** every accepted match logs which query variant, which criteria set, *and* whether the mapping came from Groq's proposal or a manual edit — extends FR-21/NFR-06 to cover the new intent-extraction step.
- **No direct LinkedIn access at any layer, and no autonomous search-tool-calling by any LLM** — the hard architectural constraint from NFR-02 is explicitly extended in v3.0 to name this: Groq is never given a tool that lets it decide when/how to query Tavily or fetch a URL. Its two jobs are call-in-call-out, schema-bounded, never agentic.
- **Third-party API exceptions:** Groq and Tavily remain recorded exceptions to the original self-hosted-only line (BRD §10). SearXNG's removal doesn't change this — it's simply no longer part of the system at all, not "kept as a fallback."

## Definition of Ready

- [x] Stack (Python/LangGraph/FastAPI/React, chat-style) — resolved.
- [x] Data storage (Redis: cache, resumability/dedup, mapping/criteria, and session/turn history) — resolved.
- [x] Auth (none for Phase 1) — resolved.
- [x] LLM integration — two bounded jobs (intent extraction, verification), both schema-validated with retry — resolved.
- [x] Search integration (Tavily only; SearXNG fully removed) — resolved.
- [x] Chat/session data model and API contract — resolved.
- [x] The agentic-vs-deterministic question — resolved: core stays deterministic; LLM reasoning is scoped to intent extraction and verification only, explicitly not search-tool-calling. This was a real fork discussed with the business owner, not a default.
- [x] Every breaking constraint stated explicitly — done.
- [ ] Company alias table seed source/owner — still open, non-blocking.
- [ ] Redis cache TTL policy — still open, non-blocking.

Open items are non-blocking for the current phase of work.
