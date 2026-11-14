#!/usr/bin/env python3
"""Reconcile the normalized 2026 CSV and atlas output to the Tally ENR snapshot."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from convert_ar_tally_enr_to_openelections import candidate_and_party


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw" / "2026" / "20261103__ar__general__unofficial__tally_enr.json"
CSV_PATH = DATA / "20261103__ar__general__precinct.csv"
AGGREGATED = DATA / "ar_elections_aggregated.json"

OFFICES = {
    "U.S. Senate": "us_senate",
    "Governor": "governor",
    "Lieutenant Governor": "lieutenant_governor",
    "Attorney General": "attorney_general",
    "Auditor of State": "auditor",
    "Secretary of State": "secretary_of_state",
    "State Treasurer": "treasurer",
    "Commissioner of State Lands": "commissioner_of_state_lands",
}


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8-sig"))
    aggregated = json.loads(AGGREGATED.read_text(encoding="utf-8"))
    csv_totals: Counter[tuple[str, str, str]] = Counter()
    row_count = 0
    with CSV_PATH.open(newline="", encoding="utf-8") as source:
        rows = csv.DictReader(source)
        expected_header = [
            "county",
            "precinct",
            "office",
            "district",
            "party",
            "candidate",
            "votes",
        ]
        if rows.fieldnames != expected_header:
            raise RuntimeError(f"Unexpected CSV header: {rows.fieldnames}")
        for row in rows:
            csv_totals[(row["office"], row["party"], row["candidate"])] += int(
                row["votes"]
            )
            row_count += 1

    expected_row_count = sum(
        len(precinct.get("Choices", [])) + len(precinct.get("WriteinChoices", []))
        for contest in raw["ContestData"]
        for county in contest.get("Locations", {}).values()
        for precinct in county.get("Locations", {}).values()
    )
    if row_count != expected_row_count:
        raise RuntimeError(
            f"Expected {expected_row_count:,} CSV rows from the source, found {row_count:,}"
        )

    for office, aggregate_key in OFFICES.items():
        contest = next(
            contest
            for contest in raw["ContestData"]
            if contest.get("ContestName") == office
        )
        source_totals = Counter()
        for choice in contest.get("Choices", []):
            candidate, party = candidate_and_party(choice)
            source_totals[(office, party, candidate)] += int(choice.get("TotalVotes") or 0)
        normalized_totals = Counter(
            {key: votes for key, votes in csv_totals.items() if key[0] == office}
        )
        if normalized_totals != source_totals:
            raise RuntimeError(
                f"{office}: CSV totals differ from Tally ENR: "
                f"{normalized_totals} != {source_totals}"
            )

        atlas_rows = aggregated["results_by_year"]["2026"][aggregate_key]["general"][
            "results"
        ].values()
        atlas_total = sum(int(row.get("total_votes") or 0) for row in atlas_rows)
        if atlas_total != int(contest.get("TotalVotes") or 0):
            raise RuntimeError(
                f"{office}: atlas total {atlas_total:,} != Tally ENR "
                f"{int(contest.get('TotalVotes') or 0):,}"
            )
        print(f"{office}: {atlas_total:,} votes reconciled")

    coverage = aggregated["coverage_by_year"]["2026"]
    if not coverage.get("complete") or coverage.get("counties_present") != 75:
        raise RuntimeError(f"Incomplete 2026 atlas coverage: {coverage}")
    print(f"CSV: {row_count:,} rows; coverage: 75/75 counties; errors=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
