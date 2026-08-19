import { useEffect, useState } from "react";
import type { FieldMapping, MappingTemplate } from "../api/types";
import { STANDARD_FIELDS } from "../api/types";
import {
  applyMappingTemplate,
  apiErrorMessage,
  createMappingTemplate,
  listMappingTemplates,
} from "../api/client";

const FIELD_LABELS: Record<string, string> = {
  first_name: "First name",
  middle_name: "Middle name",
  last_name: "Last name",
  full_name: "Full name",
  company: "Company",
  title: "Title",
  email: "Email",
};

interface Props {
  sessionId: string;
  batchId: string;
  columns: string[];
  sampleRows: Record<string, string>[];
  initialMappings: FieldMapping[];
  onConfirm: (mappings: FieldMapping[]) => Promise<void> | void;
}

export default function MappingEditor({
  sessionId,
  batchId,
  columns,
  sampleRows,
  initialMappings,
  onConfirm,
}: Props) {
  const [mapping, setMapping] = useState<Record<string, string>>(() => {
    const m: Record<string, string> = {};
    for (const field of STANDARD_FIELDS) m[field] = "";
    for (const entry of initialMappings) {
      if (entry.source_column) m[entry.standard_field] = entry.source_column;
    }
    return m;
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [templates, setTemplates] = useState<MappingTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>("");
  const [unmatched, setUnmatched] = useState<string[]>([]);
  const [saveLabel, setSaveLabel] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  useEffect(() => {
    listMappingTemplates()
      .then((res) => setTemplates(res.templates))
      .catch(() => {
        /* templates are a nice-to-have; ignore failures silently */
      });
  }, []);

  function handleFieldChange(field: string, column: string) {
    setMapping((prev) => ({ ...prev, [field]: column }));
  }

  async function handleApplyTemplate() {
    if (!selectedTemplateId) return;
    setError(null);
    try {
      const res = await applyMappingTemplate(sessionId, batchId, selectedTemplateId);
      const m: Record<string, string> = {};
      for (const field of STANDARD_FIELDS) m[field] = "";
      for (const entry of res.field_mappings) {
        if (entry.source_column) m[entry.standard_field] = entry.source_column;
      }
      setMapping(m);
      setUnmatched(res.unmatched_fields);
    } catch (err) {
      setError(apiErrorMessage(err));
    }
  }

  async function handleSaveTemplate() {
    if (!saveLabel.trim()) return;
    setSaving(true);
    setSaveMessage(null);
    try {
      const fieldMappings: Record<string, string> = {};
      for (const field of STANDARD_FIELDS) {
        if (mapping[field]) fieldMappings[field] = mapping[field];
      }
      await createMappingTemplate(saveLabel.trim(), fieldMappings);
      setSaveMessage("Saved.");
      setSaveLabel("");
      const res = await listMappingTemplates();
      setTemplates(res.templates);
    } catch (err) {
      setSaveMessage(apiErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  async function handleConfirm() {
    setSubmitting(true);
    setError(null);
    try {
      const mappings: FieldMapping[] = STANDARD_FIELDS.map((field) => ({
        standard_field: field,
        source_column: mapping[field] || null,
      }));
      await onConfirm(mappings);
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mapping-editor">
      {columns.length > 0 && sampleRows.length > 0 && (
        <div className="sample-table-wrap">
          <table className="sample-table">
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sampleRows.map((row, i) => (
                <tr key={i}>
                  {columns.map((c) => (
                    <td key={c}>{row[c]}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {templates.length > 0 && (
        <div className="template-row">
          <label>
            Reuse a saved mapping:{" "}
            <select
              value={selectedTemplateId}
              onChange={(e) => setSelectedTemplateId(e.target.value)}
            >
              <option value="">— select a template —</option>
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.source_label}
                </option>
              ))}
            </select>
          </label>
          <button type="button" disabled={!selectedTemplateId} onClick={handleApplyTemplate}>
            Apply template
          </button>
          {unmatched.length > 0 && (
            <span className="warning">
              Not found in this file's columns: {unmatched.join(", ")}
            </span>
          )}
        </div>
      )}

      <div className="mapping-fields">
        {STANDARD_FIELDS.map((field) => (
          <div key={field} className="mapping-field-row">
            <span className="mapping-field-label">{FIELD_LABELS[field] ?? field}</span>
            <select
              value={mapping[field]}
              onChange={(e) => handleFieldChange(field, e.target.value)}
            >
              <option value="">Not mapped</option>
              {columns.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>

      <div className="save-template-row">
        <input
          type="text"
          placeholder="Save this mapping as… (e.g. HR export)"
          value={saveLabel}
          onChange={(e) => setSaveLabel(e.target.value)}
        />
        <button type="button" disabled={!saveLabel.trim() || saving} onClick={handleSaveTemplate}>
          {saving ? "Saving…" : "Save as template"}
        </button>
        {saveMessage && <span className="hint">{saveMessage}</span>}
      </div>

      {error && <div className="error-text">{error}</div>}

      <button type="button" className="primary" disabled={submitting} onClick={handleConfirm}>
        {submitting ? "Confirming…" : "Confirm mapping"}
      </button>
    </div>
  );
}
