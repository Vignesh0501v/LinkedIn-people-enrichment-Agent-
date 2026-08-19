"""Groq intent-extraction client -- Groq's "job 1" from the TRD.

Groq's only role here is to *propose* a `FieldMapping`/`SearchCriteria`
selection from the user's raw instructions, detected source columns, and a
few sample row values -- as strict JSON, which we validate before an
operator ever sees or confirms it. Groq is never given a tool to call Tavily
or fetch a URL, and this module never lets it initiate a second action on
its own: one call in, one JSON payload out, then Python validates it. See
doc/TRD_LinkedIn_Enrichment_Pipeline.md ("Groq's two jobs") for the boundary
this implements. NFR-07 still holds after the LangChain swap below: no
tools are bound to the model, no agent/executor loop exists, and the model
never decides *when* to call anything -- LangChain is used purely as a
client + prompt-template + message-plumbing library.

API contract (verified live, not assumed, same discipline as
app/tavily_search.py's header comment):

- Endpoint + auth: `POST https://api.groq.com/openai/v1/chat/completions`
  with `Authorization: Bearer <GROQ_API_KEY>`. Confirmed by making an
  unauthenticated request against the live endpoint on 2026-08-19, which
  returned `{"error": {"message": "Invalid API Key", "type":
  "invalid_request_error", "code": "invalid_api_key"}}` -- i.e. the endpoint
  exists and expects bearer-token auth, matching the OpenAI-compatible
  chat-completions contract described at
  https://console.groq.com/docs/api-reference. As of unit #24 that request
  is issued by `langchain_groq.ChatGroq` (which wraps the official `groq`
  SDK) rather than by a hand-rolled `httpx.Client.post`, but it is the same
  endpoint, the same bearer auth, and the same JSON body.
- JSON enforcement: Groq's `response_format` supports two mechanisms per
  https://console.groq.com/docs/api-reference and
  https://console.groq.com/docs/structured-outputs --
  `{"type": "json_schema", "json_schema": {...}}` ("Structured Outputs",
  schema-guaranteed) and `{"type": "json_object"}` ("JSON Object Mode",
  syntactically-valid-JSON-guaranteed but *not* schema-guaranteed). This
  client uses `{"type": "json_object"}` and then validates the parsed JSON
  against our own schema in Python -- which we have to do anyway per the
  TRD, since json_object mode only guarantees valid JSON syntax, not our
  field shapes.
- Model: `openai/gpt-oss-20b` (changed from the TRD's original
  `llama-3.1-8b-instant` on 2026-08-19 -- live-verified that
  `llama-3.1-8b-instant` has been fully removed from Groq's model catalog,
  returning `404 model_not_found` on every request, not merely deprecated
  or rate-limited. Queried `GET /openai/v1/models` live: no Llama chat model
  remains in Groq's catalog at all. `openai/gpt-oss-20b` was chosen as the
  closest replacement in role (small, fast, open-weight) and is also one of
  the models on Groq's `json_schema` "Structured Outputs" supported-model
  list per https://console.groq.com/docs/structured-outputs#supported-models
  -- json_object mode is kept for now rather than upgrading to json_schema,
  to keep this a minimal, verified fix rather than bundling in an unrelated
  enhancement.)

Error discipline (important, and easy to get wrong after the LangChain
swap): the `groq` SDK raises `groq.APIError` subclasses
(`APIConnectionError`, `APIStatusError`, ...) for genuine transport/HTTP
failures. Those are *not* `httpx.HTTPError` subclasses, so callers that
previously caught `httpx.HTTPError` around these methods must catch
`GROQ_TRANSPORT_ERRORS` instead (see `app/api.py`'s `create_turn`). Bad LLM
*output* still never raises -- it retries once, then fails safe.

Prompts live in `app/prompts.json` (unit #24) rather than as Python string
constants, so prompt wording can be reviewed and tuned without touching
control flow. `<<standard_fields>>`/`<<selectable_criteria_fields>>` in that
file are substituted at load time from `app.mapping`'s real constants --
angle-bracket markers rather than `str.format` placeholders because the
prompt bodies themselves contain literal `{`/`}` (they describe a JSON
schema), which `str.format` would choke on.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import groq
import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_groq import ChatGroq

from app.config import load_config
from app.mapping import SELECTABLE_CRITERIA_FIELDS, STANDARD_FIELDS, FieldMapping

DEFAULT_TIMEOUT = 15.0
GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "openai/gpt-oss-20b"

PROMPTS_PATH = Path(__file__).with_name("prompts.json")

# What a caller should catch to mean "the Groq call failed for infra reasons"
# -- `httpx.HTTPError` is kept alongside `groq.APIError` because the SDK's
# custom `http_client` can still surface raw httpx errors in some paths (and
# because dropping it would silently narrow every existing except clause).
GROQ_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (groq.APIError, httpx.HTTPError)

VALID_INTENTS: set[str] = {"data_request", "greeting"}
VALID_OUTPUT_FORMATS: set[str] = {"table", "excel"}

DEFAULT_GREETING_REPLY = (
    "Hi! I look up LinkedIn profiles for you -- type a request like "
    '"find Jane Doe at Acme", paste a table of names, or upload a '
    "spreadsheet and I'll take it from there."
)

_prompts_cache: dict[str, dict[str, str]] | None = None


def load_prompts() -> dict[str, dict[str, str]]:
    """Prompt text from `app/prompts.json`, keyed by job then by role
    (`"system"`/`"retry"`), with the field-catalog markers substituted.
    Cached after the first read -- the file never changes at runtime."""
    global _prompts_cache
    if _prompts_cache is None:
        substitutions = {
            "<<standard_fields>>": str(STANDARD_FIELDS),
            "<<selectable_criteria_fields>>": str(sorted(SELECTABLE_CRITERIA_FIELDS)),
        }
        raw = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
        resolved: dict[str, dict[str, str]] = {}
        for job, texts in raw.items():
            resolved[job] = {}
            for name, text in texts.items():
                for marker, value in substitutions.items():
                    text = text.replace(marker, value)
                resolved[job][name] = text
        _prompts_cache = resolved
    return _prompts_cache


# One shared prompt shape for every job in this codebase: a system message,
# optional prior-conversation context (unit #25's 5-turn window), the actual
# request, and an optional follow-up exchange (used only for the retry).
# The prompt bodies are passed *as variables* rather than baked into the
# template text on purpose -- they contain literal JSON braces, which
# LangChain's f-string templating would otherwise read as variable slots.
_CHAT_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", "{system_prompt}"),
        MessagesPlaceholder("context"),
        ("human", "{user_message}"),
        MessagesPlaceholder("followup"),
    ]
)


def _to_messages(entries: list[dict[str, str]] | None) -> list[BaseMessage]:
    """Convert this repo's plain `{"role", "content"}` dicts into LangChain
    messages. Anything that isn't the user speaking is treated as the
    assistant, which is what a `system`-role Turn actually is from the
    model's point of view."""
    messages: list[BaseMessage] = []
    for entry in entries or []:
        content = entry.get("content") or ""
        if entry.get("role") == "user":
            messages.append(HumanMessage(content=content))
        else:
            messages.append(AIMessage(content=content))
    return messages


