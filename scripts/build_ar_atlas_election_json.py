from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT_PATH = DATA_DIR / "ar_elections_aggregated.json"

OFFICE_TYPES = {
    "President": "presidential",
    "U.S. President": "presidential",
    "U.S. President & Vice President": "presidential",
    "U.S. Senate": "us_senate",
    "Governor": "governor",
    "Lieutenant Governor": "lieutenant_governor",
    "Attorney General": "attorney_general",
    "Auditor of State": "auditor",
    "Secretary of State": "secretary_of_state",
    "State Treasurer": "treasurer",
    "Commissioner of State Lands": "commissioner_of_state_lands",
}

OFFICE_TITLE_PREFIXES = (
    "Commissioner of State Lands ",
    "Commissioner ",
    "U.S. Representative ",
    "State Representative ",
    "Lieutenant Governor ",
    "Lt. Governor ",
    "Vice President ",
    "U.S. Senator ",
    "Treasurer of State ",
    "Secretary of State ",
    "Attorney General ",
    "State Treasurer ",
    "Auditor of State ",
    "Representative ",
    "President ",
    "Senator ",
    "Governor ",
    "Treasurer ",
    "Auditor ",
)

INPUTS = {
    2008: [DATA_DIR / "openelections-data-ar" / "2008" / "20081104__ar__general__precinct.csv"],
    2010: [DATA_DIR / "openelections-data-ar" / "2010" / "20101102__ar__general__precinct.csv"],
    2012: [DATA_DIR / "openelections-data-ar" / "2012" / "20121106__ar__general__precinct.csv"],
    2014: [DATA_DIR / "openelections-data-ar" / "2014" / "20141104__ar__general__precinct.csv"],
    2016: [DATA_DIR / "openelections-data-ar" / "2016" / "20161108__ar__general__precinct.csv"],
    2018: [DATA_DIR / "openelections-data-ar" / "2018" / "20181106__ar__general__precinct.csv"],
    2020: sorted(
        (DATA_DIR / "openelections-data-ar" / "2020" / "counties").glob(
            "20201103__ar__general__*__precinct.csv"
        )
    ),
    2022: [DATA_DIR / "20221108__ar__general__precinct.csv"],
    2024: [DATA_DIR / "20241105__ar__general__precinct.csv"],
}

# These files are maintained as completed statewide releases.  County-name
# presence is useful as a diagnostic, but is not a completeness test for them.
AUTHORITATIVE_COMPLETE_YEARS = {2012, 2014}


def normalize_county_name(value: str) -> str:
    county = " ".join((value or "").strip().upper().split())
    if county.endswith(" COUNTY"):
        county = county[:-7].strip()
    if county in {"ST FRANCIS", "ST. FRANCIS", "SAINT FRANCIS"}:
        return "ST. FRANCIS"
    return county


def normalize_party(value: str) -> str:
    party = " ".join((value or "").strip().upper().split())
    if party in {"DEM", "DEMOCRAT", "DEMOCRATIC", "D"}:
        return "DEM"
    if party in {"REP", "REPUBLICAN", "R"}:
        return "REP"
    return party


def normalize_candidate_name(value: str) -> str:
    candidate = " ".join((value or "").strip().split())

    def strip_title(name: str) -> str:
        name = name.strip()
        name_lower = name.lower()
        for prefix in OFFICE_TITLE_PREFIXES:
            if name_lower.startswith(prefix.lower()):
                name = name[len(prefix):].strip()
                break
        suffix = " Commissioner of State Lands"
        if name.lower().endswith(suffix.lower()):
            name = name[:-len(suffix)].strip()
        return name

    return " / ".join(strip_title(part) for part in candidate.split("/"))


def color_for_margin(rep_votes: int, dem_votes: int, total_votes: int) -> str:
    if total_votes <= 0:
        return "#d1d5db"
    signed = (rep_votes - dem_votes) / total_votes * 100
    strength = abs(signed)
    if signed > 0:
        return "#991b1b" if strength >= 20 else ("#dc2626" if strength >= 10 else "#f87171")
    if signed < 0:
        return "#1e3a8a" if strength >= 20 else ("#2563eb" if strength >= 10 else "#60a5fa")
    return "#a855f7"


