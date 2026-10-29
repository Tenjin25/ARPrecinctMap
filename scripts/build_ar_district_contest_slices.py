from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

import build_ar_atlas_election_json as atlas
from build_ar_precinct_crosswalks import canonical_label, clean_label, match_precinct


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCE_PATH = DATA_DIR / "ar_elections_aggregated.json"
CROSSWALK_DIR = DATA_DIR / "crosswalks"
OUTPUT_DIR = DATA_DIR / "district_contests"
SAFE_METHODS = {"exact", "canonical", "containment", "fuzzy_high"}


def vintage_for_year(year: int) -> int:
    if year <= 2008:
        return 2000
    if year <= 2018:
        return 2010
    return 2020


def contest_type_for_ui(value: str) -> str:
    return "president" if value == "presidential" else value


def load_crosswalk(vintage: int):
    path = CROSSWALK_DIR / f"vtd{vintage}_to_2022_districts.csv"
    targets_by_county: dict[str, dict[str, dict]] = defaultdict(dict)
    weights: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    with path.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            county = atlas.normalize_county_name(row["county"])
            code = row["vtd_code"].strip()
            vtd_key = f"{county}|{code}"
            if vtd_key not in targets_by_county[county]:
                name = row["vtd_name"].strip()
                targets_by_county[county][vtd_key] = {
                    "name": name,
                    "code": code,
                    "vtd_key": vtd_key,
                    "clean": clean_label(name),
                    "canonical": canonical_label(name),
                }
            weights[vtd_key][row["scope"]][row["district"]] = float(row["weight"])
    return {county: list(items.values()) for county, items in targets_by_county.items()}, weights


def finish_result(node: dict, dem_candidate: str, rep_candidate: str) -> dict:
    dem = int(round(node.get("dem_votes", 0.0)))
    rep = int(round(node.get("rep_votes", 0.0)))
    other = int(round(node.get("other_votes", 0.0)))
    total = dem + rep + other
    margin = rep - dem
    if rep > dem and rep >= other:
        winner = "R"
    elif dem > rep and dem >= other:
        winner = "D"
    elif total:
        winner = "O"
    else:
        winner = "T"
    return {
        "dem_votes": dem,
        "rep_votes": rep,
        "other_votes": other,
        "total_votes": total,
        "dem_candidate": dem_candidate,
        "rep_candidate": rep_candidate,
        "margin": margin,
        "margin_pct": (abs(margin) / total * 100) if total else 0,
        "winner": winner,
    }


def main() -> int:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    coverage_by_year = payload.get("coverage_by_year", {})
    crosswalks = {vintage: load_crosswalk(vintage) for vintage in (2000, 2010, 2020)}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []

    for year_text, contests in sorted(payload["results_by_year"].items()):
        year = int(year_text)
        vintage = vintage_for_year(year)
        targets_by_county, weights = crosswalks[vintage]
        fallback_targets, fallback_weights = crosswalks[2020]
        for source_type, contest_node in sorted(contests.items()):
            contest_type = contest_type_for_ui(source_type)
            source_results = contest_node["general"]["results"]
            aggregates = {
                scope: defaultdict(lambda: defaultdict(float))
                for scope in ("congressional", "state_house", "state_senate")
            }
            candidates = {"dem": "", "rep": ""}
            source_total = 0
            matched_total = 0
            matched_precincts = 0

            for precinct_key, result in source_results.items():
                county_raw, sep, precinct = precinct_key.partition(" - ")
                county = atlas.normalize_county_name(county_raw)
                total = int(result.get("total_votes") or 0)
                source_total += total
                candidates["dem"] = candidates["dem"] or result.get("dem_candidate") or ""
                candidates["rep"] = candidates["rep"] or result.get("rep_candidate") or ""
                if not sep:
                    continue
                match = match_precinct(precinct, targets_by_county.get(county, []))
                matched = list(match["targets"])
                active_weights = weights
                if (
                    vintage != 2020
                    and (str(match["method"]) not in SAFE_METHODS or len(matched) != 1)
                ):
                    fallback_match = match_precinct(precinct, fallback_targets.get(county, []))
                    fallback_matched = list(fallback_match["targets"])
                    if str(fallback_match["method"]) in SAFE_METHODS and len(fallback_matched) == 1:
                        match = fallback_match
                        matched = fallback_matched
                        active_weights = fallback_weights
                if str(match["method"]) not in SAFE_METHODS or len(matched) != 1:
                    continue
                vtd_key = str(matched[0]["vtd_key"])
                if vtd_key not in active_weights:
                    continue
                matched_total += total
                matched_precincts += 1
                for scope, district_weights in active_weights[vtd_key].items():
                    for district, weight in district_weights.items():
                        target = aggregates[scope][district]
                        target["dem_votes"] += int(result.get("dem_votes") or 0) * weight
                        target["rep_votes"] += int(result.get("rep_votes") or 0) * weight
                        target["other_votes"] += int(result.get("other_votes") or 0) * weight

            coverage = (matched_total / source_total) if source_total else 0.0
            if coverage < 0.60:
                print(
                    f"{year} {contest_type}: skipped district slices; "
                    f"matched vote coverage is only {coverage:.1%}"
                )
                continue
            for scope, districts in aggregates.items():
                results = {
                    district: finish_result(values, candidates["dem"], candidates["rep"])
                    for district, values in sorted(districts.items(), key=lambda item: int(item[0]))
                }
                if not results:
                    continue
                file_name = f"{scope}_{contest_type}_{year}.json"
                node = {
                    "year": year,
                    "scope": scope,
                    "contest_type": contest_type,
                    "meta": {
                        "source": "NHGIS block crosswalks + Census 2022 block equivalency files",
                        "vtd_vintage": vintage,
                        "matched_precincts": matched_precincts,
                        "matched_vote_coverage": round(coverage, 6),
                        "approximate": True,
                        "statewide_partial": not bool(coverage_by_year.get(str(year), {}).get("complete", True)),
                        "missing_counties": coverage_by_year.get(str(year), {}).get("missing_counties", []),
                    },
                    "general": {"results": results},
                }
                (OUTPUT_DIR / file_name).write_text(
                    json.dumps(node, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                manifest.append(
                    {
                        "year": year,
                        "scope": scope,
                        "contest_type": contest_type,
                        "file": file_name,
                        "rows": len(results),
                        "coverage_pct": round(coverage * 100, 2),
                        "vtd_vintage": vintage,
                        "partial": not bool(coverage_by_year.get(str(year), {}).get("complete", True)),
                        "missing_counties": coverage_by_year.get(str(year), {}).get("missing_counties", []),
                    }
                )
            print(
                f"{year} {contest_type}: {matched_precincts}/{len(source_results)} precincts; "
                f"{coverage:.1%} vote coverage"
            )

    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps({"state": "Arkansas", "lines_year": 2022, "files": manifest}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(manifest)} district contest slices to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
