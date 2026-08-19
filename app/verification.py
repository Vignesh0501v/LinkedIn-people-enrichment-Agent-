"""Groq candidate-verification client -- Groq's "job 2" from the TRD.

Groq's only role here is to *judge* a search candidate already retrieved by
the deterministic `app.tavily_search.TavilyClient` and already scored by
`app.scoring.score_candidate` -- it never decides what to search for, never
calls Tavily, and never fetches a URL. One call in (the candidate's text plus
the row's selected-field values), one JSON payload out
(`is_match`/`confidence`/`city`/`reason`), then Python validates it. See
doc/TRD_LinkedIn_Enrichment_Pipeline.md ("Groq's two jobs", job 2) and
doc/PLAN_LinkedIn_Enrichment_Pipeline.md unit #18 for the design this module
implements.

This module deliberately reuses `app.intent_extraction.GroqClient` rather
than duplicating the HTTP/auth/JSON-mode plumbing -- same live-verified API
contract (endpoint, bearer auth, `response_format: {"type": "json_object"}`,
model `openai/gpt-oss-20b`; see that module's docstring for the full
justification, including why the TRD's original `llama-3.1-8b-instant` was
replaced), just called here with a different system/user prompt for a
different judgment task. Since unit #24 that client is LangChain-backed and
its prompt text lives in `app/prompts.json` under the `"verification"` job
-- same relocation as job 1's prompts, same wording as before.
"""

import json
from dataclasses import dataclass
from typing import Any

from app.intent_extraction import GroqClient, load_prompts
from app.mapping import FieldMapping, value_for_field
from app.tavily_search import Candidate


@dataclass(frozen=True)
class VerificationResult:
    is_match: bool
    confidence: float
    city: str | None
    reason: str
    resolved: bool = True
    """False only when Groq's output stayed malformed after the retry (the
    fail-safe path) -- i.e. we genuinely don't know, as opposed to Groq
    confidently judging `is_match=False`. Callers should treat an unresolved
    result differently from a real negative judgment (see `app.batch`'s
    verification-routing logic): a real "no" can safely become NOT_FOUND,
    but "we couldn't get an answer" should stay flagged for human review.
    """


def _build_user_message(candidate: Candidate, row_field_values: dict[str, str]) -> str:
    return (
        f"Candidate title: {json.dumps(candidate.title)}\n"
        f"Candidate snippet: {json.dumps(candidate.snippet)}\n"
        f"Row field values: {json.dumps(row_field_values)}\n\n"
        "Return the JSON object described in the system message now."
    )

def _try_parse(raw: str) -> dict[str, Any] | None:
    """Parse `raw` and check it has the required top-level JSON shape.

    Returns None (rather than raising) for anything malformed -- the caller
    decides whether that means "retry" or "fail safe". Matches
    `app.intent_extraction._try_parse`'s structural-validation-only
    discipline: type/range clamping of individual fields happens afterwards
    in `_sanitize_result`, not here.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("is_match"), bool):
        return None
    if not isinstance(parsed.get("confidence"), (int, float)) or isinstance(parsed.get("confidence"), bool):
        return None
    city = parsed.get("city")
    if city is not None and not isinstance(city, str):
        return None
    if not isinstance(parsed.get("reason"), str):
        return None
    return parsed


def _sanitize_result(parsed: dict[str, Any]) -> VerificationResult:
    """Turn a structurally-valid-but-untrusted JSON payload into a
    `VerificationResult`, clamping `confidence` into `[0.0, 1.0]` rather
    than trusting the model's claim blindly.
    """
    confidence = float(parsed["confidence"])
    confidence = max(0.0, min(1.0, confidence))

    return VerificationResult(
        is_match=parsed["is_match"],
        confidence=confidence,
        city=parsed.get("city"),
        reason=parsed["reason"],
    )


def _fail_safe_result() -> VerificationResult:
    """No confident judgment could be obtained -- fail safe to "not a
    match, no confidence" so a caller's confidence-routing treats the row
    as unresolved rather than trusting a fabricated judgment (same
    retry-then-fail-safe discipline as `app.intent_extraction`, per the
    TRD's FR-08 principle).
    """
    return VerificationResult(
        is_match=False,
        confidence=0.0,
        city=None,
        resolved=False,
        reason="verification failed — treat as unresolved",
    )


def verify_candidate(
    candidate: Candidate,
    row_values: dict[str, str],
    selected_fields: set[str],
    mappings: list[FieldMapping],
    client: GroqClient | None = None,
) -> VerificationResult:
    """Ask Groq to judge whether `candidate` matches the row's selected
    field values; retry once on malformed output; fail safe if it's still
    malformed.

    Never raises on malformed/invalid LLM output. Genuine transport/HTTP
    failures (timeout, non-2xx, etc.) still propagate, matching
    `app.intent_extraction.GroqClient.propose_mapping`'s distinction
    between "bad LLM output" (fail safe) and "infra failure" (propagate).

    Opens (and closes) its own `GroqClient` if one isn't injected -- tests
    and callers that need to control the transport pass their own, mirroring
    `app.intent_extraction.propose_mapping`'s call-in-call-out shape.
    """
    owns_client = client is None
    active_client = client if client is not None else GroqClient()
    try:
        prompts = load_prompts()["verification"]
        row_field_values = {field: value_for_field(row_values, field, mappings) for field in selected_fields}
        user_message = _build_user_message(candidate, row_field_values)

        raw = active_client.complete(prompts["system"], user_message)
        parsed = _try_parse(raw)

        if parsed is None:
            raw = active_client.complete(
                prompts["system"],
                user_message,
                followup=[
                    {"role": "system", "content": raw},
                    {"role": "user", "content": prompts["retry"]},
                ],
            )
            parsed = _try_parse(raw)

        if parsed is None:
            return _fail_safe_result()

        return _sanitize_result(parsed)
    finally:
        if owns_client:
            active_client.close()