def main() -> int:
    results_by_year: dict[str, dict] = {}
    coverage_by_year: dict[str, dict] = {}

    for year, input_paths in INPUTS.items():
        counties_seen: set[str] = set()
        grouped: dict[tuple[str, str], dict] = defaultdict(
            lambda: {
                "dem_votes": 0,
                "rep_votes": 0,
                "other_votes": 0,
                "dem_candidate": "",
                "rep_candidate": "",
            }
        )

        if not input_paths:
            raise RuntimeError(f"No precinct files configured for {year}")
        for input_path in input_paths:
            if not input_path.exists():
                raise FileNotFoundError(input_path)
            with input_path.open(newline="", encoding="utf-8-sig") as source:
                for row in csv.DictReader(source):
                    office_type = OFFICE_TYPES.get((row.get("office") or "").strip())
                    if not office_type:
                        continue
                    county = normalize_county_name(row.get("county") or "")
                    precinct = " ".join((row.get("precinct") or "").strip().upper().split())
                    if not county or not precinct:
                        continue
                    counties_seen.add(county)
                    key = f"{county} - {precinct}"
                    node = grouped[(office_type, key)]
                    votes_raw = str(row.get("votes") or "0").replace(",", "").strip()
                    votes = int(float(votes_raw or 0))
                    party = normalize_party(row.get("party") or "")
                    candidate = normalize_candidate_name(row.get("candidate") or "")
                    if party == "DEM":
                        node["dem_votes"] += votes
                        node["dem_candidate"] = node["dem_candidate"] or candidate
                    elif party == "REP":
                        node["rep_votes"] += votes
                        node["rep_candidate"] = node["rep_candidate"] or candidate
                    else:
                        node["other_votes"] += votes

        expected_counties_payload = json.loads(
            (DATA_DIR / "tl_2020_05_county20.geojson").read_text(encoding="utf-8")
        )
        expected_counties = {
            normalize_county_name(feature["properties"].get("NAME20") or "")
            for feature in expected_counties_payload.get("features", [])
        }
        missing_counties = sorted(expected_counties - counties_seen)
        is_complete = year in AUTHORITATIVE_COMPLETE_YEARS or len(missing_counties) == 0
        coverage_by_year[str(year)] = {
            "complete": is_complete,
            "counties_present": len(counties_seen),
            "counties_expected": len(expected_counties),
            "missing_counties": [] if is_complete else missing_counties,
        }
        if not is_complete:
            print(
                f"{year}: partial precinct coverage "
                f"({len(counties_seen)}/{len(expected_counties)} counties; "
                f"missing {', '.join(missing_counties)})"
            )

        year_payload: dict[str, dict] = {}
        for (office_type, precinct_key), node in grouped.items():
            dem_votes = node["dem_votes"]
            rep_votes = node["rep_votes"]
            other_votes = node["other_votes"]
            total_votes = dem_votes + rep_votes + other_votes
            margin = rep_votes - dem_votes
            margin_pct = (abs(margin) / total_votes * 100) if total_votes else 0
            if rep_votes > dem_votes and rep_votes >= other_votes:
                winner = "R"
            elif dem_votes > rep_votes and dem_votes >= other_votes:
                winner = "D"
            elif total_votes:
                winner = "O"
            else:
                winner = "T"

            contest = year_payload.setdefault(office_type, {"general": {"results": {}}})
            contest["general"]["results"][precinct_key] = {
                **node,
                "total_votes": total_votes,
                "margin": margin,
                "margin_pct": margin_pct,
                "winner": winner,
                "competitiveness": {"color": color_for_margin(rep_votes, dem_votes, total_votes)},
            }

        results_by_year[str(year)] = year_payload
        print(
            f"{year}: {len(grouped)} precinct-contest rows across "
            f"{len(year_payload)} statewide offices"
        )

    payload = {
        "state": "Arkansas",
        "state_fips": "05",
        "coverage_by_year": coverage_by_year,
        "results_by_year": results_by_year,
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
