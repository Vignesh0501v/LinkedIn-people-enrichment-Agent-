# LinkedIn People Enrichment Agent

A chat-style internal tool that takes a list of names/companies (or a single free-text
request) and enriches each row with a matched LinkedIn profile URL, city, and a
confidence-scored match status — via a **deterministic** search/scoring pipeline plus
two narrowly-scoped, schema-validated LLM calls (never an autonomous search agent).

Full requirements/design history lives in [`doc/`](doc/):
[`BRD_LinkedIn_Enrichment_Pipeline.docx`](doc/BRD_LinkedIn_Enrichment_Pipeline.docx) (business requirements),
[`TRD_LinkedIn_Enrichment_Pipeline.md`](doc/TRD_LinkedIn_Enrichment_Pipeline.md) (technical design),
[`PLAN_LinkedIn_Enrichment_Pipeline.md`](doc/PLAN_LinkedIn_Enrichment_Pipeline.md) (unit-by-unit build log). This README is
the short version.

---

## What it does

You talk to it like a chat app:

- **Upload a spreadsheet** (`.xlsx`) of people/companies to enrich.
- **Paste a table** (tab- or comma-delimited text) instead of uploading a file.
- **Type a one-off request** — `"find Jane Doe at Acme"` — for a single lookup.
- **Just say hi** — greetings and chitchat get a friendly reply, not a pipeline run.

For any real request, the flow is:

1. **Mapping proposal.** The LLM reads your file's columns (or your free text) and
   proposes which column maps to which standard field (`first_name`, `company`,
   `title`, `email`, ...). You review and confirm it in a structured control — the
   model never guesses silently past this point.
2. **Search-criteria selection.** You pick which mapped fields to actually search on.
   The search mode (person vs. company lookup) is *derived* from that selection, not
   a manual toggle: `{"company"}` alone → company-page search; anything else → person
   search.
3. **Search.** Each row is searched against Tavily, restricted to `linkedin.com`, in
   up to two query passes (tight, then broadened).
4. **Score.** Candidates are scored deterministically against your selected criteria.
5. **Verify.** Borderline candidates go to the LLM for a structured judgment
   (`is_match` / `confidence` / `city` / `reason`) — never a first-pass decision-maker,
   only a second opinion on results the deterministic scorer already found.
6. **Result.** Small results (≤10 rows) render as a table right in the chat; larger
   batches (this tool is sized for 200–1,500 rows) get a `.xlsx` download with a
   per-row **Results** sheet and a **Summary** sheet (counts by status, criteria used,
   timestamp).

Every session is saved. A sidebar lists past conversations — reopening one replays
the full history, including resuming an unfinished mapping step or a still-running
batch, not just showing dead text.

---

## Architecture

```mermaid
flowchart TB
    subgraph Browser
        UI["React SPA<br/>chat thread + session sidebar"]
    end

    subgraph Backend["FastAPI backend (app/)"]
        API["api.py<br/>session/turn HTTP contract"]
        INTENT["intent_extraction.py<br/>LangChain + ChatGroq<br/>(job 1: mapping proposal,<br/>greeting/output-format/single-lookup)"]
        MAP["mapping.py<br/>FieldMapping / SearchCriteria<br/>mode derivation"]
        SEARCH["tavily_search.py<br/>2-step, linkedin.com-restricted"]
        SCORE["scoring.py<br/>deterministic candidate scoring"]
        VERIFY["verification.py<br/>ChatGroq (job 2: judge a candidate)"]
        BATCH["batch.py<br/>Batch/Row orchestration,<br/>bounded concurrency"]
    end

    PG[("PostgreSQL<br/>Session / Turn history")]
    REDIS[("Redis<br/>search cache, batch/row<br/>resumability, mapping templates")]
    GROQ[["Groq API<br/>openai/gpt-oss-20b"]]
    TAVILY[["Tavily Search API"]]

    UI -- "HTTP (multipart/JSON)" --> API
    API --> INTENT
    API --> MAP
    API --> BATCH
    BATCH --> SEARCH
    BATCH --> SCORE
    BATCH --> VERIFY
    INTENT -.LangChain ChatGroq.-> GROQ
    VERIFY -.LangChain ChatGroq.-> GROQ
    SEARCH -.HTTPS.-> TAVILY
    API --> PG
    BATCH --> REDIS
```