def _message_text(content: Any) -> str:
    """Flatten a LangChain message's content to plain text. Normally a
    string; some providers return a list of content blocks, so this handles
    that shape rather than stringifying a list into unparseable JSON."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        ]
        return "".join(parts)
    return str(content)


@dataclass(frozen=True)
class MappingProposal:
    field_mappings: list[FieldMapping]
    selected_fields: set[str]
    # Unit #25. `intent` is always "data_request" on this path (a turn that
    # arrives with a file or a pasted table is unambiguously a data request,
    # whatever the accompanying text says), and is carried here only so the
    # two Groq jobs share one schema shape.
    intent: str = "data_request"
    output_format: str = "table"


@dataclass(frozen=True)
class SingleLookupExtraction:
    """The full result of the single-lookup extraction job (unit #25).

    `extract_single_lookup_fields` still returns just `dict[str, str]` for
    the callers that only want the field values; this richer shape is what
    `app/api.py` uses to decide between "run the pipeline" and "reply to a
    greeting".
    """

    fields: dict[str, str]
    intent: str = "data_request"
    output_format: str = "table"
    reply: str = ""


def _build_user_message(
    instructions_text: str, source_columns: list[str], sample_rows: list[dict]
) -> str:
    return (
        f"User instructions: {json.dumps(instructions_text)}\n"
        f"Source columns: {json.dumps(source_columns)}\n"
        f"Sample rows: {json.dumps(sample_rows)}\n\n"
        "Return the JSON object described in the system message now."
    )


def _try_parse(raw: str) -> dict[str, Any] | None:
    """Parse `raw` and check it has the required top-level JSON shape.

    Returns None (rather than raising) for anything malformed -- the caller
    decides whether that means "retry" or "fail safe".

    `intent`/`output_format` are deliberately *not* required here: they were
    added in unit #25 and both have safe defaults, so a reply that's missing
    them is still a usable mapping proposal and shouldn't cost a retry.
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("field_mappings"), list):
        return None
    if not isinstance(parsed.get("selected_fields"), list):
        return None
    return parsed


