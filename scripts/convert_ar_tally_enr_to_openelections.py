from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = (
    ROOT
    / "data"
    / "raw"
    / "2026"
    / "20261103__ar__general__unofficial__tally_enr.json"
)
DEFAULT_OUTPUT = ROOT / "data" / "20261103__ar__general__precinct.csv"

PARTIES = {
    "f9d5fae6-a1fb-4278-a4d1-5784e643689e": "DEM",
    "4c1a540e-cfe4-4ba0-8435-1dd7c3eeb1e6": "REP",
    "365b24a1-09bb-42c0-8003-233296ece789": "LIB",
    "c51b7bb8-dfe1-4c24-b9aa-e79a014c9fae": "IND",
    "a96c4f25-7aca-4dbe-bb4f-e8a2f5d7b0fb": "IND",
    "c13d01d1-663b-4cdb-b379-743d9f8e6990": "NON",
    "83b1dfd7-514f-4fa8-a46d-002660fcb278": "",
    "cc160ad0-f761-4dfd-ae97-7216d017d908": "",
}

PREFIXES_BY_PARTY = {
    "DEM": ("DEM ", "D "),
    "REP": ("REP ", "R "),
    "LIB": ("LIB ", "L "),
    "IND": ("IND ", "I "),
}

STATEWIDE_OFFICES = {
    "u.s. senate": "U.S. Senate",
    "governor": "Governor",
    "lieutenant governor": "Lieutenant Governor",
    "attorney general": "Attorney General",
    "auditor of state": "Auditor of State",
    "secretary of state": "Secretary of State",
    "state treasurer": "State Treasurer",
    "commissioner of state lands": "Commissioner of State Lands",
}

DISTRICT_CONTESTS = (
    (re.compile(r"^U\.S\.\s+Congress\s+District\s+(\d+)$", re.I), "U.S. House"),
    (re.compile(r"^State\s+Senate,?\s+District\s+(\d+)$", re.I), "State Senate"),
    (
        re.compile(r"^State\s+Representative,?\s+Dist(?:rict)?\s+(\d+)$", re.I),
        "State House",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Arkansas Tally ENR FullDataFile.json to OpenElections CSV."
    )
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def normalize_office(contest_name: str) -> tuple[str, str]:
    name = " ".join((contest_name or "").strip().split())
    statewide = STATEWIDE_OFFICES.get(name.casefold())
    if statewide:
        return statewide, ""

    for pattern, office in DISTRICT_CONTESTS:
        match = pattern.fullmatch(name)
        if match:
            return office, str(int(match.group(1)))

    special = re.fullmatch(
        r"State\s+Representative\s+District\s+(\d+)\s+Special\s+Election\s+"
        r"\(Unexpired\s+Term\)",
        name,
        re.I,
    )
    if special:
        return "State House Special Election (Unexpired Term)", str(int(special.group(1)))

    return name, ""


def choice_votes(choice: dict) -> int:
    if "TotalVotes" in choice:
        return int(choice.get("TotalVotes") or 0)
    return sum(int(item.get("Votes") or 0) for item in choice.get("Votes", []))


def candidate_and_party(choice: dict) -> tuple[str, str]:
    candidate = " ".join((choice.get("Name") or "").strip().split())
    party = PARTIES.get(choice.get("PartyID"), "")
    for prefix in PREFIXES_BY_PARTY.get(party, ()):
        if candidate.upper().startswith(prefix):
            candidate = candidate[len(prefix) :].strip()
            break
    return candidate, party


def iter_precinct_rows(contest: dict):
    office, district = normalize_office(contest.get("ContestName") or "")
    for county_id, county in sorted((contest.get("Locations") or {}).items()):
        county_name = " ".join((county.get("LocationName") or county_id).strip().split())
        precincts = county.get("Locations") or {}
        for precinct_id, precinct in sorted(
            precincts.items(), key=lambda item: (item[1].get("LocationName") or "", item[0])
        ):
            precinct_name = " ".join(
                (precinct.get("LocationName") or precinct_id).strip().split()
            )
            choices = [
                *(precinct.get("Choices") or []),
                *(precinct.get("WriteinChoices") or []),
            ]
            for choice in choices:
                candidate, party = candidate_and_party(choice)
                yield {
                    "county": county_name,
                    "precinct": precinct_name,
                    "office": office,
                    "district": district,
                    "party": party,
                    "candidate": candidate,
                    "votes": choice_votes(choice),
                }


def validate_contest(contest: dict) -> list[str]:
    errors: list[str] = []
    contest_name = contest.get("ContestName") or contest.get("ContestID") or "unknown contest"
    state_choices = [
        *(contest.get("Choices") or []),
        *(contest.get("WriteinChoices") or []),
    ]
    state_sum = sum(choice_votes(choice) for choice in state_choices)
    if state_sum != int(contest.get("TotalVotes") or 0):
        errors.append(
            f"{contest_name}: state choices sum to {state_sum}, expected {contest.get('TotalVotes')}"
        )

    county_sum = 0
    for county in (contest.get("Locations") or {}).values():
        county_sum += int(county.get("TotalVotes") or 0)
        precinct_sum = 0
        for precinct in (county.get("Locations") or {}).values():
            precinct_choices = [
                *(precinct.get("Choices") or []),
                *(precinct.get("WriteinChoices") or []),
            ]
            choice_sum = sum(choice_votes(choice) for choice in precinct_choices)
            expected = int(precinct.get("TotalVotes") or 0)
            if choice_sum != expected:
                errors.append(
                    f"{contest_name} / {county.get('LocationName')} / "
                    f"{precinct.get('LocationName')}: choices sum to {choice_sum}, "
                    f"expected {expected}"
                )
            precinct_sum += expected
        if precinct_sum != int(county.get("TotalVotes") or 0):
            errors.append(
                f"{contest_name} / {county.get('LocationName')}: precincts sum to "
                f"{precinct_sum}, expected {county.get('TotalVotes')}"
            )
    if county_sum != int(contest.get("TotalVotes") or 0):
        errors.append(
            f"{contest_name}: counties sum to {county_sum}, expected {contest.get('TotalVotes')}"
        )
    return errors


def main() -> int:
    args = parse_args()
    payload = json.loads(args.source.read_text(encoding="utf-8-sig"))
    contests = payload.get("ContestData") or []
    errors = [error for contest in contests for error in validate_contest(contest)]
    if errors:
        preview = "\n".join(errors[:20])
        raise RuntimeError(
            f"Tally ENR reconciliation failed with {len(errors)} error(s):\n{preview}"
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["county", "precinct", "office", "district", "party", "candidate", "votes"]
    counts: Counter[str] = Counter()
    row_count = 0
    with args.output.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for contest in contests:
            for row in iter_precinct_rows(contest):
                writer.writerow(row)
                counts[row["office"]] += 1
                row_count += 1

    election = payload.get("ElectionData") or {}
    turnout = payload.get("TurnoutData") or {}
    print(
        f"Wrote {row_count:,} rows from {len(contests):,} contests to {args.output}"
    )
    print(
        f"Election {election.get('ElectionID')}; official={bool(election.get('IsOfficial'))}; "
        f"precincts={turnout.get('PrecinctsReporting')}/{turnout.get('TotalPrecincts')}"
    )
    print(f"Distinct normalized offices: {len(counts):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
