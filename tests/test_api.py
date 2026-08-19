import fnmatch
import io
import json

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.batch import BatchStore
from app.intent_extraction import GroqClient
from app.api import create_app
from app.session import SessionStore, create_session_engine
from app.tavily_search import TavilyClient
from app.template_store import TemplateStore


class FakeRedis:
    """Minimal in-memory stand-in for the `redis.Redis` methods this repo's
    stores actually call. Extends the `set`/`get`/`scan_iter` surface used by
    `tests/test_template_store.py`'s `FakeRedis` with the list methods
    (`rpush`/`lset`/`lrange`) and the hash methods
    (`hset`/`hget`/`hgetall`/`hlen`) `app.batch.BatchStore` needs for its
    per-row hash. A single instance is shared across `BatchStore` and
    `TemplateStore` in these tests since each store's keys are distinctly
    prefixed (mirrors how a single real Redis instance would be shared in
    production). `SessionStore` no longer appears here: since unit #23 it is
    Postgres-backed and gets an in-memory SQLite engine instead.
    """

    def __init__(self):
        self._strings: dict[str, bytes] = {}
        self._lists: dict[str, list[bytes]] = {}
        self._hashes: dict[str, dict[str, bytes]] = {}

    def set(self, key: str, value: str) -> None:
        self._strings[key] = value.encode("utf-8")

    def get(self, key: str) -> bytes | None:
        return self._strings.get(key)

    def rpush(self, key: str, value: str) -> int:
        self._lists.setdefault(key, []).append(value.encode("utf-8"))
        return len(self._lists[key])

    def lset(self, key: str, index: int, value: str) -> None:
        self._lists[key][index] = value.encode("utf-8")

    def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        values = self._lists.get(key, [])
        if end == -1:
            return values[start:]
        return values[start : end + 1]

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


def _groq_response(field_mappings: list[dict], selected_fields: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"field_mappings": field_mappings, "selected_fields": selected_fields}
                        ),
                    }
                }
            ]
        },
    )


def _make_test_client(groq_handler, tavily_handler) -> TestClient:
    redis_client = FakeRedis()
    session_store = SessionStore(engine=create_session_engine("sqlite://"))
    batch_store = BatchStore(client=redis_client)
    template_store = TemplateStore(client=redis_client)
    tavily_client = TavilyClient(api_key="tvly-test", transport=httpx.MockTransport(tavily_handler))
    groq_client = GroqClient(
        api_key="groq-test", http_client=httpx.Client(transport=httpx.MockTransport(groq_handler))
    )

    app = create_app(
        session_store=session_store,
        batch_store=batch_store,
        template_store=template_store,
        tavily_client=tavily_client,
        groq_client=groq_client,
    )
    return TestClient(app)


_NAME_COMPANY_PROPOSAL_MAPPINGS = [
    {"standard_field": "full_name", "source_column": "Name"},
    {"standard_field": "company", "source_column": "Company"},
]


def _default_groq_handler(request: httpx.Request) -> httpx.Response:
    return _groq_response(_NAME_COMPANY_PROPOSAL_MAPPINGS, ["name", "company"])


def _create_session(client: TestClient) -> str:
    resp = client.post("/sessions")
    assert resp.status_code == 200
    return resp.json()["session_id"]


