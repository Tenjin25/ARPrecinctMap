from __future__ import annotations

import csv
import io
import json
import math
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

import build_ar_atlas_election_json as atlas
from build_ar_precinct_crosswalks import (
    abbreviation_variants,
    canonical_label,
    clean_label,
    match_precinct,
    structural_label,
)


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCE_PATH = DATA_DIR / "ar_elections_aggregated.json"
CROSSWALK_DIR = DATA_DIR / "crosswalks"
OUTPUT_DIR = DATA_DIR / "district_contests"
SAFE_METHODS = {
    "exact", "canonical", "compact_code", "structural", "structural_prefix",
    "abbreviation", "numeric_code", "containment", "fuzzy_high"
}
DISTRICT_SAFE_METHODS = {
    "ambiguous_canonical",
    "ambiguous_structural",
    "ambiguous_abbreviation",
    "ambiguous_containment",
    "structural_group",
    "numeric_group",
    "compact_code_group",
    "numeric_family_group",
}
SCOPES = ("congressional", "state_house", "state_senate")
NC_RULE_MIN_COVERAGE = 0.999
BLOCK_2024_PATH = DATA_DIR / "ar_2024_gen_2020_blocks_csv.zip"
CVAP_BLOCK_PATH = DATA_DIR / "ar_cvap_2024_2020_b_csv.zip"
REPORTING_2024_CROSSWALK = CROSSWALK_DIR / "precinct2024_to_2022_districts.csv"
PRECINCT_ALIASES_PATH = DATA_DIR / "ar_precinct_aliases.csv"
BLOCK_2024_FIELDS = {
    "president": {
        "dem": ("G24PREDHAR",),
        "rep": ("G24PRERTRU",),
        "other": ("G24PREASON", "G24PREGSTE", "G24PREIKEN", "G24PRELOLI", "G24PREPWOO"),
    },
    "treasurer": {
        "dem": ("G24TREDPAG",),
        "rep": ("G24TRERTHU",),
        "other": ("G24TRELPAK",),
    },
}


def vintage_for_year(year: int) -> int:
    if year <= 2008:
        return 2000
    if year <= 2018:
        return 2010
    return 2020


def contest_type_for_ui(value: str) -> str:
    return "president" if value == "presidential" else value


def load_crosswalk(vintage: int, path: Path | None = None):
    path = path or CROSSWALK_DIR / f"vtd{vintage}_to_2022_districts.csv"
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
                    "structural": structural_label(name),
                    "abbreviations": abbreviation_variants(name),
                    "basis_total_by_scope": {},
                }
            basis_total = float(row.get("basis_total") or 1.0)
            targets_by_county[county][vtd_key]["basis_total_by_scope"][row["scope"]] = basis_total
            weights[vtd_key][row["scope"]][row["district"]] = float(row["weight"])
    return {county: list(items.values()) for county, items in targets_by_county.items()}, weights


def load_precinct_aliases() -> dict[tuple[int, str, str], str]:
    """Load audited one-to-one historical precinct aliases to 2020 VTD labels."""
    if not PRECINCT_ALIASES_PATH.exists():
        return {}
    aliases: dict[tuple[int, str, str], str] = {}
    consensus: dict[tuple[str, str], set[str]] = defaultdict(set)
    with PRECINCT_ALIASES_PATH.open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            try:
                year = int(row.get("election_year") or 0)
            except ValueError:
                continue
            county = atlas.normalize_county_name(row.get("county_norm") or "")
            precinct = clean_label(row.get("precinct_norm") or "")
            target = str(row.get("canonical_label") or row.get("prctseq_2024") or "").strip()
            if year and county and precinct and target:
                aliases[(year, county, precinct)] = target
                consensus[(county, precinct)].add(target)
    # Polling-place labels often persist even when one election's export is
    # truncated or formatted differently. Reuse a cross-year alias only when
    # every audited occurrence points to the same 2020 VTD.
    for (county, precinct), targets in consensus.items():
        if len(targets) == 1:
            aliases[(0, county, precinct)] = next(iter(targets))
    return aliases


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