def _read_intent(parsed: dict[str, Any], default: str = "data_request") -> str:
    value = parsed.get("intent")
    return value if isinstance(value, str) and value in VALID_INTENTS else default


def _read_output_format(parsed: dict[str, Any]) -> str:
    value = parsed.get("output_format")
    return value if isinstance(value, str) and value in VALID_OUTPUT_FORMATS else "table"


def _sanitize_proposal(parsed: dict[str, Any], source_columns: list[str]) -> MappingProposal:
    """Turn a structurally-valid-but-untrusted JSON payload into a
    `MappingProposal`, dropping/unmapping anything that doesn't check out
    against `app.mapping.STANDARD_FIELDS` or the actual `source_columns` --
    never trusting the model's claim that a column exists.
    """
    proposed_by_field: dict[str, str | None] = {}
    for entry in parsed["field_mappings"]:
        if not isinstance(entry, dict):
            continue
        standard_field = entry.get("standard_field")
        if standard_field not in STANDARD_FIELDS:
            continue
        source_column = entry.get("source_column")
        if source_column is not None and source_column not in source_columns:
            # Hallucinated column name -- fail safe to unmapped rather than
            # trusting an exact-match claim the model made on its own.
            source_column = None
        proposed_by_field[standard_field] = source_column

    field_mappings = [
        FieldMapping(standard_field=field, source_column=proposed_by_field.get(field))
        for field in STANDARD_FIELDS
    ]

    selected_fields = {
        value
        for value in parsed["selected_fields"]
        if isinstance(value, str) and value in SELECTABLE_CRITERIA_FIELDS
    }

    return MappingProposal(
        field_mappings=field_mappings,
        selected_fields=selected_fields,
        # A file/paste turn is a data request by construction -- the model
        # doesn't get a vote on that (see the dataclass comment).
        intent="data_request",
        output_format=_read_output_format(parsed),
    )


def _fail_safe_proposal() -> MappingProposal:
    """Nothing mapped, nothing selected -- the operator still confirms
    everything anyway, so failing safe beats failing loud (per the TRD's
    retry discipline, shared with the Phase 3 verification job).
    """
    return MappingProposal(
        field_mappings=[FieldMapping(standard_field=field, source_column=None) for field in STANDARD_FIELDS],
        selected_fields=set(),
    )


def _build_single_lookup_user_message(instructions_text: str) -> str:
    return (
        f"Request: {json.dumps(instructions_text)}\n\n"
        "Return the JSON object described in the system message now."
    )


def _try_parse_single_lookup(raw: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("fields"), dict):
        return None
    return parsed


