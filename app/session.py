"""Session/Turn model and Postgres-backed persistence.

See doc/TRD_LinkedIn_Enrichment_Pipeline.md ("Data model" -> "Session",
"Turn") for the design this module implements: a `Session` is a user's
persistent thread (the chat UI's "context window"); each `Turn` within it is
one entry in that thread (an instruction, an upload, a mapping proposal, a
confirmation, a result summary, ...).

Why this one store moved off Redis (Plan unit #23, decision #2 of Phase 4):
chat history is the only *durable, queryable, relational* data in this
system -- it needs "list every session, newest first, with a preview", it
outlives any single batch run, and losing it is a user-visible data loss
rather than a cache miss. Redis's JSON-blob-per-key shape made the session
list an O(all-keys) scan and gave no ordering. Everything else Redis does
here (search-result caching, batch/row resumability, mapping templates) is
genuinely ephemeral or key-addressed and stays on Redis unchanged.

Storage shape is deliberately thin -- two SQLAlchemy tables and explicit
queries, no repository/unit-of-work layer -- matching the rest of this
codebase (plain dataclasses at the boundary, direct store calls). The
dataclasses below, not the ORM rows, remain the public type: nothing outside
this module ever sees a SQLAlchemy object.

- `sessions` -> one row per session.
- `turns` -> one row per turn, PK `(session_id, turn_index)`, `payload` as
  JSON (`JSONB` on Postgres, plain `JSON` on SQLite so unit tests can run
  in-memory without a server -- same "tests never need live infra" rule the
  Redis stores follow with their injectable client).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    String,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import load_config

# Per the TRD's Turn definition.
VALID_ROLES: set[str] = {"user", "system"}
VALID_KINDS: set[str] = {
    "instruction_text",
    "file_upload",
    "pasted_table",
    "mapping_proposal",
    "mapping_confirmed",
    "batch_result_summary",
    # Unit #25: a conversational reply to a turn Groq classified as a
    # greeting -- no batch, no mapping, nothing to confirm.
    "greeting_reply",
}

# How much of the first user turn to keep for a sidebar row label. Long
# enough to distinguish two sessions at a glance, short enough not to ship
# whole pasted tables in a list response.
PREVIEW_MAX_CHARS = 120


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class TurnRow(Base):
    __tablename__ = "turns"

    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    turn_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # JSONB on Postgres (indexable, stored parsed); plain JSON elsewhere so
    # the same models work against SQLite in tests.
    payload: Mapped[Any] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=True)


@dataclass(frozen=True)
class Session:
    id: str
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True)
class Turn:
    session_id: str
    turn_index: int
    role: str
    kind: str
    payload: Any


@dataclass(frozen=True)
class SessionSummary:
    """One sidebar row: enough to label and order a past session without
    fetching its full turn history (unit #23's `GET /sessions`)."""

    id: str
    created_at: datetime
    last_active_at: datetime
    first_turn_preview: str
    turn_count: int


def create_session_engine(url: str | None = None) -> Engine:
    """Build the `Engine` this store runs on.

    Defaults to `DATABASE_URL` from config (Postgres in Docker, per
    docker-compose.yml). In-memory SQLite gets `StaticPool` +
    `check_same_thread=False` so every connection sees the same database
    even across threads -- which the FastAPI `TestClient`'s background tasks
    need, and which is the only reason this helper exists rather than a bare
    `create_engine` call at each site.
    """
    url = url if url is not None else load_config().database_url
    if url.startswith("sqlite") and (":memory:" in url or url in ("sqlite://", "sqlite+pysqlite://")):
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return create_engine(url)


def _to_session(row: SessionRow) -> Session:
    return Session(
        id=row.id,
        created_at=_as_utc(row.created_at),
        last_active_at=_as_utc(row.last_active_at),
    )


def _as_utc(value: datetime) -> datetime:
    """SQLite round-trips `DateTime(timezone=True)` as a naive datetime;
    Postgres keeps the offset. Normalize so callers always get an aware UTC
    datetime regardless of backend."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_turn(row: TurnRow) -> Turn:
    return Turn(
        session_id=row.session_id,
        turn_index=row.turn_index,
        role=row.role,
        kind=row.kind,
        payload=row.payload,
    )


def _preview_text(turn: Turn | None) -> str:
    """A short human label for a session, derived from its first turn --
    whatever the user actually typed/uploaded/pasted, since that's what they
    recognize a past conversation by."""
    if turn is None:
        return ""
    payload = turn.payload if isinstance(turn.payload, dict) else {}
    text = payload.get("text") or payload.get("instructions_text") or payload.get("filename")
    if not text and turn.kind == "pasted_table":
        text = f"Pasted table ({payload.get('row_count', 0)} rows)"
    text = str(text or "").strip()
    if len(text) > PREVIEW_MAX_CHARS:
        return text[: PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return text


class SessionStore:
    def __init__(self, engine: Engine | None = None):
        """`engine` is the injection seam (replacing the pre-unit-#23 Redis
        `client` param): tests pass an in-memory SQLite engine so no live
        database is needed, exactly as the Redis-backed stores accept a fake
        client."""
        self._engine = engine if engine is not None else create_session_engine()
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
        self._schema_ready = False

    def _ensure_schema(self) -> None:
        """Create the two tables if they don't exist yet.

        Deliberately lazy rather than run in `__init__`: `app.api` builds a
        default `SessionStore()` at module import, and connecting to
        Postgres at import time would make the module unimportable without a
        running database (tests inject their own store and never touch the
        default). Alembic is overkill at this project's maturity -- there is
        exactly one schema version and no production data to migrate.
        """
        if not self._schema_ready:
            Base.metadata.create_all(self._engine)
            self._schema_ready = True

    def create_session(self) -> Session:
        self._ensure_schema()
        now = datetime.now(timezone.utc)
        session = Session(id=str(uuid.uuid4()), created_at=now, last_active_at=now)
        with self._session_factory() as db:
            db.add(SessionRow(id=session.id, created_at=now, last_active_at=now))
            db.commit()
        return session

    def get_session(self, session_id: str) -> Session | None:
        self._ensure_schema()
        with self._session_factory() as db:
            row = db.get(SessionRow, session_id)
            return _to_session(row) if row is not None else None

    def list_sessions(self, limit: int | None = None) -> list[SessionSummary]:
        """All sessions, most recently active first, each with a preview of
        its first user turn -- what a sidebar needs to render a clickable
        list without fetching every session's full history."""
        self._ensure_schema()
        with self._session_factory() as db:
            stmt = select(SessionRow).order_by(SessionRow.last_active_at.desc())
            if limit is not None:
                stmt = stmt.limit(limit)
            session_rows = list(db.scalars(stmt))
            if not session_rows:
                return []

            session_ids = [row.id for row in session_rows]
            # One extra query for all the first turns, and one for the
            # counts -- not one pair per session.
            first_turns = {
                turn_row.session_id: _to_turn(turn_row)
                for turn_row in db.scalars(
                    select(TurnRow).where(TurnRow.session_id.in_(session_ids), TurnRow.turn_index == 0)
                )
            }
            counts = dict(
                db.execute(
                    select(TurnRow.session_id, func.count())
                    .where(TurnRow.session_id.in_(session_ids))
                    .group_by(TurnRow.session_id)
                ).all()
            )

            return [
                SessionSummary(
                    id=row.id,
                    created_at=_as_utc(row.created_at),
                    last_active_at=_as_utc(row.last_active_at),
                    first_turn_preview=_preview_text(first_turns.get(row.id)),
                    turn_count=int(counts.get(row.id, 0)),
                )
                for row in session_rows
            ]

    def add_turn(self, session_id: str, role: str, kind: str, payload: Any) -> Turn:
        """Append a new turn, auto-incrementing `turn_index`.

        Raises `ValueError` if `role`/`kind` aren't one of the TRD's allowed
        values, or if `session_id` doesn't refer to an existing session.

        `turn_index` assignment is race-safe under concurrent calls for the
        same session: the session row is locked with `SELECT ... FOR UPDATE`
        before `max(turn_index)` is read, so two concurrent appends to the
        same session serialize rather than both computing the same next
        index. (The `(session_id, turn_index)` primary key is the backstop --
        a lost race would fail loudly on the unique constraint rather than
        silently overwrite a turn, which the previous Redis implementation
        avoided via `RPUSH`'s atomic length.) `FOR UPDATE` is a no-op on
        SQLite, which serializes writers anyway.
        """
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role '{role}'; must be one of {sorted(VALID_ROLES)}")
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind '{kind}'; must be one of {sorted(VALID_KINDS)}")

        self._ensure_schema()
        now = datetime.now(timezone.utc)
        with self._session_factory() as db:
            session_row = db.scalars(
                select(SessionRow).where(SessionRow.id == session_id).with_for_update()
            ).one_or_none()
            if session_row is None:
                raise ValueError(f"session '{session_id}' does not exist")

            highest = db.scalar(
                select(func.max(TurnRow.turn_index)).where(TurnRow.session_id == session_id)
            )
            turn_index = 0 if highest is None else int(highest) + 1

            db.add(
                TurnRow(
                    session_id=session_id,
                    turn_index=turn_index,
                    role=role,
                    kind=kind,
                    payload=payload,
                )
            )
            session_row.last_active_at = now
            db.commit()

        return Turn(session_id=session_id, turn_index=turn_index, role=role, kind=kind, payload=payload)

    def get_turns(self, session_id: str) -> list[Turn]:
        """All turns for a session, in chronological (turn_index) order."""
        self._ensure_schema()
        with self._session_factory() as db:
            rows = db.scalars(
                select(TurnRow).where(TurnRow.session_id == session_id).order_by(TurnRow.turn_index)
            )
            return [_to_turn(row) for row in rows]
