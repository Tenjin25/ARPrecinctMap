from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUT_DIR = DATA_DIR / "crosswalks"
sys.path.insert(0, str(ROOT / ".vendor" / "pyshp"))
sys.path.insert(0, str(ROOT / ".vendor" / "shapely312"))

import shapefile  # noqa: E402
from shapely.geometry import Point, shape as shapely_shape  # noqa: E402
from shapely.strtree import STRtree  # noqa: E402


SCOPES = {
    "congressional": DATA_DIR / "05_AR_CD118.txt",
    "state_house": DATA_DIR / "05_AR_SLDL22.txt",
    "state_senate": DATA_DIR / "05_AR_SLDU22.txt",
}


def zip_member(archive: zipfile.ZipFile, suffix: str) -> io.BytesIO:
    names = [name for name in archive.namelist() if name.lower().endswith(suffix)]
    if len(names) != 1:
        raise RuntimeError(f"Expected one {suffix} member in {archive.filename}")
    return io.BytesIO(archive.read(names[0]))


def open_reader(path: Path) -> tuple[zipfile.ZipFile, shapefile.Reader]:
    archive = zipfile.ZipFile(path)
    reader = shapefile.Reader(
        shp=zip_member(archive, ".shp"),
        shx=zip_member(archive, ".shx"),
        dbf=zip_member(archive, ".dbf"),
        encoding="latin1",
    )
    return archive, reader


def county_names() -> dict[str, str]:
    payload = json.loads((DATA_DIR / "tl_2020_05_county20.geojson").read_text(encoding="utf-8"))
    return {
        str(feature["properties"]["COUNTYFP20"]).zfill(3): feature["properties"]["NAME20"]
        for feature in payload["features"]
    }


def build_vtd_indexes() -> tuple[dict[int, dict[str, tuple[STRtree, list[dict]]]], dict[int, dict[str, dict]]]:
    indexes: dict[int, dict[str, tuple[STRtree, list[dict]]]] = {2000: {}, 2010: {}, 2020: {}}
    metadata: dict[int, dict[str, dict]] = {2000: {}, 2010: {}, 2020: {}}
    names = county_names()

    sources = {
        2000: (DATA_DIR / "ar_vtds_2008.geojson", "COUNTYFP00", "VTDST00", "NAME00"),
        2020: (DATA_DIR / "tl_2020_05_vtd20.geojson", "COUNTYFP20", "VTDST20", "NAME20"),
    }
    for year, (path, county_field, code_field, name_field) in sources.items():
        payload = json.loads(path.read_text(encoding="utf-8"))
        grouped: dict[str, list[tuple[object, dict]]] = defaultdict(list)
        for feature in payload["features"]:
            props = feature.get("properties") or {}
            county = str(props.get(county_field) or "").zfill(3)
            code = str(props.get(code_field) or "").strip()
            name = str(props.get(name_field) or code).strip()
            key = f"05{county}{code}"
            meta = {"county_fips": county, "county": names.get(county, county), "code": code, "name": name}
            grouped[county].append((shapely_shape(feature["geometry"]), meta))
            metadata[year][key] = meta
        for county, items in grouped.items():
            geometries = [item[0] for item in items]
            indexes[year][county] = (STRtree(geometries), [item[1] for item in items])

    archive, reader = open_reader(DATA_DIR / "tl_2012_05_vtd10.zip")
    grouped10: dict[str, list[tuple[object, dict]]] = defaultdict(list)
    for item in reader.iterShapeRecords():
        props = item.record.as_dict()
        county = str(props.get("COUNTYFP10") or "").zfill(3)
        code = str(props.get("VTDST10") or "").strip()
        name = str(props.get("NAME10") or code).strip()
        key = f"05{county}{code}"
        meta = {"county_fips": county, "county": names.get(county, county), "code": code, "name": name}
        grouped10[county].append((shapely_shape(item.shape.__geo_interface__), meta))
        metadata[2010][key] = meta
    archive.close()
    for county, items in grouped10.items():
        geometries = [item[0] for item in items]
        indexes[2010][county] = (STRtree(geometries), [item[1] for item in items])
    return indexes, metadata


