// Types mirroring the FastAPI contract in app/api.py — see that file for
// ground truth. Field names/shapes here are kept 1:1 with the JSON the
// backend actually returns (snake_case, matching the wire format) rather
// than translated to a client-side naming convention, to avoid drift.

export const STANDARD_FIELDS = [
  "first_name",
  "middle_name",
  "last_name",
  "full_name",
  "company",
  "title",
  "email",
] as const;
export type StandardField = (typeof STANDARD_FIELDS)[number];

// The closed set of logical criteria fields selectable in
// PUT .../criteria — see app/mapping.py SELECTABLE_CRITERIA_FIELDS.
export const SELECTABLE_CRITERIA_FIELDS = ["name", "company", "title", "email"] as const;
export type SelectableCriteriaField = (typeof SELECTABLE_CRITERIA_FIELDS)[number];

export const VALID_MATCH_STATUSES = [
  "VERIFIED",
  "REVIEW_REQUIRED",
  "NOT_FOUND",
  "MISSING_SEARCH_FIELD",
] as const;
export type MatchStatus = (typeof VALID_MATCH_STATUSES)[number];

export type BatchStatus =
  | "pending"
  | "mapping_proposed"
  | "mapping_confirmed"
  | "running"
  | "completed"
  | "failed";

// Whether a finished batch is meant to be read inline in the chat or taken
// away as an .xlsx — decided by Groq from the user's phrasing, then capped
// server-side by row count (app/api.py MAX_INLINE_TABLE_ROWS).
export type OutputFormat = "table" | "excel";

export interface FieldMapping {
  standard_field: string;
  source_column: string | null;
}

export interface MappingProposalPayload {
  batch_id: string;
  field_mappings: FieldMapping[];
  selected_fields: string[];
  // Added in Plan unit #23 — these ride along on the stored turn payload so a
  // reopened session can rebuild the batch card straight from history.
  columns: string[];
  sample_rows: Record<string, string>[];
  output_format: OutputFormat;
}

/** POST .../turns when the mapping was confident enough to confirm and run
 * without ever showing a mapping/criteria editor — the auto-mapping flow
 * that replaced the old two-step manual confirmation. */
export interface DataRequestTurnResponse {
  intent: "data_request";
  batch_id: string;
  status: "running";
  columns: string[];
  sample_rows: Record<string, string>[];
  output_format: OutputFormat;
  field_mappings: FieldMapping[];
  selected_fields: string[];
  search_mode: "person" | "company" | null;
}

/** POST .../turns when Groq classified the message as a greeting/chitchat —
 * no batch is created, there is nothing to map, only a reply to render. */
export interface GreetingTurnResponse {
  intent: "greeting";
  batch_id: null;
  reply: string;
  turn_index: number;
}

/** POST .../turns when auto-mapping wasn't confident enough to run on its
 * own — no batch (for a single lookup) or a batch left pending (for a
 * file/paste whose columns couldn't be confidently mapped). The next
 * plain-text turn in this session is treated as the answer. */
export interface ClarificationNeededTurnResponse {
  intent: "clarification_needed";
  batch_id: string | null;
  question: string;
}

export type CreateTurnResponse =
  | DataRequestTurnResponse
  | GreetingTurnResponse
  | ClarificationNeededTurnResponse;

export function isGreetingTurn(turn: CreateTurnResponse): turn is GreetingTurnResponse {
  return turn.intent === "greeting";
}

export function isClarificationTurn(turn: CreateTurnResponse): turn is ClarificationNeededTurnResponse {
  return turn.intent === "clarification_needed";
}

export interface ConfirmMappingResponse {
  batch_id: string;
  field_mappings: FieldMapping[];
}

export interface ApplyTemplateResponse {
  batch_id: string;
  field_mappings: FieldMapping[];
  unmatched_fields: string[];
}

export interface MappingTemplate {
  id: string;
  source_label: string;
  field_mappings: Record<string, string>;
}

export interface BatchPayload {
  batch_id: string;
  session_id: string;
  input_kind: "file" | "paste" | "single_lookup";
  uploaded_at: string;
  status: BatchStatus;
  row_count: number;
  search_mode: "person" | "company" | null;
  output_format: OutputFormat;
}

export interface BatchStatusResponse extends BatchPayload {
  rows_completed: number;
  counts_by_status: Record<string, number>;
}

export interface RowPayload {
  batch_id: string;
  row_index: number;
  field_values: Record<string, string>;
  linkedin_url: string | null;
  city: string | null;
  match_confidence: number | null;
  match_status: MatchStatus;
  source_reason: string | null;
  query_variant_used: string | null;
}

// The turn kinds app/session.py persists. Anything else is rendered as a
// generic system note rather than dropped, so a future backend kind still
// shows up in a replayed thread.
export type TurnKind =
  | "instruction_text"
  | "file_upload"
  | "pasted_table"
  | "greeting_reply"
  | "mapping_proposal"
  | "mapping_confirmed"
  | "clarification_question";

export interface SessionTurn {
  turn_index: number;
  role: "user" | "system";
  kind: TurnKind | string;
  payload: Record<string, unknown>;
}

export interface SessionPayload {
  session_id: string;
  created_at: string;
  last_active_at: string;
  turns: SessionTurn[];
}

/** One sidebar row from GET /sessions (newest last_active_at first). */
export interface SessionSummary {
  session_id: string;
  created_at: string;
  last_active_at: string;
  first_turn_preview: string;
  turn_count: number;
}
