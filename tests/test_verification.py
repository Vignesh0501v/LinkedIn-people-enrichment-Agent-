import json

import httpx

from app.intent_extraction import MODEL, GroqClient, load_prompts
from app.mapping import FieldMapping
from app.tavily_search import Candidate
from app.verification import VerificationResult, verify_candidate


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
    """Inject the fake wire at the `groq` SDK's `http_client` seam -- see
    tests/test_intent_extraction.py's `_client` for why this moved up a
    level in unit #24. No real Groq call is ever made here."""
    return GroqClient(api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


_CANDIDATE = Candidate(
    url="https://www.linkedin.com/in/janedoe",
    title="Jane Doe - VP Engineering - Acme Corp",
    snippet="Jane Doe is VP Engineering at Acme Corp, based in Austin.",
)

_MAPPINGS = [
    FieldMapping(standard_field="full_name", source_column="Name"),
    FieldMapping(standard_field="company", source_column="Company"),
]

_ROW_VALUES = {"Name": "Jane Doe", "Company": "Acme Corp"}
_SELECTED_FIELDS = {"name", "company"}


def test_clean_successful_response_parses_correctly():
    captured = {}
    body = json.dumps(
        {
            "is_match": True,
            "confidence": 0.92,
            "city": "Austin",
            "reason": "Name and company both match.",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.read())
        captured["auth"] = request.headers.get("authorization")
        return _chat_response(body)

    client = _client(handler)
    result = verify_candidate(_CANDIDATE, _ROW_VALUES, _SELECTED_FIELDS, _MAPPINGS, client=client)

    assert captured["auth"] == "Bearer test-key"
    assert captured["json"]["model"] == MODEL
    assert captured["json"]["response_format"] == {"type": "json_object"}
    # Job 2's prompt lives in app/prompts.json since unit #24, same as job 1's.
    assert captured["json"]["messages"][0]["content"] == load_prompts()["verification"]["system"]

    assert result == VerificationResult(
        is_match=True, confidence=0.92, city="Austin", reason="Name and company both match."
    )


def test_confidence_outside_range_gets_clamped():
    body = json.dumps(
        {
            "is_match": True,
            "confidence": 1.7,
            "city": None,
            "reason": "Strong match.",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(body)

    client = _client(handler)
    result = verify_candidate(_CANDIDATE, _ROW_VALUES, _SELECTED_FIELDS, _MAPPINGS, client=client)

    assert result.confidence == 1.0

    body_negative = json.dumps(
        {
            "is_match": False,
            "confidence": -0.3,
            "city": None,
            "reason": "No overlap.",
        }
    )

    def handler_negative(request: httpx.Request) -> httpx.Response:
        return _chat_response(body_negative)

    client_negative = _client(handler_negative)
    result_negative = verify_candidate(
        _CANDIDATE, _ROW_VALUES, _SELECTED_FIELDS, _MAPPINGS, client=client_negative
    )

    assert result_negative.confidence == 0.0


def test_malformed_json_retries_once_then_fails_safe():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _chat_response("not valid json at all {")

    client = _client(handler)
    result = verify_candidate(_CANDIDATE, _ROW_VALUES, _SELECTED_FIELDS, _MAPPINGS, client=client)

    assert calls["n"] == 2  # one retry, then fail safe -- no third attempt
    assert result == VerificationResult(
        is_match=False,
        confidence=0.0,
        city=None,
        resolved=False,
        reason="verification failed — treat as unresolved",
    )
    assert result.resolved is False


def test_malformed_json_first_then_valid_on_retry_succeeds():
    calls = {"n": 0}
    valid_body = json.dumps(
        {
            "is_match": True,
            "confidence": 0.6,
            "city": "Denver",
            "reason": "Plausible match on retry.",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _chat_response("not json")
        return _chat_response(valid_body)

    client = _client(handler)
    result = verify_candidate(_CANDIDATE, _ROW_VALUES, _SELECTED_FIELDS, _MAPPINGS, client=client)

    assert calls["n"] == 2
    assert result == VerificationResult(is_match=True, confidence=0.6, city="Denver", reason="Plausible match on retry.")


def test_response_missing_required_field_is_treated_as_malformed():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Missing "reason" entirely -- structurally invalid.
        return _chat_response(json.dumps({"is_match": True, "confidence": 0.8, "city": None}))

    client = _client(handler)
    result = verify_candidate(_CANDIDATE, _ROW_VALUES, _SELECTED_FIELDS, _MAPPINGS, client=client)

    assert calls["n"] == 2
    assert result == VerificationResult(
        is_match=False,
        confidence=0.0,
        city=None,
        resolved=False,
        reason="verification failed — treat as unresolved",
    )
    assert result.resolved is False
