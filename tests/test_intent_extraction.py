import json

import httpx
import pytest

from app.intent_extraction import (
    MODEL,
    GroqClient,
    extract_single_lookup,
    extract_single_lookup_fields,
    load_prompts,
    propose_mapping,
)
from app.mapping import STANDARD_FIELDS


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": content}},
            ]
        },
    )


def _client(handler) -> GroqClient:
    """Inject the fake wire at the `groq` SDK's own `http_client` seam.

    Since unit #24 `GroqClient` talks to Groq through `langchain_groq.ChatGroq`
    (which wraps the official `groq` SDK) rather than a hand-rolled
    `httpx.Client.post`, so the transport is injected one level up -- as a
    fully-formed `httpx.Client` the SDK is told to use. Same guarantee as
    before: no unit test here ever opens a real connection to Groq.
    """
    return GroqClient(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_clean_successful_json_response_parses_correctly():
    captured = {}

    body = json.dumps(
        {
            "field_mappings": [
                {"standard_field": "first_name", "source_column": "First"},
                {"standard_field": "last_name", "source_column": "Last"},
                {"standard_field": "company", "source_column": "Company Name"},
            ],
            "selected_fields": ["name", "company"],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.read())
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        return _chat_response(body)

    client = _client(handler)
    proposal = client.propose_mapping(
        instructions_text="find these people's LinkedIn profiles",
        source_columns=["First", "Last", "Company Name"],
        sample_rows=[{"First": "Jane", "Last": "Doe", "Company Name": "Acme"}],
    )

    # The LangChain client must still issue the same live-verified request:
    # same endpoint, same bearer auth, same model, same JSON-object mode.
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    assert captured["json"]["model"] == MODEL
    assert captured["json"]["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in captured["json"]["messages"]] == ["system", "user"]
    assert captured["json"]["messages"][0]["content"] == load_prompts()["intent_extraction"]["system"]

    by_field = {m.standard_field: m.source_column for m in proposal.field_mappings}
    assert by_field["first_name"] == "First"
    assert by_field["last_name"] == "Last"
    assert by_field["company"] == "Company Name"
    assert set(by_field) == set(STANDARD_FIELDS)
    assert proposal.selected_fields == {"name", "company"}


def test_hallucinated_source_column_is_treated_as_unmapped():
    body = json.dumps(
        {
            "field_mappings": [
                {"standard_field": "company", "source_column": "Employer"},
            ],
            "selected_fields": ["company"],
        }
    )
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _chat_response(body)

    client = _client(handler)
    proposal = client.propose_mapping(
        instructions_text="map this",
        source_columns=["Company Name"],  # "Employer" is not in this list
        sample_rows=[{"Company Name": "Acme"}],
    )

    assert calls["n"] == 1  # no retry -- this is per-field sanitization, not a malformed response
    by_field = {m.standard_field: m.source_column for m in proposal.field_mappings}
    assert by_field["company"] is None
    assert proposal.selected_fields == {"company"}


def test_malformed_json_retries_once_then_fails_safe():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _chat_response("not valid json at all {")

    client = _client(handler)
    proposal = client.propose_mapping(
        instructions_text="map this",
        source_columns=["First", "Last"],
        sample_rows=[{"First": "Jane", "Last": "Doe"}],
    )

    assert calls["n"] == 2  # one retry, then fail safe -- no third attempt
    assert all(m.source_column is None for m in proposal.field_mappings)
    assert {m.standard_field for m in proposal.field_mappings} == set(STANDARD_FIELDS)
    assert proposal.selected_fields == set()


def test_retry_call_replays_the_bad_reply_and_the_correction():
    """The retry has to *show* the model what it got wrong, not just ask
    again -- otherwise it's a coin flip rather than a correction."""
    captured: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.read())["messages"])
        return _chat_response("not json")

    _client(handler).propose_mapping("map this", ["A"], [{"A": "1"}])

    assert [m["role"] for m in captured[0]] == ["system", "user"]
    assert [m["role"] for m in captured[1]] == ["system", "user", "assistant", "user"]
    assert captured[1][2]["content"] == "not json"
    assert captured[1][3]["content"] == load_prompts()["intent_extraction"]["retry"]


def test_malformed_json_first_then_valid_on_retry_succeeds():
    calls = {"n": 0}
    valid_body = json.dumps(
        {
            "field_mappings": [{"standard_field": "email", "source_column": "Work Email"}],
            "selected_fields": ["email"],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _chat_response("not json")
        return _chat_response(valid_body)

    client = _client(handler)
    proposal = client.propose_mapping(
        instructions_text="map this",
        source_columns=["Work Email"],
        sample_rows=[{"Work Email": "jane@acme.com"}],
    )

    assert calls["n"] == 2
    by_field = {m.standard_field: m.source_column for m in proposal.field_mappings}
    assert by_field["email"] == "Work Email"
    assert proposal.selected_fields == {"email"}


def test_missing_top_level_key_is_treated_as_malformed_and_fails_safe():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Missing "selected_fields" entirely -- structurally invalid.
        return _chat_response(json.dumps({"field_mappings": []}))

    client = _client(handler)
    proposal = client.propose_mapping(
        instructions_text="map this", source_columns=["A"], sample_rows=[{"A": "1"}]
    )

    assert calls["n"] == 2
    assert proposal.selected_fields == set()


def test_transport_failure_propagates_rather_than_failing_safe():
    """Bad LLM *output* fails safe; a dead endpoint must not -- otherwise an
    outage silently looks like "nothing could be mapped"."""
    from app.intent_extraction import GROQ_TRANSPORT_ERRORS

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(GROQ_TRANSPORT_ERRORS):
        _client(handler).propose_mapping("map this", ["A"], [{"A": "1"}])


def test_module_level_propose_mapping_delegates_to_injected_client():
    body = json.dumps(
        {
            "field_mappings": [{"standard_field": "full_name", "source_column": "Name"}],
            "selected_fields": ["name"],
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(body)

    proposal = propose_mapping(
        instructions_text="find Jane Doe",
        source_columns=["Name"],
        sample_rows=[{"Name": "Jane Doe"}],
        client=_client(handler),
    )

    by_field = {m.standard_field: m.source_column for m in proposal.field_mappings}
    assert by_field["full_name"] == "Name"
    assert proposal.selected_fields == {"name"}


# --- Unit #25: output_format, intent, and the conversation context window ---


def test_propose_mapping_reads_output_format_and_forces_data_request_intent():
    body = json.dumps(
        {
            "field_mappings": [{"standard_field": "full_name", "source_column": "Name"}],
            "selected_fields": ["name"],
            "intent": "greeting",  # nonsense for a turn that carries a table
            "output_format": "excel",
        }
    )
    proposal = _client(lambda r: _chat_response(body)).propose_mapping("give me a spreadsheet", ["Name"], [])

    assert proposal.output_format == "excel"
    # A turn carrying tabular data is a data request by construction -- the
    # model doesn't get to reclassify it as chitchat.
    assert proposal.intent == "data_request"


def test_propose_mapping_defaults_output_format_when_absent_or_invalid():
    body = json.dumps(
        {
            "field_mappings": [],
            "selected_fields": [],
            "output_format": "carrier pigeon",
        }
    )
    assert _client(lambda r: _chat_response(body)).propose_mapping("x", [], []).output_format == "table"


def test_context_turns_are_sent_as_prior_conversation():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["messages"] = json.loads(request.read())["messages"]
        return _chat_response(json.dumps({"field_mappings": [], "selected_fields": []}))

    _client(handler).propose_mapping(
        "actually search at Acme instead",
        ["Name"],
        [],
        context=[
            {"role": "user", "content": "find Jane Doe at Globex"},
            {"role": "system", "content": "[proposed criteria fields: company, name]"},
        ],
    )

    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert captured["messages"][1]["content"] == "find Jane Doe at Globex"
    assert "actually search at Acme instead" in captured["messages"][3]["content"]


def test_extract_single_lookup_fields_clean_response_parses_correctly():
    captured = {}
    body = json.dumps({"fields": {"full_name": "Jane Doe", "company": "Acme"}})

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.read())
        return _chat_response(body)

    fields = extract_single_lookup_fields("find Jane Doe at Acme", client=_client(handler))

    assert fields == {"full_name": "Jane Doe", "company": "Acme"}
    assert captured["json"]["response_format"] == {"type": "json_object"}


def test_extract_single_lookup_fields_drops_unknown_keys_and_empty_values():
    body = json.dumps(
        {"fields": {"full_name": "Jane Doe", "not_a_real_field": "x", "email": "", "title": "  "}}
    )

    fields = extract_single_lookup_fields("find Jane Doe", client=_client(lambda r: _chat_response(body)))

    # unknown key dropped, blank/whitespace-only values dropped -- only a
    # genuinely present, valid field survives.
    assert fields == {"full_name": "Jane Doe"}


def test_extract_single_lookup_fields_malformed_json_retries_once_then_fails_safe():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _chat_response("not valid json {")

    fields = extract_single_lookup_fields("find someone", client=_client(handler))

    assert calls["n"] == 2
    assert fields == {}


def test_extract_single_lookup_fields_malformed_then_valid_on_retry_succeeds():
    calls = {"n": 0}
    valid_body = json.dumps({"fields": {"company": "Acme"}})

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _chat_response("not json")
        return _chat_response(valid_body)

    fields = extract_single_lookup_fields("find Acme's page", client=_client(handler))

    assert calls["n"] == 2
    assert fields == {"company": "Acme"}


def test_extract_single_lookup_classifies_a_greeting_and_carries_a_reply():
    body = json.dumps(
        {"fields": {}, "intent": "greeting", "output_format": "table", "reply": "Hi there! What can I look up?"}
    )

    extraction = extract_single_lookup("hi", client=_client(lambda r: _chat_response(body)))

    assert extraction.intent == "greeting"
    assert extraction.fields == {}
    assert extraction.reply == "Hi there! What can I look up?"


def test_greeting_with_no_reply_text_falls_back_to_a_canned_one():
    from app.intent_extraction import DEFAULT_GREETING_REPLY

    body = json.dumps({"fields": {}, "intent": "greeting", "reply": "   "})

    extraction = extract_single_lookup("hello", client=_client(lambda r: _chat_response(body)))

    assert extraction.reply == DEFAULT_GREETING_REPLY


def test_unknown_intent_value_falls_back_to_data_request():
    body = json.dumps({"fields": {"company": "Acme"}, "intent": "smalltalk"})

    extraction = extract_single_lookup("find Acme", client=_client(lambda r: _chat_response(body)))

    # Misrouting a real lookup to a chat reply loses the user's request, so
    # anything unrecognized fails safe towards running the pipeline.
    assert extraction.intent == "data_request"


def test_single_lookup_fail_safe_is_a_data_request_not_a_greeting():
    extraction = extract_single_lookup(
        "find someone", client=_client(lambda r: _chat_response("not json {"))
    )

    assert extraction.intent == "data_request"
    assert extraction.fields == {}
    assert extraction.reply == ""


def test_single_lookup_reads_output_format():
    body = json.dumps({"fields": {"company": "Acme"}, "output_format": "excel"})

    extraction = extract_single_lookup(
        "find Acme and give me a spreadsheet", client=_client(lambda r: _chat_response(body))
    )

    assert extraction.output_format == "excel"


def test_prompts_are_loaded_from_the_json_file_with_field_catalogs_substituted():
    prompts = load_prompts()

    assert set(prompts) == {"intent_extraction", "single_lookup_extraction", "verification"}
    assert "<<standard_fields>>" not in prompts["intent_extraction"]["system"]
    assert str(STANDARD_FIELDS) in prompts["intent_extraction"]["system"]
    assert "['company', 'email', 'name', 'title']" in prompts["intent_extraction"]["system"]
