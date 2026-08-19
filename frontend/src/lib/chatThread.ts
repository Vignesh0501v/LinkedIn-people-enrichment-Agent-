// The chat thread's message model, plus the replay that rebuilds it from a
// stored session's turn history (GET /sessions/{id}).
//
// Replay is deliberately a *reconstruction*, not a transcript dump: a stored
// `mapping_proposal` turn carries everything the live proposal response
// carried (Plan unit #23 added `columns`/`sample_rows`/`output_format` to the
// payload), so a reopened session can put a working MappingEditor back on
// screen and let the user finish a mapping they abandoned last visit. The
// follow-up `mapping_confirmed` turns are folded *into* that batch's card
// rather than rendered as separate bubbles, because they were never separate
// bubbles in the live thread either.

import type { FieldMapping, OutputFormat, SessionPayload, SessionTurn } from "../api/types";

/** Everything BatchCard needs to draw a batch, live or replayed. */
export interface BatchCardData {
  batchId: string;
  columns: string[];
  sampleRows: Record<string, string>[];
  fieldMappings: FieldMapping[];
  selectedFields: string[];
  outputFormat: OutputFormat;
}

/** What history tells us already happened to a replayed batch. BatchCard
 * still confirms against `GET .../batches/{id}` before deciding a stage —
 * turns record what the *user* did, the batch record knows whether the run
 * since finished, failed, or is still going. */
export interface BatchResume {
  confirmedMappings: FieldMapping[] | null;
  confirmedFields: string[] | null;
}

export type ChatMessage =
  | { id: string; role: "user"; kind: "text"; text: string }
  | { id: string; role: "system"; kind: "batch"; batch: BatchCardData; resume?: BatchResume }
  | { id: string; role: "system"; kind: "reply"; text: string }
  | { id: string; role: "system"; kind: "error"; text: string }
  | { id: string; role: "system"; kind: "info"; text: string };

let nextId = 0;
export function newMessageId(): string {
  nextId += 1;
  return `msg-${nextId}`;
}

function asString(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === "string" ? value : "";
}

function asNumber(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key];
  return typeof value === "number" ? value : null;
}

function asStringList(payload: Record<string, unknown>, key: string): string[] {
  const value = payload[key];
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function asFieldMappings(payload: Record<string, unknown>, key: string): FieldMapping[] | null {
  const value = payload[key];
  if (!Array.isArray(value)) return null;
  return value
    .filter((entry): entry is Record<string, unknown> => typeof entry === "object" && entry !== null)
    .map((entry) => ({
      standard_field: String(entry.standard_field ?? ""),
      source_column: typeof entry.source_column === "string" ? entry.source_column : null,
    }))
    .filter((m) => m.standard_field !== "");
}

function asSampleRows(payload: Record<string, unknown>): Record<string, string>[] {
  const value = payload.sample_rows;
  if (!Array.isArray(value)) return [];
  return value
    .filter((row): row is Record<string, unknown> => typeof row === "object" && row !== null)
    .map((row) => {
      const out: Record<string, string> = {};
      for (const [k, v] of Object.entries(row)) out[k] = v == null ? "" : String(v);
      return out;
    });
}

function asOutputFormat(payload: Record<string, unknown>): OutputFormat {
  return payload.output_format === "excel" ? "excel" : "table";
}

function userTurnText(turn: SessionTurn): string {
  const payload = turn.payload ?? {};
  const instructions = asString(payload, "instructions_text");
  if (turn.kind === "instruction_text") return asString(payload, "text") || "(empty)";
  if (turn.kind === "file_upload") {
    const rowCount = asNumber(payload, "row_count");
    const name = asString(payload, "filename") || "spreadsheet";
    const header = `Uploaded file: ${name}${rowCount != null ? ` (${rowCount} rows)` : ""}`;
    return instructions ? `${header}\n${instructions}` : header;
  }
  if (turn.kind === "pasted_table") {
    const rowCount = asNumber(payload, "row_count");
    const header = `Pasted table${rowCount != null ? `: ${rowCount} rows` : ""}`;
    return instructions ? `${header}\n${instructions}` : header;
  }
  return `(${turn.kind})`;
}

/**
 * Rebuild the thread for a stored session. Message ids are derived from the
 * session + turn index so a re-open produces stable React keys instead of
 * ever-incrementing ones.
 */
export function replaySession(session: SessionPayload): ChatMessage[] {
  // First pass: what the user later confirmed, keyed by batch. Both
  // confirm-mapping and confirm-criteria write `kind: "mapping_confirmed"`
  // turns; they're told apart by which payload key they carry.
  const resumeByBatch = new Map<string, BatchResume>();
  for (const turn of session.turns) {
    if (turn.kind !== "mapping_confirmed") continue;
    const payload = turn.payload ?? {};
    const batchId = asString(payload, "batch_id");
    if (!batchId) continue;
    const current = resumeByBatch.get(batchId) ?? { confirmedMappings: null, confirmedFields: null };
    const mappings = asFieldMappings(payload, "field_mappings");
    if (mappings && mappings.length > 0) current.confirmedMappings = mappings;
    if (Array.isArray(payload.selected_fields)) {
      current.confirmedFields = asStringList(payload, "selected_fields");
    }
    resumeByBatch.set(batchId, current);
  }

  const messages: ChatMessage[] = [];
  for (const turn of session.turns) {
    const id = `${session.session_id}-${turn.turn_index}`;
    const payload = turn.payload ?? {};

    if (turn.kind === "mapping_confirmed") continue; // folded into the batch card

    if (turn.kind === "mapping_proposal") {
      const batchId = asString(payload, "batch_id");
      if (!batchId) continue;
      messages.push({
        id,
        role: "system",
        kind: "batch",
        batch: {
          batchId,
          columns: asStringList(payload, "columns"),
          sampleRows: asSampleRows(payload),
          fieldMappings: asFieldMappings(payload, "field_mappings") ?? [],
          selectedFields: asStringList(payload, "selected_fields"),
          outputFormat: asOutputFormat(payload),
        },
        resume: resumeByBatch.get(batchId) ?? { confirmedMappings: null, confirmedFields: null },
      });
      continue;
    }

    if (turn.kind === "greeting_reply") {
      messages.push({ id, role: "system", kind: "reply", text: asString(payload, "text") });
      continue;
    }

    if (turn.role === "user") {
      messages.push({ id, role: "user", kind: "text", text: userTurnText(turn) });
      continue;
    }

    messages.push({ id, role: "system", kind: "info", text: `(${turn.kind})` });
  }

  return messages;
}