def test_happy_path_paste_to_download(monkeypatch):
    def tavily_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        query = body["query"]
        if "Jane Doe" in query:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://linkedin.com/in/janedoe",
                            "title": "Jane Doe",
                            "content": "Jane Doe - Acme - LinkedIn profile",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    client = _make_test_client(_default_groq_handler, tavily_handler)

    session_id = _create_session(client)

    pasted_text = "Name\tCompany\nJane Doe\tAcme\nJohn Smith\tGlobex"
    turn_resp = client.post(
        f"/sessions/{session_id}/turns",
        data={"pasted_text": pasted_text, "instructions_text": "find these people's LinkedIn profiles"},
    )
    assert turn_resp.status_code == 200
    turn_body = turn_resp.json()
    assert turn_body["columns"] == ["Name", "Company"]
    batch_id = turn_body["batch_id"]
    assert turn_body["proposal"]["selected_fields"] == ["company", "name"]

    mapping_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/mapping",
        json=_NAME_COMPANY_PROPOSAL_MAPPINGS,
    )
    assert mapping_resp.status_code == 200

    criteria_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/criteria",
        json={"selected_fields": ["name", "company"]},
    )
    assert criteria_resp.status_code == 200
    assert criteria_resp.json()["search_mode"] == "person"

    status_resp = client.get(f"/sessions/{session_id}/batches/{batch_id}")
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["status"] == "completed"
    assert status_body["row_count"] == 2
    assert status_body["rows_completed"] == 2
    assert status_body["counts_by_status"]["VERIFIED"] == 1
    assert status_body["counts_by_status"]["NOT_FOUND"] == 1

    rows_resp = client.get(f"/sessions/{session_id}/batches/{batch_id}/rows")
    assert rows_resp.status_code == 200
    rows = rows_resp.json()["rows"]
    assert len(rows) == 2
    jane_row = next(r for r in rows if r["linkedin_url"] == "https://linkedin.com/in/janedoe")
    assert jane_row["match_status"] == "VERIFIED"

    download_resp = client.get(f"/sessions/{session_id}/batches/{batch_id}/download")
    assert download_resp.status_code == 200
    assert download_resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    df = pd.read_excel(io.BytesIO(download_resp.content))
    # Exports by mapped standard_field name ("full_name"), not the search-criteria
    # logical name ("name") -- see the download-export fix in app/api.py.
    assert list(df.columns) == ["company", "full_name", "linkedin_url", "city", "match_confidence", "match_status"]
    assert len(df) == 2
    assert set(df["match_status"]) == {"VERIFIED", "NOT_FOUND"}

    session_resp = client.get(f"/sessions/{session_id}")
    assert session_resp.status_code == 200
    kinds = [t["kind"] for t in session_resp.json()["turns"]]
    assert kinds == ["pasted_table", "mapping_proposal", "mapping_confirmed", "mapping_confirmed"]


def test_criteria_rejects_selecting_an_unmapped_field():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    pasted_text = "Name\tCompany\nJane Doe\tAcme"
    turn_resp = client.post(f"/sessions/{session_id}/turns", data={"pasted_text": pasted_text})
    batch_id = turn_resp.json()["batch_id"]

    # Confirm a mapping that only maps "company" -- "name" is left unmapped.
    mapping_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/mapping",
        json=[{"standard_field": "company", "source_column": "Company"}],
    )
    assert mapping_resp.status_code == 200

    criteria_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/criteria",
        json={"selected_fields": ["name", "company"]},
    )
    assert criteria_resp.status_code == 422
    errors = criteria_resp.json()["detail"]["errors"]
    assert any("name" in error for error in errors)


def test_criteria_rejects_empty_selection():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    pasted_text = "Name\tCompany\nJane Doe\tAcme"
    turn_resp = client.post(f"/sessions/{session_id}/turns", data={"pasted_text": pasted_text})
    batch_id = turn_resp.json()["batch_id"]

    mapping_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/mapping",
        json=_NAME_COMPANY_PROPOSAL_MAPPINGS,
    )
    assert mapping_resp.status_code == 200

    criteria_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/criteria",
        json={"selected_fields": []},
    )
    assert criteria_resp.status_code == 422
    errors = criteria_resp.json()["detail"]["errors"]
    assert any("empty" in error for error in errors)


def test_row_missing_selected_criterion_value_is_flagged_before_any_search_call():
    tavily_calls = {"n": 0}

    def tavily_handler(request: httpx.Request) -> httpx.Response:
        tavily_calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://linkedin.com/in/janedoe",
                        "title": "Jane Doe",
                        "content": "Jane Doe - Acme - LinkedIn profile",
                    }
                ]
            },
        )

    client = _make_test_client(_default_groq_handler, tavily_handler)
    session_id = _create_session(client)

    # Second row has no Company value at all -- "company" is a selected
    # criterion, so this row must come back MISSING_SEARCH_FIELD without
    # ever calling Tavily for it.
    pasted_text = "Name\tCompany\nJane Doe\tAcme\nJohn Smith\t"
    turn_resp = client.post(f"/sessions/{session_id}/turns", data={"pasted_text": pasted_text})
    batch_id = turn_resp.json()["batch_id"]

    client.put(
        f"/sessions/{session_id}/batches/{batch_id}/mapping",
        json=_NAME_COMPANY_PROPOSAL_MAPPINGS,
    )
    criteria_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/criteria",
        json={"selected_fields": ["name", "company"]},
    )
    assert criteria_resp.status_code == 200

    rows_resp = client.get(
        f"/sessions/{session_id}/batches/{batch_id}/rows", params={"status": "MISSING_SEARCH_FIELD"}
    )
    rows = rows_resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["match_status"] == "MISSING_SEARCH_FIELD"
    assert rows[0]["linkedin_url"] is None
    assert "company" in rows[0]["source_reason"]

    # Only the Jane Doe row should have triggered a Tavily call.
    assert tavily_calls["n"] == 1