def locate_vtd(point: Point, county: str, index: dict[str, tuple[STRtree, list[dict]]]) -> dict | None:
    entry = index.get(county)
    if not entry:
        return None
    tree, metadata = entry
    for idx in tree.query(point):
        geom = tree.geometries[int(idx)]
        if geom.covers(point):
            return metadata[int(idx)]
    nearest = tree.nearest(point)
    return metadata[int(nearest)] if nearest is not None else None


def block_to_vtd(year: int, index: dict[str, tuple[STRtree, list[dict]]]) -> dict[str, str]:
    paths = {
        2000: DATA_DIR / "tl_2008_05_tabblock00.zip",
        2010: DATA_DIR / "tl_2012_05_tabblock10.zip",
        2020: DATA_DIR / "tl_2020_05_tabblock20.zip",
    }
    archive, reader = open_reader(paths[year])
    out: dict[str, str] = {}
    for item in reader.iterShapeRecords():
        props = item.record.as_dict()
        if year == 2000:
            block = str(props.get("BLKIDFP00") or "")
            county = str(props.get("COUNTYFP00") or "").zfill(3)
            point = shapely_shape(item.shape.__geo_interface__).representative_point()
        else:
            suffix = "10" if year == 2010 else "20"
            block = str(props.get("GEOID") or props.get(f"GEOID{suffix}") or "")
            county = str(props.get("COUNTYFP") or props.get(f"COUNTYFP{suffix}") or "").zfill(3)
            lon = float(props.get("INTPTLON") or props.get(f"INTPTLON{suffix}"))
            lat = float(props.get("INTPTLAT") or props.get(f"INTPTLAT{suffix}"))
            point = Point(lon, lat)
        meta = locate_vtd(point, county, index)
        if block and meta:
            out[block] = f"05{meta['county_fips']}{meta['code']}"
    archive.close()
    print(f"{year}: assigned {len(out):,} blocks to VTDs")
    return out


def load_befs() -> dict[str, dict[str, str]]:
    by_scope: dict[str, dict[str, str]] = {}
    for scope, path in SCOPES.items():
        with path.open(newline="", encoding="utf-8-sig") as source:
            rows = csv.DictReader(source)
            district_field = next(field for field in rows.fieldnames or [] if field != "GEOID")
            by_scope[scope] = {
                row["GEOID"]: str(int(row[district_field])) if row[district_field].isdigit() else row[district_field]
                for row in rows
            }
        print(f"{scope}: loaded {len(by_scope[scope]):,} BEF rows")
    return by_scope


def read_nhgis(path: Path):
    archive = zipfile.ZipFile(path)
    csv_name = next(name for name in archive.namelist() if name.lower().endswith(".csv"))
    stream = io.TextIOWrapper(archive.open(csv_name), encoding="utf-8-sig", newline="")
    return archive, csv.DictReader(stream)


def add_mix(target: dict, key: str, scope: str, district: str, value: float) -> None:
    target[key][(scope, district)] += value


def normalize_vtd_mix(raw_mix: dict[str, dict[tuple[str, str], float]]) -> dict[str, dict[tuple[str, str], float]]:
    out: dict[str, dict[tuple[str, str], float]] = {}
    for vtd, values in raw_mix.items():
        scope_totals: dict[str, float] = defaultdict(float)
        for (scope, _district), value in values.items():
            scope_totals[scope] += value
        out[vtd] = {
            (scope, district): value / scope_totals[scope]
            for (scope, district), value in values.items()
            if scope_totals[scope] > 0
        }
    return out


