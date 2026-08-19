import { useState } from "react";
import type { FieldMapping } from "../api/types";
import { SELECTABLE_CRITERIA_FIELDS } from "../api/types";
import { apiErrorMessage } from "../api/client";
import { fieldIsMapped, mappingByField } from "../lib/mappingUtils";

const CRITERIA_LABELS: Record<string, string> = {
  name: "Name",
  company: "Company",
  title: "Title",
  email: "Email",
};

interface Props {
  mappings: FieldMapping[];
  initialSelected: string[];
  onConfirm: (selectedFields: string[]) => Promise<void> | void;
}

export default function CriteriaEditor({ mappings, initialSelected, onConfirm }: Props) {
  const byField = mappingByField(mappings);
  const selectableSet: readonly string[] = SELECTABLE_CRITERIA_FIELDS;
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(initialSelected.filter((f) => selectableSet.includes(f)))
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggle(field: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(field)) next.delete(field);
      else next.add(field);
      return next;
    });
  }

  async function handleConfirm() {
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm(Array.from(selected));
    } catch (err) {
      setError(apiErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="criteria-editor">
      <p className="hint">Choose which mapped fields to search on:</p>
      <div className="criteria-options">
        {SELECTABLE_CRITERIA_FIELDS.map((field) => {
          const mapped = fieldIsMapped(field, byField);
          return (
            <label key={field} className={mapped ? "" : "disabled"}>
              <input
                type="checkbox"
                disabled={!mapped}
                checked={selected.has(field)}
                onChange={() => toggle(field)}
              />
              {CRITERIA_LABELS[field] ?? field}
              {!mapped && <span className="hint"> (not mapped)</span>}
            </label>
          );
        })}
      </div>
      {error && <div className="error-text">{error}</div>}
      <button
        type="button"
        className="primary"
        disabled={submitting || selected.size === 0}
        onClick={handleConfirm}
      >
        {submitting ? "Starting…" : "Confirm criteria & run search"}
      </button>
    </div>
  );
}