def test_turn_requires_some_input():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    resp = client.post(f"/sessions/{session_id}/turns", data={})
    assert resp.status_code == 422


def test_unknown_session_returns_404():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    resp = client.get("/sessions/does-not-exist")
    assert resp.status_code == 404


def test_mapping_templates_round_trip():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))

    create_resp = client.post(
        "/mapping-templates",
        json={"source_label": "HR export", "field_mappings": {"company": "Company Name"}},
    )
    assert create_resp.status_code == 200
    template_id = create_resp.json()["id"]

    list_resp = client.get("/mapping-templates")
    assert list_resp.status_code == 200
    templates = list_resp.json()["templates"]
    assert any(t["id"] == template_id for t in templates)


def test_apply_template_populates_mapping_from_saved_template():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    create_resp = client.post(
        "/mapping-templates",
        json={
            "source_label": "HR export",
            "field_mappings": {"company": "Company Name", "email": "Work Email"},
        },
    )
    template_id = create_resp.json()["id"]

    # "Company Name" matches the template; "Work Email" doesn't exist in
    # this batch's actual columns, so email should come back unmatched.
    pasted_text = "Name\tCompany Name\nJane Doe\tAcme"
    turn_resp = client.post(f"/sessions/{session_id}/turns", data={"pasted_text": pasted_text})
    batch_id = turn_resp.json()["batch_id"]

    apply_resp = client.post(f"/sessions/{session_id}/batches/{batch_id}/mapping/apply-template/{template_id}")
    assert apply_resp.status_code == 200
    body = apply_resp.json()
    by_field = {m["standard_field"]: m["source_column"] for m in body["field_mappings"]}
    assert by_field["company"] == "Company Name"
    assert by_field["email"] is None
    assert body["unmatched_fields"] == ["email"]


def test_apply_template_missing_template_returns_404():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    turn_resp = client.post(f"/sessions/{session_id}/turns", data={"pasted_text": "Name\tCompany\nJane\tAcme"})
    batch_id = turn_resp.json()["batch_id"]

    resp = client.post(f"/sessions/{session_id}/batches/{batch_id}/mapping/apply-template/does-not-exist")
    assert resp.status_code == 404


def test_single_lookup_happy_path():
    def single_lookup_groq_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"fields": {"full_name": "Jane Doe", "company": "Acme"}}),
                        }
                    }
                ]
            },
        )

    def tavily_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://linkedin.com/in/janedoe",
                        "title": "Jane Doe",
                        "content": "Jane Doe - Acme - LinkedIn profile",
                    }
                ]
            },
        )

    client = _make_test_client(single_lookup_groq_handler, tavily_handler)
    session_id = _create_session(client)

    turn_resp = client.post(
        f"/sessions/{session_id}/turns", data={"instructions_text": "find Jane Doe at Acme"}
    )
    assert turn_resp.status_code == 200
    turn_body = turn_resp.json()
    batch_id = turn_body["batch_id"]
    # The extracted field names ARE the batch's columns (identity mapping) --
    # this is what makes confirm_mapping able to accept a non-null source_column.
    assert set(turn_body["columns"]) == {"full_name", "company"}
    assert turn_body["proposal"]["selected_fields"] == ["company", "name"]

    proposed_mappings = turn_body["proposal"]["field_mappings"]
    mapping_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/mapping", json=proposed_mappings
    )
    assert mapping_resp.status_code == 200

    criteria_resp = client.put(
        f"/sessions/{session_id}/batches/{batch_id}/criteria",
        json={"selected_fields": ["name", "company"]},
    )
    assert criteria_resp.status_code == 200

    status_resp = client.get(f"/sessions/{session_id}/batches/{batch_id}")
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["status"] == "completed"
    assert status_body["row_count"] == 1
    assert status_body["counts_by_status"].get("VERIFIED") == 1


