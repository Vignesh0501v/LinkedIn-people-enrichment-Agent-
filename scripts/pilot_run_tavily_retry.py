"""Retry only the rows that errored (transient DNS failure) on the first
Tavily pilot run, and merge into a complete pilot_results_tavily.xlsx.
"""
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest import read_rows
from app.tavily_search import TavilyClient

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "pilot_output"
OUTPUT_PATH = OUTPUT_DIR / "pilot_results_tavily.xlsx"
ROW_PACING_SECONDS = 0.5


def is_linkedin_profile(url: str) -> bool:
    return "linkedin.com/in" in url.lower()


def main() -> None:
    existing = pd.read_excel(OUTPUT_PATH)
    error_rows = existing[existing["status"] == "ERROR"]
    print(f"Retrying {len(error_rows)} errored rows...")

    # Only sample1.xlsx had errors, all in row_index 0-11 per the prior run.
    source_path = r"C:\Users\vigne\Downloads\sample1.xlsx"
    wanted_indices = set(error_rows["row_index"].tolist())

    updates = {}
    with TavilyClient() as client:
        for row in read_rows(source_path):
            if row.row_index not in wanted_indices:
                continue
            try:
                result = client.find_linkedin_candidates(
                    {"name": row.name.display_name, "company": row.company_normalized}
                )
                linkedin_candidates = [c for c in result.candidates if is_linkedin_profile(c.url)]
                if linkedin_candidates:
                    status = "CANDIDATE_FOUND"
                elif result.candidates:
                    status = "NO_LINKEDIN_AMONG_CANDIDATES"
                else:
                    status = "NOT_FOUND"
                updates[row.row_index] = {
                    "status": status,
                    "query_used": result.query_used,
                    "top_linkedin_url": linkedin_candidates[0].url if linkedin_candidates else "",
                    "candidate_count": len(result.candidates),
                }
            except Exception as exc:  # noqa: BLE001
                print(f"  [still erroring] row {row.row_index}: {exc}")
                updates[row.row_index] = {
                    "status": "ERROR",
                    "query_used": None,
                    "top_linkedin_url": "",
                    "candidate_count": 0,
                }
            time.sleep(ROW_PACING_SECONDS)

    for idx, values in updates.items():
        mask = (existing["source_file"] == "sample1.xlsx") & (existing["row_index"] == idx)
        for col, val in values.items():
            existing.loc[mask, col] = val

    existing.to_excel(OUTPUT_PATH, index=False)

    total = len(existing)
    print(f"\nTotal rows: {total}")
    for status in ["CANDIDATE_FOUND", "NO_LINKEDIN_AMONG_CANDIDATES", "NOT_FOUND", "ERROR"]:
        count = int((existing["status"] == status).sum())
        pct = count / total * 100 if total else 0
        print(f"  {status}: {count} ({pct:.1f}%)")


if __name__ == "__main__":
    main()
