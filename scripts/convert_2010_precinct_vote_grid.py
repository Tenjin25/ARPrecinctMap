#!/usr/bin/env python3
"""Convert the 2010 Arkansas fixed-width precinct vote grid to OpenElections CSV."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "46669435-2010-ARKANSAS-Precinct-Vote-Grid.txt"
CANDIDATE_SOURCE = (
    ROOT / "data" / "openelections-data-ar" / "2010" / "20101102__ar__general.csv"
)
OUTPUT = ROOT / "data" / "20101102__ar__general__precinct.csv"

PARTIES = {
    "": "",
    "Democrat": "DEM",
    "Republican": "REP",
    "Green": "GRN",
    "Independent": "IND",
}


def number(value: str) -> int:
    return int(value.replace(",", ""))


def data_lines(pages: list[str], start: int, stop: int, headers: tuple[str, ...]) -> list[str]:
    result = []
    for page in pages[start:stop]:
        for line in page.splitlines():
            if not line.strip() or any(header in line for header in headers):
                continue
            result.append(line)
    return result


def candidate_roster() -> dict[tuple[str, str], list[tuple[str, str]]]:
    roster: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    with CANDIDATE_SOURCE.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            roster[(row["office"], row["district"])].add(
                (PARTIES[row["party"]], row["candidate"])
            )
    return {key: sorted(values) for key, values in roster.items()}


def candidate_for(
    roster: dict[tuple[str, str], list[tuple[str, str]]],
    office: str,
    district: str,
    party: str,
) -> str | None:
    matches = [
        candidate
        for candidate_party, candidate in roster.get((office, district), [])
        if candidate_party == party
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def third_candidate(
    roster: dict[tuple[str, str], list[tuple[str, str]]], office: str, district: str
) -> tuple[str, str] | None:
    matches = [
        (party, candidate)
        for party, candidate in roster.get((office, district), [])
        if party not in {"DEM", "REP"}
    ]
    return matches[0] if len(matches) == 1 else None


def main() -> None:
    pages = SOURCE.read_text(encoding="utf-8").split("\f")
    roster = candidate_roster()

    first_panel_pattern = re.compile(
        r"^\s*(.+?) County\s+(.*?)\s+"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$"
    )
    precincts = []
    row_locations = []
    for page_index, page in enumerate(pages[:53]):
        for line_index, line in enumerate(page.splitlines()):
            if not line.strip() or "GOV-DEM" in line or "Polling Location" in line:
                continue
            match = first_panel_pattern.match(line)
            if not match:
                raise ValueError(f"Could not parse first panel: {line!r}")
            county, precinct, *values = match.groups()
            precincts.append(
                {
                    "county": county.strip(),
                    "precinct": precinct.strip(),
                    "panel1": [number(value) for value in values],
                }
            )
            row_locations.append((page_index, line_index))

    second_panel = data_lines(pages, 53, 106, ("USS-REP",))
    if len(second_panel) != len(precincts):
        raise ValueError(f"Second panel has {len(second_panel)} rows; expected {len(precincts)}")
    second_pattern = re.compile(
        r"^\s*([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+"
        r"CD\s*0*(\d+)\s+(.*)$"
    )
    for precinct, line in zip(precincts, second_panel):
        match = second_pattern.match(line)
        if not match:
            raise ValueError(f"Could not parse second panel: {line!r}")
        *senate_values, district, remainder = match.groups()
        house_values = re.findall(r"[\d,]+", remainder)
        if len(house_values) == 4:
            house_values.append("0")
        if len(house_values) < 5:
            raise ValueError(f"Missing U.S. House values: {line!r}")
        precinct["panel2"] = [number(value) for value in senate_values]
        precinct["congressional_district"] = str(number(district))
        precinct["house_values"] = [number(value) for value in house_values[:5]]

    # The legislative panel preserves the same physical page and line positions as
    # the first panel. Blank lines are meaningful for precincts with no contest.
    for precinct, (page_index, line_index) in zip(precincts, row_locations):
        legislative_lines = pages[106 + page_index].splitlines()
        precinct["legislative"] = (
            legislative_lines[line_index] if line_index < len(legislative_lines) else ""
        )

    # The State House third-party column is printed as a narrow continuation panel.
    house_third_values = [
        number(line.strip())
        for line in data_lines(pages, 159, 211, ("IND",))
    ]
    house_contests = [
        precinct
        for precinct in precincts
        if re.search(r"(?:Rep\s*[0-9]|\bmulti\b)", precinct["legislative"])
    ]
    if len(house_third_values) != len(house_contests):
        raise ValueError(
            f"State House continuation has {len(house_third_values)} rows; "
            f"expected {len(house_contests)}"
        )
    for precinct, votes in zip(house_contests, house_third_values):
        precinct["house_third"] = votes

    executive_left = data_lines(pages, 211, 263, ("LTG-DEM",))
    executive_right = data_lines(pages, 263, 315, ("SEC-REP",))
    if len(executive_left) != len(precincts) or len(executive_right) != len(precincts):
        raise ValueError("Executive-office panel row counts do not match the precinct panel")
    executive_left_pattern = re.compile(
        r"^(.*?)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$"
    )
    for precinct, left, right in zip(precincts, executive_left, executive_right):
        left_match = executive_left_pattern.match(left)
        if not left_match:
            raise ValueError(f"Could not parse executive label and values: {left!r}")
        label, *left_values = left_match.groups()
        county_prefix = re.match(
            rf"^\s*{re.escape(precinct['county'])}\s+(.*)$", label, re.IGNORECASE
        )
        if not county_prefix:
            raise ValueError(f"Could not remove county from executive label: {label!r}")
        # This panel allocates more width to polling-place names than the first
        # panel and preserves suffixes that distinguish otherwise duplicate names.
        precinct["precinct"] = county_prefix.group(1).strip()
        right_values = re.findall(r"[\d,]+", right)
        if len(right_values) != 10:
            raise ValueError(f"Could not parse executive panels: {left!r} / {right!r}")
        precinct["panel5"] = [number(value) for value in left_values]
        precinct["panel6"] = [number(value) for value in right_values[:7]]

    rows: list[dict[str, str | int]] = []

    def add(
        precinct: dict,
        office: str,
        district: str,
        party: str,
        votes: int,
        candidate: str | None = None,
    ) -> None:
        resolved = candidate or candidate_for(roster, office, district, party)
        if resolved is None:
            if votes == 0:
                return
            resolved = f"{party or 'Other'} candidate"
        rows.append(
            {
                "county": precinct["county"],
                "precinct": precinct["precinct"],
                "office": office,
                "district": district,
                "party": party,
                "candidate": resolved,
                "votes": votes,
            }
        )

    for precinct in precincts:
        governor = precinct["panel1"][:4]
        for party, votes in zip(("DEM", "REP", "GRN", ""), governor):
            add(precinct, "Governor", "", party, votes)

        us_senate = [precinct["panel1"][4], *precinct["panel2"]]
        for party, votes in zip(("DEM", "REP", "GRN", "IND", ""), us_senate):
            add(precinct, "U.S. Senate", "", party, votes)

        district = precinct["congressional_district"]
        for party, votes in zip(("DEM", "REP", "GRN", "IND", ""), precinct["house_values"]):
            add(precinct, "U.S. House", district, party, votes)

        legislative = precinct["legislative"]
        senate_match = re.search(
            r"Senate\s*0*(\d+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)", legislative
        )
        if senate_match:
            senate_district, dem, rep, third = senate_match.groups()
            senate_district = str(number(senate_district))
            add(precinct, "State Senate", senate_district, "DEM", number(dem))
            add(precinct, "State Senate", senate_district, "REP", number(rep))
            third_entry = third_candidate(roster, "State Senate", senate_district)
            if third_entry:
                add(
                    precinct,
                    "State Senate",
                    senate_district,
                    third_entry[0],
                    number(third),
                    third_entry[1],
                )

        house_match = re.search(
            r"(?:Rep\s*([0-9]+(?:/[0-9]+)*)|\bmulti\b)\s+([\d,]+)\s+([\d,]+)",
            legislative,
        )
        if house_match:
            raw_district, dem, rep = house_match.groups()
            house_district = "multi" if raw_district is None else "/".join(
                str(number(part)) for part in raw_district.split("/")
            )
            third_votes = precinct["house_third"]
            if "/" in house_district or house_district == "multi":
                add(
                    precinct,
                    "State Representative",
                    house_district,
                    "DEM",
                    number(dem),
                    "Democratic candidates",
                )
                add(
                    precinct,
                    "State Representative",
                    house_district,
                    "REP",
                    number(rep),
                    "Republican candidates",
                )
                if third_votes:
                    add(
                        precinct,
                        "State Representative",
                        house_district,
                        "",
                        third_votes,
                        "Other candidates",
                    )
            else:
                add(precinct, "State Representative", house_district, "DEM", number(dem))
                add(precinct, "State Representative", house_district, "REP", number(rep))
                third_entry = third_candidate(roster, "State Representative", house_district)
                if third_entry:
                    add(
                        precinct,
                        "State Representative",
                        house_district,
                        third_entry[0],
                        third_votes,
                        third_entry[1],
                    )

        left = precinct["panel5"]
        right = precinct["panel6"]
        executive = [
            ("Lieutenant Governor", "DEM", left[0]),
            ("Lieutenant Governor", "REP", left[1]),
            ("Attorney General", "DEM", left[2]),
            ("Attorney General", "GRN", left[3]),
            ("Attorney General", "", left[4]),
            ("Secretary of State", "DEM", left[5]),
            ("Secretary of State", "REP", right[0]),
            ("State Treasurer", "DEM", right[1]),
            ("State Treasurer", "GRN", right[2]),
            ("Auditor of State", "DEM", right[3]),
            ("Auditor of State", "GRN", right[4]),
            ("Commissioner of State Lands", "DEM", right[5]),
            ("Commissioner of State Lands", "REP", right[6]),
        ]
        for office, party, votes in executive:
            add(precinct, office, "", party, votes)

    headers = ["county", "precinct", "office", "district", "party", "candidate", "votes"]
    with OUTPUT.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=headers, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(rows)

    split_rows = sum(
        row["office"] == "State Representative"
        and ("/" in str(row["district"]) or row["district"] == "multi")
        for row in rows
    )
    print(
        f"Wrote {len(rows)} rows for {len(precincts)} precinct/vote-mode records "
        f"to {OUTPUT} ({split_rows} split-district candidate rows)."
    )


if __name__ == "__main__":
    main()
