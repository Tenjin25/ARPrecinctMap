#!/usr/bin/env python3
"""Validate Arkansas district contest completeness and statewide vote totals."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SOURCE = DATA / "ar_elections_aggregated.json"
MANIFEST = DATA / "district_contests" / "manifest.json"
EXPECTED_DISTRICTS = {"congressional": 4, "state_house": 100, "state_senate": 35}


def main() -> int:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]
    errors: list[str] = []
    minimum_coverage: dict[tuple[int, str], float] = {}

    for entry in entries:
        path = DATA / "district_contests" / entry["file"]
        node = json.loads(path.read_text(encoding="utf-8"))
        results = node["general"]["results"]
        scope = str(entry["scope"])
        expected_count = EXPECTED_DISTRICTS[scope]
        if len(results) != expected_count:
            errors.append(f"{entry['file']}: {len(results)} districts, expected {expected_count}")

        year = str(entry["year"])
        contest = str(entry["contest_type"])
        source_contest = "presidential" if contest == "president" else contest
        source_results = source["results_by_year"][year][source_contest]["general"]["results"]
        certified_total = sum(int(row.get("total_votes") or 0) for row in source_results.values())
        district_total = sum(int(row.get("total_votes") or 0) for row in results.values())
        if district_total != certified_total:
            errors.append(
                f"{entry['file']}: district total {district_total:,} != certified {certified_total:,}"
            )

        key = (int(year), scope)
        match_coverage = float(entry.get("split_precinct_match_coverage_pct") or 0)
        if key not in minimum_coverage or match_coverage < minimum_coverage[key]:
            minimum_coverage[key] = match_coverage

    print(f"Validated {len(entries)} district contest files; errors={len(errors)}")
    for error in errors:
        print(f"ERROR: {error}")
    for (year, scope), coverage in sorted(minimum_coverage.items()):
        print(f"{year} {scope}: minimum split-precinct match coverage {coverage:.2f}%")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
