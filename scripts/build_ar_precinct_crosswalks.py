from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import build_ar_atlas_election_json as atlas


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
VTD_PATH = DATA_DIR / "tl_2020_05_vtd20.geojson"
AUDIT_PATH = DATA_DIR / "ar_precinct_crosswalks.csv"
ALIASES_PATH = DATA_DIR / "ar_precinct_aliases.csv"

PUBLISHED_YEARS = {2008, 2010, 2012, 2014, 2016, 2018, 2020, 2022, 2024}
NON_GEOGRAPHIC = re.compile(
    r"\b(ABSENTEE|EARLY|PROVISIONAL|OVERSEAS|UOCAVA|FAILSAFE|TOTAL|"
    r"CENTRAL COUNT|MAIL|PAPER BALLOT)\b",
    re.I,
)


def clean_label(value: str) -> str:
    text = " ".join((value or "").strip().upper().split())
    text = text.replace("&", " AND ")
    text = re.sub(r"\bPCT\b|\bPRCT\b", " PRECINCT ", text)
    text = re.sub(r"\bTWP\b", " TOWNSHIP ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return " ".join(text.split())


def canonical_label(value: str) -> str:
    text = clean_label(value)
    text = re.sub(r"^PRECINCT\s+", "", text)
    # Census VTD names commonly prefix a local sequence code, e.g.
    # "15 - Almyra" while election exports report simply "Almyra".
    text = re.sub(r"^\d+\s+(?=[A-Z])", "", text)
    tokens = []
    for token in text.split():
        if token.isdigit():
            token = str(int(token))
        else:
            match = re.fullmatch(r"0*(\d+)([A-Z]+)", token)
            if match:
                token = f"{int(match.group(1))}{match.group(2)}"
        tokens.append(token)
    return " ".join(tokens)


def structural_label(value: str) -> str:
    """Normalize administrative words without guessing at similar names."""
    tokens = canonical_label(value).split()
    return " ".join(token for token in tokens if token not in {"PRECINCT", "WARD"})


def abbreviation_variants(value: str) -> set[str]:
    """Return deterministic abbreviations used by county election exports."""
    tokens = canonical_label(value).split()
    if not tokens:
        return set()
    variants = {
        "".join(token[0] for token in tokens),
        " ".join(token[0] for token in tokens),
        " ".join(token[:3] for token in tokens),
    }
    if len(tokens) > 1:
        variants.add("".join(token[0] for token in tokens[:-1]) + " " + tokens[-1])
        variants.add(" ".join(token[:3] for token in tokens[:-1]) + " " + tokens[-1])
    return {variant for variant in variants if len(variant.replace(" ", "")) >= 2}


def load_vtds() -> dict[str, list[dict[str, str]]]:
    payload = json.loads(VTD_PATH.read_text(encoding="utf-8"))
    by_county: dict[str, list[dict[str, str]]] = defaultdict(list)
    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        county = atlas.normalize_county_name(str(props.get("county_nam") or ""))
        name = str(props.get("prec_id") or props.get("NAME20") or "").strip()
        code = str(props.get("VTDST20") or "").strip()
        if county and name:
            by_county[county].append(
                {
                    "name": name,
                    "code": code,
                    "clean": clean_label(name),
                    "canonical": canonical_label(name),
                    "structural": structural_label(name),
                    "abbreviations": abbreviation_variants(name),
                }
            )
    return by_county


def load_election_precincts() -> dict[int, dict[str, set[str]]]:
    out: dict[int, dict[str, set[str]]] = {}
    for year, paths in atlas.INPUTS.items():
        if year not in PUBLISHED_YEARS:
            continue
        counties: dict[str, set[str]] = defaultdict(set)
        for path in paths:
            with path.open(newline="", encoding="utf-8-sig") as source:
                for row in csv.DictReader(source):
                    if (row.get("office") or "").strip() not in atlas.OFFICE_TYPES:
                        continue
                    county = atlas.normalize_county_name(row.get("county") or "")
                    precinct = " ".join((row.get("precinct") or "").strip().upper().split())
                    if county and precinct:
                        counties[county].add(precinct)
        out[year] = counties
    return out


def match_precinct(label: str, targets: list[dict[str, str]]) -> dict[str, object]:
    clean = clean_label(label)
    canonical = canonical_label(label)
    if NON_GEOGRAPHIC.search(clean):
        return {"method": "non_geographic", "score": 0.0, "targets": []}

    exact = [item for item in targets if item["clean"] == clean]
    if len(exact) == 1:
        return {"method": "exact", "score": 1.0, "targets": exact}

    # County systems often preserve the same precinct code with different
    # punctuation: "5-F3-50", "5F350", and "PRECINCT 5F350" are equivalent.
    # Check this before canonical matching, which intentionally strips leading
    # sequence numbers from labels like "15 - Almyra" and would turn "2-A"
    # into the ambiguous label "A".
    source_compact = re.sub(r"[^A-Z0-9]", "", (label or "").upper())
    source_compact = re.sub(r"^PRECINCT", "", source_compact)
    if len(source_compact) >= 2 and re.search(r"\d", source_compact) and re.search(r"[A-Z]", source_compact):
        compact_matches = []
        for item in targets:
            target_compact = re.sub(r"[^A-Z0-9]", "", (item.get("name") or "").upper())
            target_compact = re.sub(r"^PRECINCT", "", target_compact)
            if target_compact == source_compact:
                compact_matches.append(item)
        if len(compact_matches) == 1:
            return {"method": "compact_code", "score": 0.988, "targets": compact_matches}
        if len(compact_matches) > 1:
            return {"method": "compact_code_group", "score": 0.987, "targets": compact_matches}

        # Garland County historically reported ballot-style suffixes such as
        # "09R" while later Census VTDs encode the same precinct family as
        # 091-095.  When the target set is purely numeric, retain every VTD
        # in that two-digit family instead of treating the row as unmatched.
        family = re.fullmatch(r"0*(\d{1,2})[A-Z][A-Z0-9]*", source_compact)
        if family:
            family_number = int(family.group(1))
            family_matches = []
            for item in targets:
                target_name = clean_label(item.get("name") or "")
                if not re.fullmatch(r"\d{3}", target_name):
                    continue
                if int(target_name) // 10 == family_number:
                    family_matches.append(item)
            if len(family_matches) == 1:
                return {"method": "numeric_code", "score": 0.976, "targets": family_matches}
            if len(family_matches) > 1:
                return {"method": "numeric_family_group", "score": 0.972, "targets": family_matches}

    canonical_matches = [item for item in targets if item["canonical"] == canonical]
    if len(canonical_matches) == 1:
        return {"method": "canonical", "score": 0.99, "targets": canonical_matches}
    if len(canonical_matches) > 1:
        return {"method": "ambiguous_canonical", "score": 0.99, "targets": canonical_matches}

    structural = structural_label(label)
    structural_matches = [
        item for item in targets
        if (item["structural"] if "structural" in item else structural_label(item["name"])) == structural
    ]
    if len(structural_matches) == 1:
        return {"method": "structural", "score": 0.985, "targets": structural_matches}
    if len(structural_matches) > 1:
        return {"method": "ambiguous_structural", "score": 0.985, "targets": structural_matches}

    abbreviation_matches = [
        item for item in targets
        if canonical in (item["abbreviations"] if "abbreviations" in item else abbreviation_variants(item["name"]))
    ]
    if len(abbreviation_matches) == 1:
        return {"method": "abbreviation", "score": 0.98, "targets": abbreviation_matches}
    if len(abbreviation_matches) > 1:
        return {"method": "ambiguous_abbreviation", "score": 0.98, "targets": abbreviation_matches}

    # Some county exports combine a municipality or numbered group that is
    # represented by several VTDs.  Preserve all candidates so the district
    # builder can use the result only when every candidate is district-safe.
    structural_group = [
        item for item in targets
        if len(structural) >= 4
        and not any(token.isdigit() for token in structural.split())
        and (item["structural"] if "structural" in item else structural_label(item["name"])).startswith(structural + " ")
    ]
    if len(structural_group) == 1:
        return {"method": "structural_prefix", "score": 0.971, "targets": structural_group}
    if len(structural_group) > 1:
        return {"method": "structural_group", "score": 0.97, "targets": structural_group}

    # Older county exports often identify a polling place by one or more
    # precinct numbers ("056/058 WINFIELD UMC"), while Census VTDs retain
    # lettered pieces ("PCT 056A").  Treat all listed numeric pieces as one
    # reporting group; the district builder combines them by their basis/CVAP.
    clean_tokens = clean.split()
    source_codes: list[int] = []
    for token in clean_tokens:
        if token.isdigit():
            source_codes.append(int(token))
        else:
            break
    if source_codes:
        numeric_matches = []
        for item in targets:
            target_tokens = item["clean"].split()
            if target_tokens and target_tokens[0] == "PRECINCT":
                target_tokens = target_tokens[1:]
            first = target_tokens[0] if target_tokens else ""
            number = re.fullmatch(r"0*(\d+)[A-Z]*", first)
            if number and int(number.group(1)) in source_codes:
                numeric_matches.append(item)
        if len(numeric_matches) == 1:
            return {"method": "numeric_code", "score": 0.975, "targets": numeric_matches}
        if len(numeric_matches) > 1:
            return {"method": "numeric_group", "score": 0.97, "targets": numeric_matches}

    source_tokens = canonical.split()
    month_tokens = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
    if source_tokens and source_tokens[0].isdigit() and all(
        token in month_tokens for token in source_tokens[1:]
    ):
        prefix_matches = [
            item for item in targets
            if item["canonical"].split()
            and item["canonical"].split()[0] == source_tokens[0]
        ]
        if len(prefix_matches) > 1:
            return {"method": "numeric_group", "score": 0.97, "targets": prefix_matches}

    contained = [
        item for item in targets
        if len(canonical) >= 5
        and (canonical in item["canonical"] or item["canonical"] in canonical)
    ]
    if len(contained) == 1:
        return {"method": "containment", "score": 0.95, "targets": contained}
    if len(contained) > 1:
        return {"method": "ambiguous_containment", "score": 0.95, "targets": contained}

    scored = sorted(
        [
            (
                SequenceMatcher(None, canonical, item["canonical"]).ratio(),
                item,
            )
            for item in targets
            if canonical and item["canonical"]
        ],
        key=lambda pair: pair[0],
    )
    if not scored:
        return {"method": "unmatched", "score": 0.0, "targets": []}
    best_score, best = scored[-1]
    runner_up = scored[-2][0] if len(scored) > 1 else 0.0
    if best_score >= 0.94 and best_score - runner_up >= 0.08:
        return {"method": "fuzzy_high", "score": best_score, "targets": [best]}
    return {
        "method": "ambiguous" if best_score >= 0.80 else "unmatched",
        "score": best_score,
        "targets": [best] if best_score >= 0.80 else [],
    }


def main() -> int:
    vtds = load_vtds()
    elections = load_election_precincts()
    audit_rows: list[dict[str, object]] = []
    alias_rows: list[dict[str, str]] = []
    safe_methods = {"exact", "canonical", "structural", "abbreviation", "containment", "fuzzy_high"}

    for year in sorted(elections):
        stats: dict[str, int] = defaultdict(int)
        for county in sorted(elections[year]):
            targets = vtds.get(county, [])
            for precinct in sorted(elections[year][county]):
                result = match_precinct(precinct, targets)
                method = str(result["method"])
                matches = list(result["targets"])
                stats[method] += 1
                best = matches[0] if matches else {}
                audit_rows.append(
                    {
                        "election_year": year,
                        "county_norm": county,
                        "election_precinct": precinct,
                        "vtd_year": 2020,
                        "vtd_code": best.get("code", ""),
                        "vtd_name": best.get("name", ""),
                        "match_method": method,
                        "match_score": f"{float(result['score']):.4f}",
                        "candidate_count": len(matches),
                    }
                )
                if method in safe_methods and len(matches) == 1:
                    alias_rows.append(
                        {
                            "county_norm": county,
                            "precinct_norm": precinct,
                            "canonical_label": str(best["name"]),
                            "prctseq_2024": str(best["name"]),
                            "election_year": str(year),
                            "match_method": method,
                            "match_score": f"{float(result['score']):.4f}",
                        }
                    )
        matched = sum(stats[m] for m in safe_methods)
        total = sum(stats.values())
        print(f"{year}: {matched}/{total} safely matched ({matched / total:.1%}) {dict(stats)}")

    with AUDIT_PATH.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(audit_rows[0]))
        writer.writeheader()
        writer.writerows(audit_rows)

    with ALIASES_PATH.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(alias_rows[0]))
        writer.writeheader()
        writer.writerows(alias_rows)

    print(f"Wrote {AUDIT_PATH} ({len(audit_rows)} rows)")
    print(f"Wrote {ALIASES_PATH} ({len(alias_rows)} conservative aliases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