**Why deterministic, not agentic:** this was a deliberate, explicitly-confirmed
architecture decision (see the TRD's NFR-07). Groq is called for exactly two bounded,
schema-validated jobs — proposing a field mapping and judging a borderline
candidate — and is **never** given a tool to call Tavily or fetch a URL itself. The
model can't decide *when* or *how* to search; it only reasons over inputs the
deterministic pipeline already retrieved and logged. This keeps every accepted match
auditable back to a specific query the code constructed, not something an LLM decided
to go look for.

### Request flow, end to end

```mermaid
sequenceDiagram
    participant U as User (browser)
    participant A as FastAPI (app/api.py)
    participant L as Groq (LangChain)
    participant T as Tavily
    participant P as Postgres
    participant R as Redis

    U->>A: POST /sessions/{id}/turns (file / paste / text)
    A->>P: read last 5 turns (context)
    A->>L: propose mapping (or classify greeting/single-lookup)
    L-->>A: schema-validated JSON (retry once if malformed)
    A->>P: save turn (mapping_proposal)
    A-->>U: proposed mapping + sample rows

    U->>A: PUT .../mapping (confirm)
    U->>A: PUT .../criteria (confirm, kicks off run)
    A-->>U: 200 (batch now "running")

    par per row, bounded concurrency
        A->>T: search (linkedin.com only)
        T-->>A: candidates
        A->>A: deterministic scoring
        alt borderline candidate
            A->>L: verify candidate
            L-->>A: is_match / confidence / city
        end
        A->>R: cache + resumability state
    end

    U->>A: GET .../batches/{id} (poll)
    A-->>U: status, counts

    alt output_format == "table"
        U->>A: GET .../rows
        A-->>U: inline table
    else output_format == "excel"
        U->>A: GET .../download
        A-->>U: .xlsx (Results + Summary sheets)
    end
```

---

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Backend | Python, FastAPI | session/turn-oriented HTTP API, background task execution |
| LLM orchestration | LangChain (`langchain-core` + `langchain-groq`) | wraps 2 bounded Groq calls only — no agent/tool-calling loop |
| LLM | Groq API, `openai/gpt-oss-20b` | free-tier hosted inference; swapped in after Groq fully removed the originally-chosen `llama-3.1-8b-instant` from its catalog |
| Search | Tavily Search API | `include_domains=["linkedin.com"]`, `search_depth: "basic"` (1 credit/request), 2-step query ladder |
| Chat history DB | PostgreSQL (via SQLAlchemy) | `Session`/`Turn` persistence — dedicated DB, separate from Redis |
| Cache / resumability | Redis | search-result cache, batch/row resumability, mapping-template storage |
| Frontend | React + TypeScript (Vite) | chat-thread SPA, session sidebar |
| Excel I/O | `pandas` + `openpyxl` | ingestion and the two-sheet export |
| Containerization | Docker Compose | 4 services: `redis`, `postgres`, `backend`, `frontend` (nginx) |
| Tests | `pytest` | 120+ tests, fully isolated from real Groq/Tavily/Redis/Postgres via dependency-injected fakes |

### Key backend modules

- `app/api.py` — the FastAPI app and full HTTP contract.
- `app/session.py` — Postgres-backed `Session`/`Turn` chat history.
- `app/intent_extraction.py` — LangChain `GroqClient`; proposes field mappings,
  classifies greetings, infers output format, extracts single-lookup field values.
  Prompts live in `app/prompts.json`, not hardcoded strings.
- `app/mapping.py` — `FieldMapping` / `SearchCriteria` / search-mode derivation. The
  single source of truth both the LLM proposal and the deterministic pipeline agree on.
- `app/tavily_search.py` — the Tavily client and two-step query ladder.
- `app/scoring.py` — deterministic candidate scoring against selected criteria.
- `app/verification.py` — the second bounded Groq job (candidate judgment).
- `app/batch.py` — `Batch`/`Row` persistence and `run_batch` orchestration (bounded
  concurrency across Tavily + Groq calls).
- `app/ingest.py` / `app/paste_ingest.py` — `.xlsx` and pasted-text row parsing.
- `app/template_store.py` — reusable mapping templates (Redis).

### Standard fields & match statuses

Every mapping targets one of a fixed field catalog: `first_name`, `middle_name`,
`last_name`, `full_name`, `company`, `title`, `email`. Search criteria are chosen from
a narrower selectable set — `name`, `company`, `title`, `email` (`name` is a logical
grouping over the four name sub-fields).

Each row ends in one of: `VERIFIED`, `REVIEW_REQUIRED`, `NOT_FOUND`,
`MISSING_SEARCH_FIELD` — the pipeline flags uncertainty rather than guessing (a
confident non-match is `NOT_FOUND`; an unresolved/ambiguous one stays
`REVIEW_REQUIRED` for a human, never silently promoted either way).

---

## Required external services (bring your own keys)

Nothing runs without these two — both are third-party API exceptions to an original
"self-hosted only" preference, made deliberately for reliability (SearXNG's search
engines were getting CAPTCHA'd almost immediately from a single IP) and simplicity.

| Variable | Service | Get it from | Used for |
|---|---|---|---|
| `GROQ_API_KEY` | [Groq](https://console.groq.com/) | Groq Console → API Keys (free tier) | Mapping proposals, greeting/output-format classification, and candidate verification (`openai/gpt-oss-20b`) |
| `TAVILY_API_KEY` | [Tavily](https://tavily.com/) | Tavily dashboard (free tier: 1,000 credits/month, no card required) | LinkedIn candidate search |

Both keys are read once at process start via `app/config.py` (which loads `.env`
through `python-dotenv` for local runs; Docker Compose passes them through as real
environment variables instead). **If `GROQ_API_KEY`/`TAVILY_API_KEY` are missing or
invalid, requests that need them fail with a clean `502 Groq API request failed: ...`
or the equivalent Tavily error — they don't crash the server**, but nothing will match
until real keys are set.

### Full `.env` reference

See [`.env.example`](.env.example) for the template. Copy it to `.env` (already
gitignored) and fill in:

```bash
REDIS_URL=redis://localhost:6379
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5433/linkedin_enrichment
GROQ_API_KEY=
TAVILY_API_KEY=
```

- `REDIS_URL` / `DATABASE_URL` — defaults point at `localhost` for running the backend
  directly (`uvicorn`); Docker Compose overrides both to the container service names
  (`redis`, `postgres`) automatically, you don't need to change them for `docker compose up`.
- `DATABASE_URL` can also point at a local file (`sqlite:///./chat.db`) to run with no
  database server at all — handy for a quick check without Docker.
- Postgres's Compose-published host port is **5433**, not 5432 (avoids colliding with
  any other Postgres container already on your machine).

---

## Running it

### Option A — Docker Compose (recommended, matches production)

```bash
cp .env.example .env   # then fill in GROQ_API_KEY and TAVILY_API_KEY
docker compose up --build
```

This starts all four services: `redis`, `postgres`, `backend` (FastAPI on
`:8000`), and `frontend` (nginx-served production build on `:5173`). Open
`http://localhost:5173`.

### Option B — Local dev (hot-reload, no Docker)

```bash
# Backend
pip install -r requirements.txt
docker compose up -d redis postgres    # or point DATABASE_URL at sqlite instead
uvicorn app.api:app --reload --port 8000

# Frontend, in a separate terminal
cd frontend
npm install
npm run dev   # http://localhost:5173, talks to the backend via VITE_API_BASE_URL
```

### Tests

```bash
python -m pytest -q     # backend — 120+ tests, no external services required
cd frontend && npm run build && npm run lint
```

---

## Project status

Every unit through Plan Phase 4 is done and live-verified against a real running
Docker Compose stack — see [`doc/PLAN_LinkedIn_Enrichment_Pipeline.md`](doc/PLAN_LinkedIn_Enrichment_Pipeline.md)
for the full, dated build log, including the specific things that were live-tested
(not just unit-tested) at each stage. Known open items: a company-alias lookup table
(seed source/owner still undecided) and a cleanup pass on some duplicated
Redis-store/retry-logic boilerplate that accumulated across a few modules — both
tracked, neither blocking.