# --- Unit #23: session list + reload-able mapping_proposal payload ---


def _groq_json_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]},
    )


def test_list_sessions_is_newest_first_with_a_usable_preview():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))

    first = _create_session(client)
    second = _create_session(client)
    client.post(f"/sessions/{first}/turns", data={"pasted_text": "Name\tCompany\nJane Doe\tAcme"})

    resp = client.get("/sessions")
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]

    # `first` was touched most recently (it got a turn), so it sorts first.
    assert [s["session_id"] for s in sessions] == [first, second]
    assert sessions[0]["first_turn_preview"] == "Pasted table (1 rows)"
    assert sessions[0]["turn_count"] == 2  # the pasted table + the mapping proposal
    assert sessions[1]["first_turn_preview"] == ""
    assert "last_active_at" in sessions[0] and "created_at" in sessions[0]


def test_mapping_proposal_turn_carries_columns_and_sample_rows_for_reload():
    """The reload gap flagged when unit #12 shipped: without columns and
    sample rows on the turn itself, a reopened session can only render the
    proposal as inert text -- there's nothing to populate the dropdowns or
    the sample-row preview from."""
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    client.post(
        f"/sessions/{session_id}/turns",
        data={"pasted_text": "Name\tCompany\nJane Doe\tAcme\nJohn Smith\tGlobex"},
    )

    turns = client.get(f"/sessions/{session_id}").json()["turns"]
    proposal_turn = next(t for t in turns if t["kind"] == "mapping_proposal")

    assert proposal_turn["payload"]["columns"] == ["Name", "Company"]
    assert proposal_turn["payload"]["sample_rows"] == [
        {"Name": "Jane Doe", "Company": "Acme"},
        {"Name": "John Smith", "Company": "Globex"},
    ]
    assert proposal_turn["payload"]["output_format"] == "table"


# --- Unit #25: greeting detection, output_format, context window ---


