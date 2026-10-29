#!/usr/bin/env python3
"""Aggregate Arkansas block-level CVAP demographics to atlas geographies."""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CVAP_ZIP = DATA / "ar_cvap_2024_2020_b_csv.zip"
BLOCK_ZIP = DATA / "ar_2024_gen_2020_blocks_csv.zip"
COUNTY_GEOJSON = DATA / "tl_2020_05_county20.geojson"
sys.path.insert(0, str(ROOT / ".vendor" / "pyshp"))
sys.path.insert(0, str(ROOT / ".vendor" / "shapely312"))

import shapefile  # noqa: E402
from shapely.geometry import Point, shape as shapely_shape  # noqa: E402
from shapely.strtree import STRtree  # noqa: E402

CVAP_FIELDS = {
    "cvap_tot": "CVAP_TOT24",
    "cvap_hsp": "CVAP_HSP24",
    "cvap_wht": "CVAP_WHT24",
    "cvap_bla": "CVAP_BLA24",
    "cvap_ami": "CVAP_AMI24",
    "cvap_asi": "CVAP_ASI24",
    "cvap_nhp": "CVAP_NHP24",
    "cvap_2om": "CVAP_2OM24",
}

DISTRICTS = {
    "congressional": (DATA / "05_AR_CD118.txt", "CDFP", DATA / "ar_congressional_demographics.csv"),
    "state_house": (DATA / "05_AR_SLDL22.txt", "SLDLST", DATA / "ar_state_house_demographics.csv"),
    "state_senate": (DATA / "05_AR_SLDU22.txt", "SLDUST", DATA / "ar_state_senate_demographics.csv"),
}


def int_value(row: dict[str, str], field: str) -> int:
    try:
        return int(float(row.get(field) or 0))
    except (TypeError, ValueError):
        return 0


def blank_totals() -> dict[str, int]:
    return {field: 0 for field in CVAP_FIELDS}


def add_totals(target: dict[str, int], values: dict[str, int]) -> None:
    for field in CVAP_FIELDS:
        target[field] += values[field]


def percentages(values: dict[str, int]) -> dict[str, float]:
    total = values["cvap_tot"]
    def pct(field: str) -> float:
        return round((values[field] / total) * 100, 4) if total else 0.0
    return {
        "white_vap_pct": pct("cvap_wht"),
        "black_vap_pct": pct("cvap_bla"),
        "hispanic_vap_pct": pct("cvap_hsp"),
        "native_vap_pct": pct("cvap_ami"),
        "asian_vap_pct": pct("cvap_asi"),
        "pacific_vap_pct": pct("cvap_nhp"),
        "multiracial_vap_pct": pct("cvap_2om"),
    }


def county_names() -> dict[str, str]:
    payload = json.loads(COUNTY_GEOJSON.read_text(encoding="utf-8"))
    return {
        str(feature["properties"]["COUNTYFP20"]).zfill(3): str(feature["properties"]["NAME20"])
        for feature in payload["features"]
    }


def load_block_cvap() -> dict[str, dict[str, int]]:
    values: dict[str, dict[str, int]] = {}
    with zipfile.ZipFile(CVAP_ZIP) as archive:
        name = next(item for item in archive.namelist() if item.lower().endswith(".csv"))
        with archive.open(name) as raw:
            rows = csv.DictReader((line.decode("utf-8-sig") for line in raw))
            for row in rows:
                geoid = str(row.get("GEOID20") or "").strip()
                if not geoid:
                    continue
                values[geoid] = {
                    output_field: int_value(row, source_field)
                    for output_field, source_field in CVAP_FIELDS.items()
                }
    return values


def aggregate_counties_and_precincts(
    blocks: dict[str, dict[str, int]],
) -> tuple[dict[str, dict[str, int]], dict[tuple[str, str], dict[str, int]]]:
    counties = defaultdict(blank_totals)
    precincts = defaultdict(blank_totals)
    with zipfile.ZipFile(BLOCK_ZIP) as archive:
        name = next(item for item in archive.namelist() if item.lower().endswith(".csv"))
        with archive.open(name) as raw:
            rows = csv.DictReader((line.decode("utf-8-sig") for line in raw))
            for row in rows:
                geoid = str(row.get("GEOID20") or "").strip()
                values = blocks.get(geoid)
                if values is None:
                    continue
                county_fips = str(row.get("COUNTYFP") or geoid[2:5]).zfill(3)
                raw_precinct = str(row.get("PRECINCTID") or "").strip()
                precinct_code = raw_precinct.split("-:-", 1)[-1].strip().upper()
                add_totals(counties[county_fips], values)
                if precinct_code:
                    add_totals(precincts[(county_fips, precinct_code)], values)
    return dict(counties), dict(precincts)


def zip_member(archive: zipfile.ZipFile, suffix: str) -> io.BytesIO:
    names = [name for name in archive.namelist() if name.lower().endswith(suffix)]
    if len(names) != 1:
        raise RuntimeError(f"Expected one {suffix} member in {archive.filename}")
    return io.BytesIO(archive.read(names[0]))