def district_safe_assignments(matches: list[dict], weights: dict) -> dict[str, str]:
    """Apply the NC-style county-cluster rule without vote reconciliation.

    Candidate VTDs are already county-constrained.  A cluster is assignable
    only when every candidate has at least 99.9% of its block weight in the
    same district for the requested scope.
    """
    assignments: dict[str, str] = {}
    for scope in SCOPES:
        districts: set[str] = set()
        valid = bool(matches)
        for item in matches:
            district_weights = weights.get(str(item["vtd_key"]), {}).get(scope, {})
            if not district_weights:
                valid = False
                break
            district, weight = max(district_weights.items(), key=lambda item: item[1])
            if weight < NC_RULE_MIN_COVERAGE:
                valid = False
                break
            districts.add(district)
        if valid and len(districts) == 1:
            assignments[scope] = next(iter(districts))
    return assignments


def load_block_district_assignments() -> dict[str, dict[str, str]]:
    paths = {
        "congressional": DATA_DIR / "05_AR_CD118.txt",
        "state_house": DATA_DIR / "05_AR_SLDL22.txt",
        "state_senate": DATA_DIR / "05_AR_SLDU22.txt",
    }
    assignments: dict[str, dict[str, str]] = {}
    for scope, path in paths.items():
        with path.open(newline="", encoding="utf-8-sig") as source:
            rows = csv.DictReader(source)
            district_field = next(field for field in rows.fieldnames or [] if field != "GEOID")
            assignments[scope] = {
                row["GEOID"]: str(int(row[district_field]))
                for row in rows
                if row.get("GEOID") and row.get(district_field)
            }
    return assignments


def load_county_cvap_weights() -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, dict]]:
    assignments = load_block_district_assignments()
    county_payload = json.loads((DATA_DIR / "tl_2020_05_county20.geojson").read_text(encoding="utf-8"))
    county_names = {
        str(feature["properties"]["COUNTYFP20"]).zfill(3): atlas.normalize_county_name(
            feature["properties"]["NAME20"]
        )
        for feature in county_payload["features"]
    }
    raw = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    block_counts = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    with zipfile.ZipFile(CVAP_BLOCK_PATH) as archive:
        csv_name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
        stream = io.TextIOWrapper(archive.open(csv_name), encoding="utf-8-sig", newline="")
        for row in csv.DictReader(stream):
            block = row["GEOID20"]
            county = county_names.get(block[2:5])
            if not county:
                continue
            cvap = float(row.get("CVAP_TOT24") or 0)
            for scope in SCOPES:
                district = assignments[scope].get(block)
                if not district:
                    continue
                raw[scope][county][district] += cvap
                block_counts[scope][county][district] += 1

    weights: dict[str, dict[str, dict[str, float]]] = {scope: {} for scope in SCOPES}
    diagnostics: dict[str, dict] = {scope: {"whole": [], "split": []} for scope in SCOPES}
    for scope in SCOPES:
        for county in sorted(county_names.values()):
            values = raw[scope][county]
            total = sum(values.values())
            if total <= 0:
                values = block_counts[scope][county]
                total = sum(values.values())
            shares = {district: value / total for district, value in values.items() if value > 0}
            dominant, dominant_share = max(shares.items(), key=lambda item: item[1])
            if dominant_share >= NC_RULE_MIN_COVERAGE:
                weights[scope][county] = {dominant: 1.0}
                diagnostics[scope]["whole"].append(county)
            else:
                weights[scope][county] = shares
                diagnostics[scope]["split"].append({
                    "county": county,
                    "district_shares": {district: round(share, 8) for district, share in sorted(shares.items())},
                })
    return weights, diagnostics


def hamilton_allocate(total: int, weights: dict[str, float]) -> dict[str, int]:
    raw = {district: total * weight for district, weight in weights.items()}
    allocated = {district: math.floor(value) for district, value in raw.items()}
    remainder = total - sum(allocated.values())
    order = sorted(raw, key=lambda district: (-(raw[district] - allocated[district]), int(district)))
    for district in order[:remainder]:
        allocated[district] += 1
    return allocated


