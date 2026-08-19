import type {
  ApplyTemplateResponse,
  BatchStatusResponse,
  ConfirmMappingResponse,
  CreateTurnResponse,
  FieldMapping,
  MappingTemplate,
  RowPayload,
  SessionPayload,
  SessionSummary,
} from "./types";

const API_BASE_URL: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";

class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

async function handleResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = response.statusText;
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function apiErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = err.detail as { detail?: { errors?: string[] } | string } | undefined;
    const inner = (detail as { detail?: unknown } | undefined)?.detail ?? detail;
    if (inner && typeof inner === "object" && "errors" in (inner as Record<string, unknown>)) {
      const errors = (inner as { errors?: string[] }).errors;
      if (Array.isArray(errors)) return errors.join("; ");
    }
    if (typeof inner === "string") return inner;
    return err.message;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

export async function createSession(): Promise<{ session_id: string; created_at: string }> {
  const res = await fetch(`${API_BASE_URL}/sessions`, { method: "POST" });
  return handleResponse(res);
}

export async function listSessions(limit?: number): Promise<{ sessions: SessionSummary[] }> {
  const url = new URL(`${API_BASE_URL}/sessions`);
  if (limit != null) url.searchParams.set("limit", String(limit));
  const res = await fetch(url.toString());
  return handleResponse(res);
}

export async function getSession(sessionId: string): Promise<SessionPayload> {
  const res = await fetch(`${API_BASE_URL}/sessions/${sessionId}`);
  return handleResponse(res);
}

export interface CreateTurnInput {
  file?: File | null;
  pastedText?: string | null;
  instructionsText?: string | null;
}

export async function createTurn(
  sessionId: string,
  input: CreateTurnInput
): Promise<CreateTurnResponse> {
  const form = new FormData();
  if (input.file) form.append("file", input.file);
  if (input.pastedText) form.append("pasted_text", input.pastedText);
  if (input.instructionsText) form.append("instructions_text", input.instructionsText);

  const res = await fetch(`${API_BASE_URL}/sessions/${sessionId}/turns`, {
    method: "POST",
    body: form,
  });
  return handleResponse(res);
}

export async function confirmMapping(
  sessionId: string,
  batchId: string,
  mappings: FieldMapping[]
): Promise<ConfirmMappingResponse> {
  const res = await fetch(
    `${API_BASE_URL}/sessions/${sessionId}/batches/${batchId}/mapping`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mappings),
    }
  );
  return handleResponse(res);
}

export async function applyMappingTemplate(
  sessionId: string,
  batchId: string,
  templateId: string
): Promise<ApplyTemplateResponse> {
  const res = await fetch(
    `${API_BASE_URL}/sessions/${sessionId}/batches/${batchId}/mapping/apply-template/${templateId}`,
    { method: "POST" }
  );
  return handleResponse(res);
}

export async function listMappingTemplates(): Promise<{ templates: MappingTemplate[] }> {
  const res = await fetch(`${API_BASE_URL}/mapping-templates`);
  return handleResponse(res);
}

export async function createMappingTemplate(
  sourceLabel: string,
  fieldMappings: Record<string, string>
): Promise<MappingTemplate> {
  const res = await fetch(`${API_BASE_URL}/mapping-templates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_label: sourceLabel, field_mappings: fieldMappings }),
  });
  return handleResponse(res);
}

export async function confirmCriteria(
  sessionId: string,
  batchId: string,
  selectedFields: string[]
): Promise<BatchStatusResponse> {
  const res = await fetch(
    `${API_BASE_URL}/sessions/${sessionId}/batches/${batchId}/criteria`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_fields: selectedFields }),
    }
  );
  return handleResponse(res);
}

export async function getBatchStatus(
  sessionId: string,
  batchId: string
): Promise<BatchStatusResponse> {
  const res = await fetch(`${API_BASE_URL}/sessions/${sessionId}/batches/${batchId}`);
  return handleResponse(res);
}

export async function listBatchRows(
  sessionId: string,
  batchId: string,
  status?: string
): Promise<{ rows: RowPayload[] }> {
  const url = new URL(`${API_BASE_URL}/sessions/${sessionId}/batches/${batchId}/rows`);
  if (status) url.searchParams.set("status", status);
  const res = await fetch(url.toString());
  return handleResponse(res);
}

export async function downloadBatch(sessionId: string, batchId: string): Promise<void> {
  const res = await fetch(
    `${API_BASE_URL}/sessions/${sessionId}/batches/${batchId}/download`
  );
  if (!res.ok) {
    throw new ApiError(res.status, await res.text());
  }
  const blob = await res.blob();
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `batch_${batchId}.xlsx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}

export { API_BASE_URL };