def test_greeting_turn_replies_conversationally_without_creating_a_batch():
    def greeting_handler(request: httpx.Request) -> httpx.Response:
        return _groq_json_response(
            {"fields": {}, "intent": "greeting", "output_format": "table", "reply": "Hi! What can I look up?"}
        )

    client = _make_test_client(greeting_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    resp = client.post(f"/sessions/{session_id}/turns", data={"instructions_text": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent"] == "greeting"
    assert body["batch_id"] is None
    assert body["reply"] == "Hi! What can I look up?"

    kinds = [t["kind"] for t in client.get(f"/sessions/{session_id}").json()["turns"]]
    assert kinds == ["instruction_text", "greeting_reply"]


def test_greeting_detection_does_not_apply_to_a_pasted_table():
    # A table is a data request whatever the model says about the prose --
    # otherwise a chatty "hi, here is my list" would discard the list.
    def greeting_shaped_handler(request: httpx.Request) -> httpx.Response:
        return _groq_json_response(
            {
                "field_mappings": _NAME_COMPANY_PROPOSAL_MAPPINGS,
                "selected_fields": ["name", "company"],
                "intent": "greeting",
            }
        )

    client = _make_test_client(
        greeting_shaped_handler, lambda request: httpx.Response(200, json={"results": []})
    )
    session_id = _create_session(client)

    resp = client.post(
        f"/sessions/{session_id}/turns",
        data={"pasted_text": "Name\tCompany\nJane Doe\tAcme", "instructions_text": "hi there"},
    )

    assert resp.json()["intent"] == "data_request"
    assert resp.json()["batch_id"] is not None


def test_output_format_excel_is_honoured_from_the_users_phrasing():
    def excel_handler(request: httpx.Request) -> httpx.Response:
        return _groq_json_response(
            {
                "field_mappings": _NAME_COMPANY_PROPOSAL_MAPPINGS,
                "selected_fields": ["name", "company"],
                "output_format": "excel",
            }
        )

    client = _make_test_client(excel_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    resp = client.post(
        f"/sessions/{session_id}/turns",
        data={"pasted_text": "Name\tCompany\nJane Doe\tAcme", "instructions_text": "download as excel"},
    )

    assert resp.json()["output_format"] == "excel"
    batch_id = resp.json()["batch_id"]
    assert client.get(f"/sessions/{session_id}/batches/{batch_id}").json()["output_format"] == "excel"


def test_large_batch_forces_excel_even_when_a_table_was_requested():
    from app.api import MAX_INLINE_TABLE_ROWS

    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    rows = "\n".join(f"Person {i}\tAcme" for i in range(MAX_INLINE_TABLE_ROWS + 1))
    resp = client.post(f"/sessions/{session_id}/turns", data={"pasted_text": f"Name\tCompany\n{rows}"})

    assert resp.json()["output_format"] == "excel"


def test_small_batch_stays_a_table():
    client = _make_test_client(_default_groq_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    resp = client.post(f"/sessions/{session_id}/turns", data={"pasted_text": "Name\tCompany\nJane Doe\tAcme"})

    assert resp.json()["output_format"] == "table"


def test_prior_turns_are_sent_to_groq_as_conversation_context():
    from app.api import CONTEXT_WINDOW_TURNS

    seen: list[list[dict]] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.read())["messages"])
        return _groq_response(_NAME_COMPANY_PROPOSAL_MAPPINGS, ["name", "company"])

    client = _make_test_client(recording_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    client.post(f"/sessions/{session_id}/turns", data={"pasted_text": "Name\tCompany\nJane Doe\tGlobex"})
    client.post(
        f"/sessions/{session_id}/turns",
        data={"pasted_text": "Name\tCompany\nJane Doe\tAcme", "instructions_text": "actually search at Acme"},
    )

    # First call had no history; the second one carries the earlier turns.
    # Message layout is [system] + context + [user].
    assert [m["role"] for m in seen[0]] == ["system", "user"]
    assert len(seen[1]) > 2
    assert len(seen[1]) - 2 <= CONTEXT_WINDOW_TURNS


def test_context_window_is_capped_at_five_turns():
    from app.api import CONTEXT_WINDOW_TURNS

    seen: list[list[dict]] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.read())["messages"])
        return _groq_response(_NAME_COMPANY_PROPOSAL_MAPPINGS, ["name", "company"])

    client = _make_test_client(recording_handler, lambda request: httpx.Response(200, json={"results": []}))
    session_id = _create_session(client)

    for _ in range(6):
        client.post(f"/sessions/{session_id}/turns", data={"pasted_text": "Name\tCompany\nJane Doe\tAcme"})

    # 10 turns of history exist by the last call; only the window is sent.
    assert len(seen[-1]) - 2 <= CONTEXT_WINDOW_TURNS


# --- Unit #26: Summary sheet in the Excel export ---


def test_download_has_a_summary_sheet_alongside_results():
    def tavily_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://linkedin.com/in/janedoe",
                        "title": "Jane Doe",
                        "content": "Jane Doe - Acme - LinkedIn profile",
                    }
                ]
            },
        )

    client = _make_test_client(_default_groq_handler, tavily_handler)
    session_id = _create_session(client)

    turn_resp = client.post(f"/sessions/{session_id}/turns", data={"pasted_text": "Name\tCompany\nJane Doe\tAcme"})
    batch_id = turn_resp.json()["batch_id"]
    client.put(f"/sessions/{session_id}/batches/{batch_id}/mapping", json=_NAME_COMPANY_PROPOSAL_MAPPINGS)
    client.put(
        f"/sessions/{session_id}/batches/{batch_id}/criteria", json={"selected_fields": ["name", "company"]}
    )

    download = client.get(f"/sessions/{session_id}/batches/{batch_id}/download")
    assert download.status_code == 200

    sheets = pd.read_excel(io.BytesIO(download.content), sheet_name=None)
    assert list(sheets) == ["Results", "Summary"]

    summary = dict(zip(sheets["Summary"]["Metric"], sheets["Summary"]["Value"]))
    assert summary["Batch ID"] == batch_id
    assert summary["Search mode"] == "person"
    assert summary["Search criteria fields"] == "company, name"
    assert int(summary["Total rows"]) == 1
    assert int(summary["Rows VERIFIED"]) == 1
    assert int(summary["Rows NOT_FOUND"]) == 0
    assert summary["Generated at (UTC)"]