def combine_target_weights(matches: list[dict], weights: dict, scope: str) -> dict[str, float]:
    """Combine one or more matched VTDs using their CVAP/basis totals."""
    combined: dict[str, float] = defaultdict(float)
    basis_sum = 0.0
    for item in matches:
        vtd_weights = weights.get(str(item["vtd_key"]), {}).get(scope, {})
        if not vtd_weights:
            continue
        basis = float(item.get("basis_total_by_scope", {}).get(scope) or 1.0)
        basis_sum += basis
        for district, share in vtd_weights.items():
            combined[district] += basis * share
    if basis_sum <= 0:
        return {}
    total = sum(combined.values())
    return {district: value / total for district, value in combined.items() if value > 0}


def precinct_weight_options(
    year: int,
    county: str,
    precinct: str,
    crosswalks: dict[int, tuple[dict, dict]],
    reporting_2024: tuple[dict, dict] | None,
) -> list[tuple[dict, list[dict], dict]]:
    vintage = vintage_for_year(year)
    targets_by_county, weights = crosswalks[vintage]
    fallback_targets, fallback_weights = crosswalks[2020]
    options: list[tuple[dict, list[dict], dict]] = []
    if year >= 2022 and reporting_2024:
        reporting_targets, reporting_weights = reporting_2024
        match = match_precinct(precinct, reporting_targets.get(county, []))
        options.append((match, list(match["targets"]), reporting_weights))
    match = match_precinct(precinct, targets_by_county.get(county, []))
    options.append((match, list(match["targets"]), weights))
    if 2014 <= year < 2022 and reporting_2024:
        reporting_targets, reporting_weights = reporting_2024
        match = match_precinct(precinct, reporting_targets.get(county, []))
        options.append((match, list(match["targets"]), reporting_weights))
    if vintage != 2020:
        match = match_precinct(precinct, fallback_targets.get(county, []))
        options.append((match, list(match["targets"]), fallback_weights))
    else:
        # Several counties, especially Sebastian, continued reporting with
        # 2010-era ward/precinct identifiers through 2020 and into 2022.
        legacy_targets, legacy_weights = crosswalks[2010]
        match = match_precinct(precinct, legacy_targets.get(county, []))
        options.append((match, list(match["targets"]), legacy_weights))
    return options


def resolve_named_region_weights(
    year: int,
    county: str,
    precinct: str,
    scope: str,
    crosswalks: dict[int, tuple[dict, dict]],
) -> dict[str, float]:
    """Resolve high-confidence historical municipal early/absentee pools."""
    if year > 2008 or county != "SEBASTIAN":
        return {}
    label = clean_label(precinct)
    region = ""
    if "FORT SMITH" in label and ("EARLY" in label or "ABSENTEE" in label):
        region = "fort_smith"
    elif "GREENWOOD" in label and ("EARLY" in label or "ABSENTEE" in label):
        region = "greenwood"
    if not region:
        return {}

    targets_by_county, weights = crosswalks[2000]
    candidates = []
    for item in targets_by_county.get(county, []):
        name = clean_label(item.get("name") or "")
        is_fort_smith = "FORT SMITH WARD" in name or name.startswith("SMITH WARD")
        if (region == "fort_smith" and is_fort_smith) or (region == "greenwood" and not is_fort_smith):
            candidates.append(item)
    return combine_target_weights(candidates, weights, scope)