def write_crosswalk(
    year: int,
    mix: dict,
    metadata: dict[str, dict],
    basis_totals: dict[str, dict[str, float]] | None = None,
    basis_labels: dict[str, dict[str, str]] | None = None,
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"vtd{year}_to_2022_districts.csv"
    with path.open("w", newline="", encoding="utf-8") as output:
        fields = [
            "vtd_year", "county_fips", "county", "vtd_code", "vtd_name",
            "scope", "district", "weight", "basis_total", "weight_basis",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for vtd, values in sorted(mix.items()):
            meta = metadata.get(vtd) or {}
            for (scope, district), weight in sorted(values.items()):
                writer.writerow(
                    {
                        "vtd_year": year,
                        "county_fips": meta.get("county_fips", ""),
                        "county": meta.get("county", ""),
                        "vtd_code": meta.get("code", ""),
                        "vtd_name": meta.get("name", ""),
                        "scope": scope,
                        "district": district,
                        "weight": f"{weight:.10f}",
                        "basis_total": f"{(basis_totals or {}).get(vtd, {}).get(scope, 1.0):.6f}",
                        "weight_basis": (basis_labels or {}).get(vtd, {}).get(scope, "legacy block crosswalk"),
                    }
                )
    print(f"Wrote {path} ({len(mix):,} VTDs)")


def main() -> int:
    indexes, metadata = build_vtd_indexes()
    befs = load_befs()

    cvap_by_block: dict[str, float] = {}
    archive, rows = read_nhgis(DATA_DIR / "ar_cvap_2024_2020_b_csv.zip")
    for row in rows:
        cvap_by_block[row["GEOID20"]] = float(row.get("CVAP_TOT24") or 0)
    archive.close()

    raw_cvap = {
        year: defaultdict(lambda: defaultdict(float))
        for year in (2000, 2010, 2020)
    }
    raw_blocks = {
        year: defaultdict(lambda: defaultdict(float))
        for year in (2000, 2010, 2020)
    }
    archive, reader = open_reader(DATA_DIR / "tl_2020_05_tabblock20.zip")
    processed = 0
    for item in reader.iterShapeRecords():
        props = item.record.as_dict()
        block = str(props.get("GEOID20") or props.get("GEOID") or "")
        county = str(props.get("COUNTYFP20") or props.get("COUNTYFP") or "").zfill(3)
        if not block or not county:
            continue
        lon = float(props.get("INTPTLON20") or props.get("INTPTLON"))
        lat = float(props.get("INTPTLAT20") or props.get("INTPTLAT"))
        point = Point(lon, lat)
        cvap = cvap_by_block.get(block, 0.0)
        for year in (2000, 2010, 2020):
            meta = locate_vtd(point, county, indexes[year])
            if not meta:
                continue
            vtd = f"05{meta['county_fips']}{meta['code']}"
            for scope, assignments in befs.items():
                district = assignments.get(block)
                if not district:
                    continue
                add_mix(raw_cvap[year], vtd, scope, district, cvap)
                add_mix(raw_blocks[year], vtd, scope, district, 1.0)
        processed += 1
    archive.close()
    print(f"Assigned {processed:,} current blocks to each historical VTD vintage")

    for year in (2000, 2010, 2020):
        chosen: dict[str, dict[tuple[str, str], float]] = defaultdict(lambda: defaultdict(float))
        basis_totals: dict[str, dict[str, float]] = defaultdict(dict)
        basis_labels: dict[str, dict[str, str]] = defaultdict(dict)
        all_vtds = set(raw_cvap[year]) | set(raw_blocks[year])
        for vtd in all_vtds:
            for scope in SCOPES:
                cvap_values = {
                    district: value
                    for (candidate_scope, district), value in raw_cvap[year][vtd].items()
                    if candidate_scope == scope and value > 0
                }
                block_values = {
                    district: value
                    for (candidate_scope, district), value in raw_blocks[year][vtd].items()
                    if candidate_scope == scope and value > 0
                }
                values = cvap_values or block_values
                total = sum(values.values())
                if total <= 0:
                    continue
                basis_totals[vtd][scope] = total
                basis_labels[vtd][scope] = "2024 CVAP" if cvap_values else "block-count fallback"
                for district, value in values.items():
                    chosen[vtd][(scope, district)] = value / total
        write_crosswalk(year, chosen, metadata[year], basis_totals, basis_labels)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
