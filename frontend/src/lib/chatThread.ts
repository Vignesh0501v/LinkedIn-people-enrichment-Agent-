// The chat thread's message model, plus the replay that rebuilds it from a
// stored session's turn history (GET /sessions/{id}).
//
// Replay is a *reconstruction*, not a transcript dump: a `mapping_proposal`
// turn only becomes a batch card if a paired `mapping_confirmed` turn
// exists for the same batch (auto-mapping confirmed and ran it) — one that
// never got confirmed is a batch still stuck waiting on a clarifying
// answer from an earlier session, which has nothing useful to resume, so it
// renders as nothing rather than a dead editor. The `mapping_confirmed`
// turns themselves are folded *into* that batch's card rather than shown as
// separate bubbles, same as they were live.

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

/** What history tells us already happened to a replayed batch — just enough
 * to show what it searched on before the live status fetch lands. */
export interface BatchResume {
  selectedFields: string[];
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
  // What auto-mapping actually confirmed, keyed by batch — a
  // `mapping_proposal` turn only becomes a batch card if it has a match here.
  const resumeByBatch = new Map<string, BatchResume>();
  for (const turn of session.turns) {
    if (turn.kind !== "mapping_confirmed") continue;
    const payload = turn.payload ?? {};
    const batchId = asString(payload, "batch_id");
    if (!batchId) continue;
    resumeByBatch.set(batchId, { selectedFields: asStringList(payload, "selected_fields") });
  }

  const messages: ChatMessage[] = [];
  for (const turn of session.turns) {
    const id = `${session.session_id}-${turn.turn_index}`;
    const payload = turn.payload ?? {};

    if (turn.kind === "mapping_confirmed") continue; // folded into the batch card

    if (turn.kind === "mapping_proposal") {
      const batchId = asString(payload, "batch_id");
      const resume = batchId ? resumeByBatch.get(batchId) : undefined;
      if (!batchId || !resume) continue; // never confirmed -- nothing to resume
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
        resume,
      });
      continue;
    }

    if (turn.kind === "greeting_reply") {
      messages.push({ id, role: "system", kind: "reply", text: asString(payload, "text") });
      continue;
    }

    if (turn.kind === "clarification_question") {
      messages.push({ id, role: "system", kind: "reply", text: asString(payload, "question") });
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