def resolve_precinct_weights(
    year: int,
    county: str,
    precinct: str,
    scope: str,
    crosswalks: dict[int, tuple[dict, dict]],
    reporting_2024: tuple[dict, dict] | None,
    aliases: dict[tuple[int, str, str], str] | None = None,
) -> tuple[dict[str, float], str]:
    regional = resolve_named_region_weights(year, county, precinct, scope, crosswalks)
    if regional:
        return regional, "named_region"
    precinct_clean = clean_label(precinct)
    alias = (aliases or {}).get((year, county, precinct_clean)) or (aliases or {}).get(
        (0, county, precinct_clean)
    )
    if alias:
        alias_targets, alias_weights = crosswalks[2020]
        alias_match = match_precinct(alias, alias_targets.get(county, []))
        alias_matches = list(alias_match.get("targets") or [])
        if len(alias_matches) == 1:
            resolved = combine_target_weights(alias_matches, alias_weights, scope)
            if resolved:
                return resolved, f"audited_alias_{alias_match['method']}"
    options = precinct_weight_options(year, county, precinct, crosswalks, reporting_2024)
    method_rank = {
        "exact": 100,
        "canonical": 95,
        "compact_code": 94,
        "structural": 92,
        "numeric_code": 90,
        "abbreviation": 88,
        "containment": 82,
        "fuzzy_high": 78,
        "structural_group": 76,
        "compact_code_group": 75,
        "numeric_family_group": 77,
        "numeric_group": 74,
        "ambiguous_canonical": 68,
        "ambiguous_structural": 66,
        "ambiguous_abbreviation": 64,
        "ambiguous_containment": 62,
    }
    resolved_options: list[tuple[int, int, dict[str, float], str]] = []
    for order, (match, matches, active_weights) in enumerate(options):
        method = str(match["method"])
        if method in SAFE_METHODS and len(matches) == 1:
            resolved = combine_target_weights(matches, active_weights, scope)
            if resolved:
                resolved_options.append((method_rank.get(method, 0), -order, resolved, method))
            continue
        if method not in DISTRICT_SAFE_METHODS or not matches:
            continue
        if method in {"structural_group", "compact_code_group", "numeric_family_group", "numeric_group"}:
            resolved = combine_target_weights(matches, active_weights, scope)
            if resolved:
                resolved_options.append((method_rank.get(method, 0), -order, resolved, method))
                continue
        district = district_safe_assignments(matches, active_weights).get(scope)
        if district:
            resolved_options.append((method_rank.get(method, 0), -order, {district: 1.0}, method))
    if resolved_options:
        _rank, _order, resolved, method = max(resolved_options, key=lambda item: (item[0], item[1]))
        return resolved, method
    return {}, "county_cvap_fallback"


def load_2024_block_party_weights() -> dict:
    """Return within-county district shares from the supplied 2024 block returns."""
    if not BLOCK_2024_PATH.exists():
        return {}
    assignments = load_block_district_assignments()
    county_payload = json.loads((DATA_DIR / "tl_2020_05_county20.geojson").read_text(encoding="utf-8"))
    county_names = {
        str(feature["properties"]["COUNTYFP20"]).zfill(3): atlas.normalize_county_name(
            feature["properties"]["NAME20"]
        )
        for feature in county_payload["features"]
    }
    raw = defaultdict(
        lambda: defaultdict(
            lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        )
    )
    with zipfile.ZipFile(BLOCK_2024_PATH) as archive:
        csv_name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
        stream = io.TextIOWrapper(archive.open(csv_name), encoding="utf-8-sig", newline="")
        for row in csv.DictReader(stream):
            block = row["GEOID20"]
            county = county_names.get(block[2:5])
            if not county:
                continue
            for contest, fields in BLOCK_2024_FIELDS.items():
                party_votes = {
                    party: sum(int(row.get(field) or 0) for field in party_fields)
                    for party, party_fields in fields.items()
                }
                for scope in SCOPES:
                    district = assignments[scope].get(block)
                    if not district:
                        continue
                    for party, votes in party_votes.items():
                        raw[contest][scope][county][party][district] += votes

    normalized = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
    for contest, scopes in raw.items():
        for scope, counties in scopes.items():
            for county, parties in counties.items():
                for party, district_votes in parties.items():
                    total = sum(district_votes.values())
                    if total > 0:
                        normalized[contest][scope][county][f"{party}_votes"] = {
                            district: votes / total
                            for district, votes in district_votes.items()
                            if votes > 0
                        }
    return normalized