def aggregate_vtds(blocks: dict[str, dict[str, int]]) -> dict[tuple[str, str], dict[str, int]]:
    payload = json.loads((DATA / "tl_2020_05_vtd20.geojson").read_text(encoding="utf-8"))
    grouped: dict[str, list[tuple[object, str]]] = defaultdict(list)
    for feature in payload["features"]:
        props = feature.get("properties") or {}
        county = str(props.get("COUNTYFP20") or "").zfill(3)
        name = str(props.get("NAME20") or props.get("VTDST20") or "").strip().upper()
        grouped[county].append((shapely_shape(feature["geometry"]), name))
    indexes = {
        county: (STRtree([item[0] for item in items]), [item[1] for item in items])
        for county, items in grouped.items()
    }

    archive = zipfile.ZipFile(DATA / "tl_2020_05_tabblock20.zip")
    reader = shapefile.Reader(
        shp=zip_member(archive, ".shp"),
        shx=zip_member(archive, ".shx"),
        dbf=zip_member(archive, ".dbf"),
        encoding="latin1",
    )
    vtds = defaultdict(blank_totals)
    for item in reader.iterShapeRecords():
        props = item.record.as_dict()
        geoid = str(props.get("GEOID20") or props.get("GEOID") or "").strip()
        values = blocks.get(geoid)
        county = str(props.get("COUNTYFP20") or props.get("COUNTYFP") or "").zfill(3)
        index = indexes.get(county)
        if values is None or index is None:
            continue
        lon = float(props.get("INTPTLON20") or props.get("INTPTLON"))
        lat = float(props.get("INTPTLAT20") or props.get("INTPTLAT"))
        point = Point(lon, lat)
        tree, names = index
        candidates = tree.query(point)
        match_index = next(
            (int(idx) for idx in candidates if tree.geometries[int(idx)].covers(point)),
            None,
        )
        if match_index is None:
            nearest = tree.nearest(point)
            match_index = int(nearest) if nearest is not None else None
        if match_index is not None:
            add_totals(vtds[(county, names[match_index])], values)
    archive.close()
    return dict(vtds)


def aggregate_districts(
    blocks: dict[str, dict[str, int]], path: Path, district_field: str
) -> dict[str, dict[str, int]]:
    districts = defaultdict(blank_totals)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            geoid = str(row.get("GEOID") or "").strip()
            district = str(row.get(district_field) or "").strip().lstrip("0") or "0"
            values = blocks.get(geoid)
            if values is not None:
                add_totals(districts[district], values)
    return dict(districts)


def write_district_csv(path: Path, rows: dict[str, dict[str, int]]) -> None:
    fields = [
        "district", "total_population", "cvap_total_24",
        "white_vap_pct", "black_vap_pct", "hispanic_vap_pct", "native_vap_pct",
        "asian_vap_pct", "pacific_vap_pct", "multiracial_vap_pct",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for district in sorted(rows, key=lambda value: int(value)):
            values = rows[district]
            writer.writerow({
                "district": district,
                "total_population": values["cvap_tot"],
                "cvap_total_24": values["cvap_tot"],
                **percentages(values),
            })


def main() -> int:
    names = county_names()
    blocks = load_block_cvap()
    counties, reporting_precincts = aggregate_counties_and_precincts(blocks)
    precincts = aggregate_vtds(blocks)

    aggregate_payload = {
        "state": "Arkansas",
        "source": "2020-2024 ACS CVAP special tabulation, block-disaggregated estimates",
        "geography_vintage": 2020,
        "reporting_precinct_count": len(reporting_precincts),
        "counties": {},
        "precincts": {},
    }
    county_demo = {
        "state": "Arkansas",
        "source": aggregate_payload["source"],
        "metric": "CVAP",
        "counties": {},
    }
    for county_fips in sorted(counties):
        values = counties[county_fips]
        name = names.get(county_fips, county_fips)
        aggregate_payload["counties"][county_fips] = {"county_name": name, **values}
        pct = percentages(values)
        county_demo["counties"][name.upper()] = {
            "county": name,
            "cvap_18plus": values["cvap_tot"],
            "vap_18plus": values["cvap_tot"],
            "white_pop_pct": pct["white_vap_pct"],
            "black_pop_pct": pct["black_vap_pct"],
            "hispanic_pop_pct": pct["hispanic_vap_pct"],
            "native_pop_pct": pct["native_vap_pct"],
            "asian_pop_pct": pct["asian_vap_pct"],
            "pacific_pop_pct": pct["pacific_vap_pct"],
            "multiracial_pop_pct": pct["multiracial_vap_pct"],
        }
    for (county_fips, precinct_code), values in sorted(precincts.items()):
        aggregate_payload["precincts"][f"{county_fips}-{precinct_code}"] = values

    (DATA / "ar_cvap_2024_aggregates.json").write_text(
        json.dumps(aggregate_payload, indent=2) + "\n", encoding="utf-8"
    )
    (DATA / "ar_county_demographics.json").write_text(
        json.dumps(county_demo, indent=2) + "\n", encoding="utf-8"
    )

    district_count = 0
    for _scope, (bef_path, field, output_path) in DISTRICTS.items():
        district_rows = aggregate_districts(blocks, bef_path, field)
        district_count += len(district_rows)
        write_district_csv(output_path, district_rows)

    print(
        f"Wrote demographics for {len(counties)} counties, {len(precincts)} VTDs, "
        f"and {district_count} districts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
