"""Tavily search client — the sole search backend for this pipeline.

Takes a flexible field-name -> value dict instead of fixed name/company
parameters, since the operator can select any combination of mapped fields
(name, company, title, email) as search criteria per batch (see the TRD's
"Search-criteria & mode derivation" section).

Auth: Tavily's `/search` endpoint uses bearer-token auth — the request must
carry an `Authorization: Bearer <TAVILY_API_KEY>` header (confirmed against
Tavily's live OpenAPI spec at
https://docs.tavily.com/documentation/api-reference/endpoint/search, which
declares `security: [{bearerAuth: []}]` with `scheme: bearer`). There is no
`api_key` body-field fallback in the current spec.

Query shape: `include_domains` (array of domain strings) restricts results to
linkedin.com, and `search_depth: "basic"` keeps cost at 1 credit/request
(both parameter names/values confirmed against the same spec).
"""

from dataclasses import dataclass

import httpx

from app.config import load_config

DEFAULT_TIMEOUT = 10.0
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
LINKEDIN_DOMAIN = "linkedin.com"


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    snippet: str


@dataclass(frozen=True)
class SearchResult:
    candidates: list[Candidate]
    query_used: str | None
    reason: str | None

# Preferred ordering for assembling the query string; any other selected
# field (e.g. a future standard field) is appended afterwards in whatever
# order the caller's dict provides it.
_FIELD_ORDER = ("name", "company", "title", "email")

# Fields kept in the broadened (attempt 2) fallback query.
_FALLBACK_FIELDS = ("name", "company")


def _build_query(fields: dict[str, str]) -> str:
    ordered_keys = [key for key in _FIELD_ORDER if key in fields]
    ordered_keys += [key for key in fields if key not in _FIELD_ORDER]
    parts = [f'"{fields[key].strip()}"' for key in ordered_keys if fields.get(key, "").strip()]
    return " ".join(parts)


class TavilyClient:
    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
        base_url: str = TAVILY_SEARCH_URL,
    ):
        self._api_key = api_key if api_key is not None else load_config().tavily_api_key
        self._base_url = base_url
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TavilyClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _search(self, query: str) -> list[Candidate]:
        response = self._client.post(
            self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "query": query,
                "search_depth": "basic",
                "include_domains": [LINKEDIN_DOMAIN],
            },
        )
        response.raise_for_status()
        payload = response.json()
        return [
            Candidate(url=item["url"], title=item.get("title", ""), snippet=item.get("content", ""))
            for item in payload.get("results", [])
            if item.get("url")
        ]

    def find_linkedin_candidates(self, fields: dict[str, str]) -> SearchResult:
        """Two-step lookup.

        Attempt 1 uses every provided field value. Attempt 2 (only fired if
        attempt 1 comes back empty) drops title/email and keeps only
        name/company, if either was in the original field set — otherwise
        there is no fallback and the empty attempt-1 result is returned as-is.
        """
        clean_fields = {key: value for key, value in fields.items() if value and value.strip()}

        primary_query = _build_query(clean_fields)
        if not primary_query:
            return SearchResult(candidates=[], query_used=None, reason="no search field values provided")

        candidates = self._search(primary_query)
        if candidates:
            return SearchResult(candidates=candidates, query_used=primary_query, reason=None)

        fallback_fields = {key: value for key, value in clean_fields.items() if key in _FALLBACK_FIELDS}
        if not fallback_fields:
            return SearchResult(
                candidates=[],
                query_used=None,
                reason="no candidates found after 1 query attempt",
            )

        fallback_query = _build_query(fallback_fields)
        candidates = self._search(fallback_query)
        if candidates:
            return SearchResult(candidates=candidates, query_used=fallback_query, reason=None)

        return SearchResult(
            candidates=[],
            query_used=None,
            reason="no candidates found after 2 query attempts",
        )
