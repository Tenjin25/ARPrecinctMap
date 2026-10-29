from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from pathlib import Path

import build_ar_atlas_election_json as atlas


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCE = DATA_DIR / "ar_2024_gen_2020_blocks_csv.zip"
OUTPUT = DATA_DIR / "crosswalks" / "precinct2024_to_2022_districts.csv"
SCOPES = {
    "congressional": (DATA_DIR / "05_AR_CD118.txt", "CDFP"),
    "state_house": (DATA_DIR / "05_AR_SLDL22.txt", "SLDLST"),
    "state_senate": (DATA_DIR / "05_AR_SLDU22.txt", "SLDUST"),
}


def main() -> int:
    assignments: dict[str, dict[str, str]] = {}
    for scope, (path, field) in SCOPES.items():
        with path.open(newline="", encoding="utf-8-sig") as source:
            assignments[scope] = {
                row["GEOID"]: str(int(row[field]))
                for row in csv.DictReader(source)
                if row.get("GEOID") and row.get(field)
            }

    weighted = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    block_counts = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    metadata: dict[str, tuple[str, str]] = {}
    with zipfile.ZipFile(SOURCE) as archive:
        csv_name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
        stream = io.TextIOWrapper(archive.open(csv_name), encoding="utf-8-sig", newline="")
        for row in csv.DictReader(stream):
            precinct_id = (row.get("PRECINCTID") or "").strip()
            county_raw, separator, precinct = precinct_id.partition("-:-")
            county = atlas.normalize_county_name(county_raw)
            precinct = " ".join(precinct.strip().upper().split())
            if not separator or not county or not precinct:
                continue
            key = f"{county}|{precinct}"
            metadata[key] = (county, precinct)
            block = row["GEOID20"]
            vap = float(row.get("VAP_MOD") or 0)
            for scope in SCOPES:
                district = assignments[scope].get(block)
                if not district:
                    continue
                weighted[key][scope][district] += vap
                block_counts[key][scope][district] += 1

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as output:
        fields = ["vtd_year", "county_fips", "county", "vtd_code", "vtd_name", "scope", "district", "weight"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for key, (county, precinct) in sorted(metadata.items()):
            for scope in SCOPES:
                values = weighted[key][scope]
                total = sum(values.values())
                if total <= 0:
                    values = block_counts[key][scope]
                    total = sum(values.values())
                for district, value in sorted(values.items(), key=lambda item: int(item[0])):
                    writer.writerow({
                        "vtd_year": 2024,
                        "county_fips": "",
                        "county": county,
                        "vtd_code": precinct,
                        "vtd_name": precinct,
                        "scope": scope,
                        "district": district,
                        "weight": f"{value / total:.10f}",
                    })
    print(f"Wrote {OUTPUT} ({len(metadata):,} reporting precincts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