def _sanitize_single_lookup_fields(parsed: dict[str, Any]) -> dict[str, str]:
    """Keep only entries whose key is a real standard field and whose value
    is non-empty text -- never trust the model's key choice or an empty/
    non-string value blindly."""
    fields = parsed["fields"]
    sanitized: dict[str, str] = {}
    for key, value in fields.items():
        if key not in STANDARD_FIELDS:
            continue
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text:
            sanitized[key] = text
    return sanitized


def _sanitize_single_lookup(parsed: dict[str, Any]) -> SingleLookupExtraction:
    fields = _sanitize_single_lookup_fields(parsed)
    intent = _read_intent(parsed)
    reply = parsed.get("reply")
    reply = reply.strip() if isinstance(reply, str) else ""
    if intent == "greeting" and not reply:
        reply = DEFAULT_GREETING_REPLY
    return SingleLookupExtraction(
        fields=fields,
        intent=intent,
        output_format=_read_output_format(parsed),
        reply=reply,
    )


class GroqClient:
    """A thin, non-agentic wrapper over `langchain_groq.ChatGroq`.

    `http_client` is the test seam (replacing the pre-unit-#24 `transport`
    param): the `groq` SDK forwards it straight to its own transport layer,
    so `GroqClient(http_client=httpx.Client(transport=httpx.MockTransport(...)))`
    gives unit tests full control of the wire without any real network call
    -- same intent as `TavilyClient`'s `transport` param, one level up the
    stack because that's where the SDK exposes injection.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: "httpx.Client | None" = None,
        base_url: str | None = None,
        model: str = MODEL,
        chat_model: "ChatGroq | None" = None,
    ):
        self._api_key = api_key if api_key is not None else load_config().groq_api_key
        self._model = model
        self._http_client = http_client
        # An unset key becomes a placeholder rather than None/"": the groq
        # SDK rejects both at *construction* time, which would turn a missing
        # env var into an import-time crash (`app.api` builds a default
        # GroqClient at module scope, so the module would stop importing at
        # all). A placeholder defers the failure to call time, where it
        # surfaces as a 401 -- exactly the pre-unit-#24 behavior, and a
        # `groq.AuthenticationError` that callers already handle as a
        # transport error.
        self._chat = chat_model if chat_model is not None else ChatGroq(
            model=model,
            api_key=self._api_key or "missing-groq-api-key",
            base_url=base_url,
            temperature=0,
            timeout=timeout,
            # The SDK retries 5xx/connection errors by default; this client
            # has its own explicit retry-once-on-malformed-output discipline
            # and callers treat infra errors as fatal, so keep the two from
            # compounding.
            max_retries=0,
            http_client=http_client,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()

    def __enter__(self) -> "GroqClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        context: list[dict[str, str]] | None = None,
        followup: list[dict[str, str]] | None = None,
    ) -> str:
        """One chat completion, returned as raw text for the caller to parse.

        `context` is optional prior-conversation history (unit #25's bounded
        window); `followup` is the assistant-reply-plus-correction pair used
        by the retry path. Raises `GROQ_TRANSPORT_ERRORS` on infra failure;
        never inspects or validates the model's content.
        """
        messages = _CHAT_PROMPT.format_messages(
            system_prompt=system_prompt,
            user_message=user_message,
            context=_to_messages(context),
            followup=_to_messages(followup),
        )
        response = self._chat.invoke(messages)
        return _message_text(response.content)

    def propose_mapping(
        self,
        instructions_text: str,
        source_columns: list[str],
        sample_rows: list[dict],
        context: list[dict[str, str]] | None = None,
    ) -> MappingProposal:
        """Call Groq once for a proposed mapping; retry once on malformed
        output; fail safe (everything unmapped) if it's still malformed.

        Never raises on malformed/invalid LLM output -- this is only a
        proposal the operator confirms anyway. Genuine transport/HTTP
        failures (timeout, non-2xx, etc.) still propagate as
        `GROQ_TRANSPORT_ERRORS`, matching TavilyClient's discipline of not
        swallowing infra errors.

        `context` (unit #25) is the session's last few turns, rendered as
        prior conversation so short follow-ups resolve against what was
        already discussed.
        """
        prompts = load_prompts()["intent_extraction"]
        user_message = _build_user_message(instructions_text, source_columns, sample_rows)

        raw = self.complete(prompts["system"], user_message, context=context)
        parsed = _try_parse(raw)

        if parsed is None:
            raw = self.complete(
                prompts["system"],
                user_message,
                context=context,
                followup=[{"role": "system", "content": raw}, {"role": "user", "content": prompts["retry"]}],
            )
            parsed = _try_parse(raw)

        if parsed is None:
            return _fail_safe_proposal()

        return _sanitize_proposal(parsed, source_columns)

    def extract_single_lookup(
        self, instructions_text: str, context: list[dict[str, str]] | None = None
    ) -> SingleLookupExtraction:
        """The full single-lookup extraction: field values plus unit #25's
        `intent`/`output_format`/`reply`. Same retry-once-then-fail-safe
        discipline as `propose_mapping`; the fail-safe result is an empty
        extraction treated as a data request, so the operator fills fields in
        by hand rather than the turn being silently misrouted to a greeting.
        """
        prompts = load_prompts()["single_lookup_extraction"]
        user_message = _build_single_lookup_user_message(instructions_text)

        raw = self.complete(prompts["system"], user_message, context=context)
        parsed = _try_parse_single_lookup(raw)

        if parsed is None:
            raw = self.complete(
                prompts["system"],
                user_message,
                context=context,
                followup=[{"role": "system", "content": raw}, {"role": "user", "content": prompts["retry"]}],
            )
            parsed = _try_parse_single_lookup(raw)

        if parsed is None:
            return SingleLookupExtraction(fields={})

        return _sanitize_single_lookup(parsed)

    def extract_single_lookup_fields(
        self, instructions_text: str, context: list[dict[str, str]] | None = None
    ) -> dict[str, str]:
        """Extract field *values* (not a column mapping) directly from a
        single-lookup free-text request, e.g. "find Jane Doe at Acme" ->
        `{"full_name": "Jane Doe", "company": "Acme"}`. A single_lookup turn
        has no source columns to map, so `propose_mapping`'s schema doesn't
        apply -- this is the extraction job that makes single_lookup
        actually reach `run_batch` instead of having no fields anything can
        ever map to. Same retry-once-then-fail-safe discipline; failing safe
        here returns an empty dict (nothing extracted) rather than raising,
        so the operator fills fields in by hand instead of the batch being
        silently unusable.
        """
        return self.extract_single_lookup(instructions_text, context=context).fields


def propose_mapping(
    instructions_text: str,
    source_columns: list[str],
    sample_rows: list[dict],
    client: GroqClient | None = None,
    context: list[dict[str, str]] | None = None,
) -> MappingProposal:
    """Module-level convenience wrapper matching this repo's call-in-call-out
    shape. Opens (and closes) its own `GroqClient` if one isn't injected --
    tests and callers that need to control the transport pass their own.
    """
    owns_client = client is None
    active_client = client if client is not None else GroqClient()
    try:
        return active_client.propose_mapping(instructions_text, source_columns, sample_rows, context=context)
    finally:
        if owns_client:
            active_client.close()


def extract_single_lookup_fields(
    instructions_text: str,
    client: GroqClient | None = None,
    context: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    """Module-level convenience wrapper, same call-in-call-out shape as
    `propose_mapping`."""
    owns_client = client is None
    active_client = client if client is not None else GroqClient()
    try:
        return active_client.extract_single_lookup_fields(instructions_text, context=context)
    finally:
        if owns_client:
            active_client.close()


def extract_single_lookup(
    instructions_text: str,
    client: GroqClient | None = None,
    context: list[dict[str, str]] | None = None,
) -> SingleLookupExtraction:
    """Module-level convenience wrapper around `GroqClient.extract_single_lookup`
    (unit #25's richer result), same call-in-call-out shape as its
    fields-only sibling."""
    owns_client = client is None
    active_client = client if client is not None else GroqClient()
    try:
        return active_client.extract_single_lookup(instructions_text, context=context)
    finally:
        if owns_client:
            active_client.close()
