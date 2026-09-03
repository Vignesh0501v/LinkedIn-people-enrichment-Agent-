import { useEffect, useRef, useState } from "react";
import type { BatchStatusResponse, OutputFormat, RowPayload } from "../api/types";
import { apiErrorMessage, downloadBatch, getBatchStatus, listBatchRows } from "../api/client";
import type { BatchCardData, BatchResume } from "../lib/chatThread";
import ResultsTable from "./ResultsTable";

type Stage =
  | "loading"
  | "running"
  | "completed"
  | "failed"
  // The session's turn history outlived the batch record itself (turns live
  // in Postgres, batches/rows in Redis) — replayable as history, not resumable.
  | "unavailable";

interface Props {
  sessionId: string;
  batch: BatchCardData;
  /** Set only when this card was rebuilt from a reopened session — it makes
   * the card ask the backend where the batch actually got to before deciding
   * what to render. */
  resume?: BatchResume;
}

const POLL_INTERVAL_MS = 2000;

export default function BatchCard({ sessionId, batch, resume }: Props) {
  // A card is only ever created for a batch that was already confirmed and
  // handed to run_batch (see app/api.py's auto-mapping flow) — there is no
  // mapping/criteria step to show here, live or replayed.
  const [stage, setStage] = useState<Stage>("loading");
  const [batchStatus, setBatchStatus] = useState<BatchStatusResponse | null>(null);
  const [rows, setRows] = useState<RowPayload[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [tableExpanded, setTableExpanded] = useState(false);
  const pollRef = useRef<number | null>(null);

  function stopPolling() {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }

  useEffect(() => {
    return () => stopPolling();
  }, []);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const status = await getBatchStatus(sessionId, batch.batchId);
        if (cancelled) return;
        setBatchStatus(status);
        if (status.status === "completed") {
          const rowsRes = await listBatchRows(sessionId, batch.batchId);
          if (cancelled) return;
          setRows(rowsRes.rows);
          setStage("completed");
        } else if (status.status === "failed") {
          setStage("failed");
        } else if (status.status === "mapping_proposed") {
          // Stuck awaiting a clarifying answer from an earlier session —
          // nothing to poll toward, and no editor to resume it with.
          setStage("unavailable");
        } else {
          setStage("running");
          startPolling();
        }
      } catch (err) {
        if (cancelled) return;
        setError(apiErrorMessage(err));
        setStage("unavailable");
      }
    })();

    return () => {
      cancelled = true;
    };
    // Runs once per mounted card — a card is keyed by turn/message id, so a
    // different batch means a different component instance.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function startPolling() {
    stopPolling();
    pollRef.current = window.setInterval(async () => {
      try {
        const status = await getBatchStatus(sessionId, batch.batchId);
        setBatchStatus(status);
        if (status.status === "completed") {
          stopPolling();
          const rowsRes = await listBatchRows(sessionId, batch.batchId);
          setRows(rowsRes.rows);
          setStage("completed");
        } else if (status.status === "failed") {
          stopPolling();
          setStage("failed");
        }
      } catch (err) {
        setError(apiErrorMessage(err));
        stopPolling();
      }
    }, POLL_INTERVAL_MS);
  }

  async function handleDownload() {
    setDownloadError(null);
    try {
      await downloadBatch(sessionId, batch.batchId);
    } catch (err) {
      setDownloadError(apiErrorMessage(err));
    }
  }

  // The batch record is authoritative once we have it (the server may have
  // downgraded a requested table to excel on row count); the turn payload is
  // the fallback before the first status response lands.
  const outputFormat: OutputFormat = batchStatus?.output_format ?? batch.outputFormat;
  const inlineTable = outputFormat === "table";
  const searchedOn =
    resume?.selectedFields ?? batch.selectedFields;

  return (
    <div className="batch-card">
      {stage === "loading" && <p className="hint">Loading this request…</p>}

      {stage === "unavailable" && (
        <div className="confirmed-summary">
          This request is from an earlier session and its batch data is no longer available
          {error ? ` (${error})` : ""}. Send it again to re-run it.
        </div>
      )}

      {stage !== "loading" && stage !== "unavailable" && searchedOn.length > 0 && (
        <div className="confirmed-summary">
          <span className="stage-title">Searching by:</span> {searchedOn.join(", ")}
          {batchStatus?.search_mode && <> · mode: {batchStatus.search_mode}</>}
        </div>
      )}

      {stage === "running" && (
        <div className="progress-block">
          <p className="stage-title">Running search…</p>
          <p>
            {batchStatus?.rows_completed ?? 0} / {batchStatus?.row_count ?? batch.sampleRows.length}{" "}
            rows completed
          </p>
          {batchStatus?.counts_by_status && Object.keys(batchStatus.counts_by_status).length > 0 && (
            <ul className="counts-list">
              {Object.entries(batchStatus.counts_by_status).map(([status, count]) => (
                <li key={status}>
                  {status}: {count}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {stage === "failed" && <div className="error-text">Batch failed. {error}</div>}

      {stage === "completed" && batchStatus && (
        <div className="results-block">
          <p className="stage-title">Done — {batchStatus.rows_completed} rows processed</p>
          {batchStatus.counts_by_status && (
            <ul className="counts-list">
              {Object.entries(batchStatus.counts_by_status).map(([status, count]) => (
                <li key={status}>
                  {status}: {count}
                </li>
              ))}
            </ul>
          )}

          {/* Plan unit #25/#27: the user asked for one or the other, so lead
              with whichever they asked for. "table" small enough to read in
              chat → show it; otherwise the .xlsx is the point. */}
          {inlineTable ? (
            <>
              {rows && <ResultsTable rows={rows} />}
              <div className="secondary-actions">
                <button type="button" className="link-button" onClick={handleDownload}>
                  Download as .xlsx
                </button>
              </div>
            </>
          ) : (
            <>
              <button type="button" className="primary" onClick={handleDownload}>
                Download .xlsx
              </button>
              {rows && rows.length > 0 && (
                <div className="secondary-actions">
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => setTableExpanded((v) => !v)}
                  >
                    {tableExpanded ? "Hide preview" : `Preview ${rows.length} rows`}
                  </button>
                </div>
              )}
              {tableExpanded && rows && <ResultsTable rows={rows} />}
            </>
          )}

          {downloadError && <div className="error-text">{downloadError}</div>}
        </div>
      )}

      {error && stage !== "failed" && stage !== "unavailable" && (
        <div className="error-text">{error}</div>
      )}
    </div>
  );
}