def build_county_rule_slices(payload: dict, coverage_by_year: dict) -> int:
    county_weights, diagnostics = load_county_cvap_weights()
    expected_districts = {
        scope: {
            district
            for weights_by_district in county_weights[scope].values()
            for district in weights_by_district
        }
        for scope in SCOPES
    }
    crosswalks = {vintage: load_crosswalk(vintage) for vintage in (2000, 2010, 2020)}
    reporting_2024 = load_crosswalk(2024, REPORTING_2024_CROSSWALK) if REPORTING_2024_CROSSWALK.exists() else None
    aliases = load_precinct_aliases()
    block_party_weights = load_2024_block_party_weights()
    manifest: list[dict] = []
    for year_text, contests in sorted(payload["results_by_year"].items()):
        year = int(year_text)
        for source_type, contest_node in sorted(contests.items()):
            contest_type = contest_type_for_ui(source_type)
            source_results = contest_node["general"]["results"]
            county_totals = defaultdict(lambda: defaultdict(int))
            county_precincts = defaultdict(list)
            candidates = {"dem": "", "rep": ""}
            for precinct_key, result in source_results.items():
                county_raw, separator, precinct = precinct_key.partition(" - ")
                if not separator:
                    continue
                county = atlas.normalize_county_name(county_raw)
                county_precincts[county].append((precinct, result))
                county_totals[county]["dem_votes"] += int(result.get("dem_votes") or 0)
                county_totals[county]["rep_votes"] += int(result.get("rep_votes") or 0)
                county_totals[county]["other_votes"] += int(result.get("other_votes") or 0)
                candidates["dem"] = candidates["dem"] or result.get("dem_candidate") or ""
                candidates["rep"] = candidates["rep"] or result.get("rep_candidate") or ""

            for scope in SCOPES:
                aggregates = defaultdict(lambda: defaultdict(int))
                whole_county_votes = 0
                split_matched_votes = 0
                split_fallback_votes = 0
                split_block_votes = 0
                match_methods: dict[str, int] = defaultdict(int)
                for county, totals in county_totals.items():
                    county_district_weights = county_weights[scope].get(county)
                    if not county_district_weights:
                        raise RuntimeError(f"No {scope} CVAP weights for {county}")
                    county_vote_total = sum(totals.values())
                    if len(county_district_weights) == 1:
                        district = next(iter(county_district_weights))
                        for party in ("dem_votes", "rep_votes", "other_votes"):
                            aggregates[district][party] += totals[party]
                        whole_county_votes += county_vote_total
                        continue

                    direct_weights = (
                        block_party_weights.get(contest_type, {}).get(scope, {}).get(county, {})
                        if year == 2024
                        else {}
                    )
                    if direct_weights:
                        for party in ("dem_votes", "rep_votes", "other_votes"):
                            party_weights = direct_weights.get(party) or county_district_weights
                            for district, votes in hamilton_allocate(totals[party], party_weights).items():
                                aggregates[district][party] += votes
                        split_block_votes += county_vote_total
                        continue

                    for precinct, result in county_precincts[county]:
                        precinct_weights, method = resolve_precinct_weights(
                            year,
                            county,
                            precinct,
                            scope,
                            crosswalks,
                            reporting_2024,
                            aliases,
                        )
                        precinct_vote_total = sum(
                            int(result.get(party) or 0)
                            for party in ("dem_votes", "rep_votes", "other_votes")
                        )
                        if precinct_weights:
                            split_matched_votes += precinct_vote_total
                            match_methods[method] += 1
                        else:
                            precinct_weights = county_district_weights
                            split_fallback_votes += precinct_vote_total
                            match_methods["county_cvap_fallback"] += 1
                        for party in ("dem_votes", "rep_votes", "other_votes"):
                            for district, votes in hamilton_allocate(
                                int(result.get(party) or 0), precinct_weights
                            ).items():
                                aggregates[district][party] += votes

                split_precinct_votes = split_matched_votes + split_fallback_votes
                split_match_coverage = (
                    split_matched_votes / split_precinct_votes if split_precinct_votes else 1.0
                )
                results = {
                    district: finish_result(values, candidates["dem"], candidates["rep"])
                    for district, values in sorted(aggregates.items(), key=lambda item: int(item[0]))
                }
                missing_districts = sorted(
                    expected_districts[scope] - set(results), key=int
                )
                if missing_districts:
                    raise RuntimeError(
                        f"{year} {contest_type} {scope} omitted districts: "
                        + ", ".join(missing_districts)
                    )
                file_name = f"{scope}_{contest_type}_{year}.json"
                node = {
                    "year": year,
                    "scope": scope,
                    "contest_type": contest_type,
                    "meta": {
                        "source": "Certified county/precinct totals + block CVAP + official 2022 BEFs",
                        "allocation_method": (
                            "whole county at 99.9%; split counties preserve precinct geography; "
                            "2024 block-return patterns are rescaled to certified county totals"
                        ),
                        "county_rule_threshold": NC_RULE_MIN_COVERAGE,
                        "whole_counties": len(diagnostics[scope]["whole"]),
                        "split_counties": diagnostics[scope]["split"],
                        "whole_county_vote_total": whole_county_votes,
                        "split_block_direct_vote_total": split_block_votes,
                        "split_precinct_matched_vote_total": split_matched_votes,
                        "split_county_cvap_fallback_vote_total": split_fallback_votes,
                        "split_precinct_match_coverage": round(split_match_coverage, 6),
                        "split_match_methods": dict(sorted(match_methods.items())),
                        "matched_vote_coverage": 1.0,
                        "district_coverage_complete": True,
                        "district_count": len(results),
                        "statewide_totals_preserved": True,
                        "reconciles_precinct_votes": False,
                        "precinct_name_matching": bool(split_precinct_votes),
                        "approximate": bool(diagnostics[scope]["split"]),
                    },
                    "general": {"results": results},
                }
                (OUTPUT_DIR / file_name).write_text(json.dumps(node, ensure_ascii=False, indent=2), encoding="utf-8")
                manifest.append({
                    "year": year,
                    "scope": scope,
                    "contest_type": contest_type,
                    "file": file_name,
                    "rows": len(results),
                    "coverage_pct": 100.0,
                    "county_rule": True,
                    "whole_counties": len(diagnostics[scope]["whole"]),
                    "split_counties": len(diagnostics[scope]["split"]),
                    "split_precinct_match_coverage_pct": round(split_match_coverage * 100, 2),
                    "partial": not bool(coverage_by_year.get(str(year), {}).get("complete", True)),
                    "missing_counties": coverage_by_year.get(str(year), {}).get("missing_counties", []),
                })
        print(f"{year}: wrote county-rule district slices")
    manifest.sort(key=lambda entry: (int(entry["year"]), entry["scope"], entry["contest_type"]))
    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps({"state": "Arkansas", "lines_year": 2022, "files": manifest}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(manifest)} county-rule district contest slices to {OUTPUT_DIR}")
    return 0


