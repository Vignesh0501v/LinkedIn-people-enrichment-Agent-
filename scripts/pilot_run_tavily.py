"""Phase 1 pilot, re-run against Tavily (Plan unit #8).

Same 71 real rows across the same 3 sample files used for the original
SearXNG pilot, so the CANDIDATE_FOUND rate is directly comparable to that
baseline (5.6%, 4/71) rather than measuring something different.
"""
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest import read_rows
from app.tavily_search import TavilyClient

SAMPLE_FILES = [
    r"C:\Users\vigne\Downloads\sample1.xlsx",
    r"C:\Users\vigne\Downloads\sample2.xlsx",
    r"C:\Users\vigne\Downloads\sample3.xlsx",
]

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "pilot_output"
ROW_PACING_SECONDS = 0.5  # Tavily is a real metered API, not a scraper — lighter pacing than the SearXNG pilot


def is_linkedin_profile(url: str) -> bool:
    return "linkedin.com/in" in url.lower()


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    records = []

    with TavilyClient() as client:
        for file_path in SAMPLE_FILES:
            source = Path(file_path).name
            for row in read_rows(file_path):
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
                    query_used = result.query_used
                    top_url = linkedin_candidates[0].url if linkedin_candidates else ""
                    candidate_count = len(result.candidates)
                except Exception as exc:  # noqa: BLE001 - pilot script, log and keep going
                    status = "ERROR"
                    query_used = None
                    top_url = ""
                    candidate_count = 0
                    print(f"  [error] {source} row {row.row_index}: {exc}")

                records.append(
                    {
                        "source_file": source,
                        "row_index": row.row_index,
                        "name": row.name.display_name,
                        "company_normalized": row.company_normalized,
                        "status": status,
                        "query_used": query_used,
                        "top_linkedin_url": top_url,
                        "candidate_count": candidate_count,
                    }
                )
                time.sleep(ROW_PACING_SECONDS)

    df = pd.DataFrame(records)
    output_path = OUTPUT_DIR / "pilot_results_tavily.xlsx"
    df.to_excel(output_path, index=False)

    total = len(df)
    print(f"\nTotal rows processed: {total}")
    for status in ["CANDIDATE_FOUND", "NO_LINKEDIN_AMONG_CANDIDATES", "NOT_FOUND", "ERROR"]:
        count = int((df["status"] == status).sum())
        pct = count / total * 100 if total else 0
        print(f"  {status}: {count} ({pct:.1f}%)")
    print(f"\nPer-row detail written to: {output_path}")


if __name__ == "__main__":
    main()
