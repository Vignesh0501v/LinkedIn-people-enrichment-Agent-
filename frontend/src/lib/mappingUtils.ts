import type { FieldMapping, SelectableCriteriaField } from "../api/types";

// Client-side mirror of app/mapping.py's `_field_is_mapped` — used only to
// decide which criteria checkboxes are enabled/pre-checked in the UI. The
// backend re-validates on PUT .../criteria regardless, so this is a UX
// convenience, not the source of truth.
export function mappingByField(mappings: FieldMapping[]): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const m of mappings) out[m.standard_field] = m.source_column;
  return out;
}

export function fieldIsMapped(
  field: SelectableCriteriaField,
  byField: Record<string, string | null>
): boolean {
  if (field === "name") {
    if (byField.full_name) return true;
    return Boolean(byField.first_name || byField.middle_name || byField.last_name);
  }
  return Boolean(byField[field]);
}