def write_2024_block_slices(payload: dict, manifest: list[dict]) -> None:
    if not BLOCK_2024_PATH.exists():
        return
    assignments = load_block_district_assignments()
    aggregates = {
        contest: {
            scope: defaultdict(lambda: defaultdict(float))
            for scope in SCOPES
        }
        for contest in BLOCK_2024_FIELDS
    }
    source_totals = {contest: 0 for contest in BLOCK_2024_FIELDS}
    assigned_totals = {
        contest: {scope: 0 for scope in SCOPES}
        for contest in BLOCK_2024_FIELDS
    }
    block_rows = 0
    with zipfile.ZipFile(BLOCK_2024_PATH) as archive:
        csv_name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
        stream = io.TextIOWrapper(archive.open(csv_name), encoding="utf-8-sig", newline="")
        for row in csv.DictReader(stream):
            block_rows += 1
            block = row["GEOID20"]
            for contest, fields in BLOCK_2024_FIELDS.items():
                votes = {
                    party: sum(int(row.get(field) or 0) for field in party_fields)
                    for party, party_fields in fields.items()
                }
                total = sum(votes.values())
                source_totals[contest] += total
                if not total:
                    continue
                for scope in SCOPES:
                    district = assignments[scope].get(block)
                    if not district:
                        continue
                    assigned_totals[contest][scope] += total
                    target = aggregates[contest][scope][district]
                    target["dem_votes"] += votes["dem"]
                    target["rep_votes"] += votes["rep"]
                    target["other_votes"] += votes["other"]

    manifest[:] = [entry for entry in manifest if int(entry["year"]) != 2024]
    for contest, scopes in aggregates.items():
        statewide_results = payload["results_by_year"]["2024"][
            "presidential" if contest == "president" else contest
        ]["general"]["results"]
        example = next(iter(statewide_results.values()))
        certified_total = sum(int(result.get("total_votes") or 0) for result in statewide_results.values())
        for scope, districts in scopes.items():
            results = {
                district: finish_result(values, example.get("dem_candidate", ""), example.get("rep_candidate", ""))
                for district, values in sorted(districts.items(), key=lambda item: int(item[0]))
            }
            block_coverage = assigned_totals[contest][scope] / source_totals[contest] if source_totals[contest] else 0
            certified_coverage = assigned_totals[contest][scope] / certified_total if certified_total else 0
            file_name = f"{scope}_{contest}_2024.json"
            node = {
                "year": 2024,
                "scope": scope,
                "contest_type": contest,
                "meta": {
                    "source": "Redistricting Data Hub 2024 results disaggregated to 2020 Census blocks + official 2022 BEFs",
                    "block_rows": block_rows,
                    "matched_vote_coverage": round(certified_coverage, 6),
                    "block_assignment_coverage": round(block_coverage, 6),
                    "passes_99_9_coverage": certified_coverage >= NC_RULE_MIN_COVERAGE,
                    "certified_statewide_vote_total": certified_total,
                    "source_vote_total": source_totals[contest],
                    "assigned_vote_total": assigned_totals[contest][scope],
                    "approximate": True,
                    "block_disaggregated": True,
                    "reconciles_votes": False,
                },
                "general": {"results": results},
            }
            (OUTPUT_DIR / file_name).write_text(json.dumps(node, ensure_ascii=False, indent=2), encoding="utf-8")
            manifest.append({
                "year": 2024,
                "scope": scope,
                "contest_type": contest,
                "file": file_name,
                "rows": len(results),
                "coverage_pct": round(certified_coverage * 100, 2),
                "block_assignment_coverage_pct": round(block_coverage * 100, 2),
                "passes_99_9_coverage": certified_coverage >= NC_RULE_MIN_COVERAGE,
                "vtd_vintage": 2020,
                "block_disaggregated": True,
                "partial": False,
                "missing_counties": [],
            })
            print(
                f"2024 {contest}: {scope} block coverage {block_coverage:.4%}; "
                f"certified vote coverage {certified_coverage:.4%}"
            )


