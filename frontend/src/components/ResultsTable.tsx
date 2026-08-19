import type { RowPayload } from "../api/types";

interface Props {
  rows: RowPayload[];
}

const STATUS_CLASS: Record<string, string> = {
  VERIFIED: "status-verified",
  REVIEW_REQUIRED: "status-review",
  NOT_FOUND: "status-notfound",
  MISSING_SEARCH_FIELD: "status-missing",
};

export default function ResultsTable({ rows }: Props) {
  if (rows.length === 0) {
    return <p className="hint">No rows yet.</p>;
  }

  return (
    <div className="results-table-wrap">
      <table className="results-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Fields</th>
            <th>LinkedIn URL</th>
            <th>City</th>
            <th>Confidence</th>
            <th>Status</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.row_index}>
              <td>{row.row_index + 1}</td>
              <td>
                {Object.entries(row.field_values)
                  .filter(([, v]) => v)
                  .map(([k, v]) => `${k}: ${v}`)
                  .join(", ")}
              </td>
              <td>
                {row.linkedin_url ? (
                  <a href={row.linkedin_url} target="_blank" rel="noreferrer">
                    {row.linkedin_url}
                  </a>
                ) : (
                  "—"
                )}
              </td>
              <td>{row.city || "—"}</td>
              <td>{row.match_confidence != null ? row.match_confidence.toFixed(2) : "—"}</td>
              <td>
                <span className={`status-pill ${STATUS_CLASS[row.match_status] ?? ""}`}>
                  {row.match_status}
                </span>
              </td>
              <td>{row.source_reason || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
