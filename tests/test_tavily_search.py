import httpx

from app.tavily_search import TavilyClient


def _response(urls: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {"url": url, "title": "John Doe", "content": "Acme"} for url in urls
            ]
        },
    )


def test_attempt_1_succeeds_uses_all_provided_fields_and_include_domains():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read()
        captured["auth"] = request.headers.get("authorization")
        import json

        captured["json"] = json.loads(captured["body"])
        return _response(["https://linkedin.com/in/johndoe"])

    client = TavilyClient(
        api_key="tvly-test-key",
        transport=httpx.MockTransport(handler),
    )
    fields = {"name": "John Doe", "company": "Acme", "title": "CEO", "email": "john@acme.com"}
    result = client.find_linkedin_candidates(fields)

    assert result.reason is None
    assert len(result.candidates) == 1
    assert result.candidates[0].url == "https://linkedin.com/in/johndoe"
    assert captured["auth"] == "Bearer tvly-test-key"
    assert captured["json"]["include_domains"] == ["linkedin.com"]
    assert captured["json"]["search_depth"] == "basic"
    assert "John Doe" in captured["json"]["query"]
    assert "Acme" in captured["json"]["query"]
    assert "CEO" in captured["json"]["query"]
    assert "john@acme.com" in captured["json"]["query"]


def test_attempt_1_empty_then_attempt_2_succeeds_with_name_and_company_only():
    import json

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        calls.append(body["query"])
        if len(calls) == 1:
            return _response([])
        return _response(["https://linkedin.com/in/johndoe"])

    client = TavilyClient(api_key="tvly-test-key", transport=httpx.MockTransport(handler))
    fields = {"name": "John Doe", "company": "Acme", "title": "CEO", "email": "john@acme.com"}
    result = client.find_linkedin_candidates(fields)

    assert len(calls) == 2
    assert "CEO" in calls[0]
    assert "CEO" not in calls[1]
    assert "john@acme.com" not in calls[1]
    assert "John Doe" in calls[1]
    assert "Acme" in calls[1]
    assert result.reason is None
    assert len(result.candidates) == 1
    assert result.query_used == calls[1]


def test_both_attempts_empty_reports_a_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        return _response([])

    client = TavilyClient(api_key="tvly-test-key", transport=httpx.MockTransport(handler))
    fields = {"name": "Jane Smith", "company": "Globex"}
    result = client.find_linkedin_candidates(fields)

    assert result.candidates == []
    assert result.query_used is None
    assert result.reason is not None
    assert "2" in result.reason


def test_no_name_or_company_selected_means_no_fallback_attempt():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _response([])

    client = TavilyClient(api_key="tvly-test-key", transport=httpx.MockTransport(handler))
    fields = {"title": "CEO", "email": "jane@globex.com"}
    result = client.find_linkedin_candidates(fields)

    assert call_count["n"] == 1
    assert result.candidates == []
    assert result.query_used is None
    assert result.reason is not None
    assert "1" in result.reason