def main() -> int:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    coverage_by_year = payload.get("coverage_by_year", {})
    if CVAP_BLOCK_PATH.exists() and "--precinct-model" not in sys.argv and "--block-2024-only" not in sys.argv:
        return build_county_rule_slices(payload, coverage_by_year)
    if "--block-2024-only" in sys.argv:
        manifest_path = OUTPUT_DIR / "manifest.json"
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = list(manifest_payload.get("files", []))
        write_2024_block_slices(payload, manifest)
        manifest.sort(key=lambda entry: (int(entry["year"]), entry["scope"], entry["contest_type"]))
        manifest_path.write_text(
            json.dumps({"state": "Arkansas", "lines_year": 2022, "files": manifest}, indent=2),
            encoding="utf-8",
        )
        print("Updated 2024 block-based district slices and manifest")
        return 0
    crosswalks = {vintage: load_crosswalk(vintage) for vintage in (2000, 2010, 2020)}
    reporting_2024 = load_crosswalk(2024, REPORTING_2024_CROSSWALK) if REPORTING_2024_CROSSWALK.exists() else None
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
                for scope in SCOPES
            }
            candidates = {"dem": "", "rep": ""}
            source_total = 0
            matched_total = {scope: 0 for scope in SCOPES}
            matched_precincts = {scope: 0 for scope in SCOPES}
            district_safe_precincts = {scope: 0 for scope in SCOPES}

            for precinct_key, result in source_results.items():
                county_raw, sep, precinct = precinct_key.partition(" - ")
                county = atlas.normalize_county_name(county_raw)
                total = int(result.get("total_votes") or 0)
                source_total += total
                candidates["dem"] = candidates["dem"] or result.get("dem_candidate") or ""
                candidates["rep"] = candidates["rep"] or result.get("rep_candidate") or ""
                if not sep:
                    continue
                options = []
                if year >= 2018 and reporting_2024:
                    reporting_targets, reporting_weights = reporting_2024
                    reporting_match = match_precinct(precinct, reporting_targets.get(county, []))
                    options.append((reporting_match, list(reporting_match["targets"]), reporting_weights))
                primary = match_precinct(precinct, targets_by_county.get(county, []))
                options.append((primary, list(primary["targets"]), weights))
                if 2014 <= year < 2018 and reporting_2024:
                    reporting_targets, reporting_weights = reporting_2024
                    reporting_match = match_precinct(precinct, reporting_targets.get(county, []))
                    options.append((reporting_match, list(reporting_match["targets"]), reporting_weights))
                if vintage != 2020:
                    fallback = match_precinct(precinct, fallback_targets.get(county, []))
                    options.append((fallback, list(fallback["targets"]), fallback_weights))

                direct = next(
                    (
                        (matches, active_weights)
                        for match, matches, active_weights in options
                        if str(match["method"]) in SAFE_METHODS and len(matches) == 1
                    ),
                    None,
                )
                if direct:
                    matches, active_weights = direct
                    vtd_key = str(matches[0]["vtd_key"])
                    for scope, district_weights in active_weights.get(vtd_key, {}).items():
                        matched_total[scope] += total
                        matched_precincts[scope] += 1
                        for district, weight in district_weights.items():
                            target = aggregates[scope][district]
                            target["dem_votes"] += int(result.get("dem_votes") or 0) * weight
                            target["rep_votes"] += int(result.get("rep_votes") or 0) * weight
                            target["other_votes"] += int(result.get("other_votes") or 0) * weight
                    continue

                consensus_options = []
                for match, matches, active_weights in options:
                    if str(match["method"]) not in DISTRICT_SAFE_METHODS or len(matches) < 2:
                        continue
                    assignments = district_safe_assignments(matches, active_weights)
                    consensus_options.append((assignments, active_weights))
                assignments = max(consensus_options, key=lambda item: len(item[0]), default=({}, weights))[0]
                for scope, district in assignments.items():
                    matched_total[scope] += total
                    matched_precincts[scope] += 1
                    district_safe_precincts[scope] += 1
                    target = aggregates[scope][district]
                    target["dem_votes"] += int(result.get("dem_votes") or 0)
                    target["rep_votes"] += int(result.get("rep_votes") or 0)
                    target["other_votes"] += int(result.get("other_votes") or 0)

            for scope, districts in aggregates.items():
                coverage = (matched_total[scope] / source_total) if source_total else 0.0
                if coverage < 0.60:
                    continue
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
                        "matched_precincts": matched_precincts[scope],
                        "district_safe_precincts": district_safe_precincts[scope],
                        "county_cluster_rule": {
                            "enabled": True,
                            "minimum_district_coverage": NC_RULE_MIN_COVERAGE,
                            "reconciles_votes": False,
                        },
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
                        "county_cluster_min_coverage": NC_RULE_MIN_COVERAGE,
                        "partial": not bool(coverage_by_year.get(str(year), {}).get("complete", True)),
                        "missing_counties": coverage_by_year.get(str(year), {}).get("missing_counties", []),
                    }
                )
            print(
                f"{year} {contest_type}: "
                + ", ".join(
                    f"{scope} {matched_total[scope] / source_total:.1%}"
                    for scope in SCOPES
                )
            )

    write_2024_block_slices(payload, manifest)
    manifest.sort(key=lambda entry: (int(entry["year"]), entry["scope"], entry["contest_type"]))
    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps({"state": "Arkansas", "lines_year": 2022, "files": manifest}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(manifest)} district contest slices to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
