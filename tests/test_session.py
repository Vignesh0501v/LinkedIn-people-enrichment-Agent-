import pytest

from app.session import SessionStore, create_session_engine


def _store() -> SessionStore:
    """A store backed by a throwaway in-memory SQLite database.

    Replaces the pre-unit-#23 `FakeRedis` stand-in: the injection seam moved
    from a fake Redis client to a real (but disposable) SQLAlchemy engine, so
    these tests exercise the actual SQL the Postgres deployment runs, without
    needing a Postgres server in CI or the sandbox.
    """
    return SessionStore(engine=create_session_engine("sqlite://"))


def test_create_session_returns_session_with_id_and_timestamps():
    store = _store()

    session = store.create_session()

    assert session.id
    assert session.created_at is not None
    assert session.last_active_at == session.created_at


def test_get_session_round_trips():
    store = _store()
    created = store.create_session()

    fetched = store.get_session(created.id)

    assert fetched == created


def test_get_session_missing_id_returns_none():
    store = _store()

    assert store.get_session("does-not-exist") is None


def test_add_turn_auto_increments_turn_index_in_order():
    store = _store()
    session = store.create_session()

    turn_0 = store.add_turn(
        session.id, role="user", kind="instruction_text", payload={"text": "find Jane Doe at Acme"}
    )
    turn_1 = store.add_turn(
        session.id,
        role="system",
        kind="mapping_proposal",
        payload={"field_mappings": [{"standard_field": "company", "source_column": "Company"}]},
    )

    assert turn_0.turn_index == 0
    assert turn_1.turn_index == 1
    assert turn_0.session_id == session.id
    assert turn_1.session_id == session.id


def test_get_turns_returns_history_in_order():
    store = _store()
    session = store.create_session()

    store.add_turn(session.id, role="user", kind="instruction_text", payload={"text": "first"})
    store.add_turn(session.id, role="system", kind="mapping_proposal", payload={"n": 2})
    store.add_turn(session.id, role="user", kind="mapping_confirmed", payload={"n": 3})

    turns = store.get_turns(session.id)

    assert [turn.turn_index for turn in turns] == [0, 1, 2]
    assert [turn.kind for turn in turns] == ["instruction_text", "mapping_proposal", "mapping_confirmed"]
    assert turns[0].payload == {"text": "first"}
    assert turns[2].payload == {"n": 3}


def test_turn_payload_round_trips_nested_json():
    """`payload` is a JSON column, not a string -- nested structures (which
    the enriched unit #23 mapping_proposal payload uses) must survive intact."""
    store = _store()
    session = store.create_session()

    payload = {
        "field_mappings": [{"standard_field": "company", "source_column": "Company"}],
        "columns": ["Name", "Company"],
        "sample_rows": [{"Name": "Jane Doe", "Company": "Acme"}],
    }
    store.add_turn(session.id, role="system", kind="mapping_proposal", payload=payload)

    assert store.get_turns(session.id)[0].payload == payload


def test_get_turns_for_unknown_session_returns_empty_list():
    store = _store()

    assert store.get_turns("does-not-exist") == []


def test_add_turn_updates_session_last_active_at():
    store = _store()
    session = store.create_session()

    store.add_turn(session.id, role="user", kind="instruction_text", payload={"text": "hi"})

    updated = store.get_session(session.id)
    assert updated.last_active_at >= session.last_active_at


def test_add_turn_rejects_invalid_role():
    store = _store()
    session = store.create_session()

    with pytest.raises(ValueError):
        store.add_turn(session.id, role="assistant", kind="instruction_text", payload={})


def test_add_turn_rejects_invalid_kind():
    store = _store()
    session = store.create_session()

    with pytest.raises(ValueError):
        store.add_turn(session.id, role="user", kind="not_a_real_kind", payload={})


def test_add_turn_accepts_greeting_reply_kind():
    store = _store()
    session = store.create_session()

    turn = store.add_turn(session.id, role="system", kind="greeting_reply", payload={"text": "Hi!"})

    assert turn.kind == "greeting_reply"


def test_add_turn_rejects_unknown_session():
    store = _store()

    with pytest.raises(ValueError):
        store.add_turn("does-not-exist", role="user", kind="instruction_text", payload={})


def test_list_sessions_is_newest_active_first_with_previews():
    store = _store()
    older = store.create_session()
    newer = store.create_session()

    store.add_turn(older.id, role="user", kind="instruction_text", payload={"text": "find Jane Doe at Acme"})
    store.add_turn(newer.id, role="user", kind="file_upload", payload={"filename": "leads.xlsx", "row_count": 3})
    store.add_turn(newer.id, role="system", kind="mapping_proposal", payload={"batch_id": "b"})

    summaries = store.list_sessions()

    assert [s.id for s in summaries] == [newer.id, older.id]
    assert summaries[0].first_turn_preview == "leads.xlsx"
    assert summaries[0].turn_count == 2
    assert summaries[1].first_turn_preview == "find Jane Doe at Acme"
    assert summaries[1].turn_count == 1


def test_list_sessions_previews_a_pasted_table_and_truncates_long_text():
    store = _store()
    pasted = store.create_session()
    long_text = store.create_session()

    store.add_turn(pasted.id, role="user", kind="pasted_table", payload={"row_count": 42, "warnings": []})
    store.add_turn(long_text.id, role="user", kind="instruction_text", payload={"text": "x" * 500})

    by_id = {s.id: s for s in store.list_sessions()}

    assert by_id[pasted.id].first_turn_preview == "Pasted table (42 rows)"
    assert len(by_id[long_text.id].first_turn_preview) <= 120
    assert by_id[long_text.id].first_turn_preview.endswith("…")


def test_list_sessions_includes_a_session_with_no_turns_yet():
    store = _store()
    session = store.create_session()

    summaries = store.list_sessions()

    assert [s.id for s in summaries] == [session.id]
    assert summaries[0].first_turn_preview == ""
    assert summaries[0].turn_count == 0


def test_list_sessions_respects_limit():
    store = _store()
    for _ in range(3):
        store.create_session()

    assert len(store.list_sessions(limit=2)) == 2
