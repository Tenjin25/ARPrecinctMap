from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
sys.path.insert(0, str(ROOT / ".vendor" / "pyshp"))

import shapefile  # noqa: E402


LAYERS = [
    ("tl_2020_05_county20.zip", "tl_2020_05_county20.geojson", "county"),
    ("tl_2020_05_vtd20.zip", "tl_2020_05_vtd20.geojson", "vtd"),
    ("tl_2022_05_cd118.zip", "tl_2022_05_cd118.geojson", "congressional"),
    ("tl_2022_05_sldl.zip", "tl_2022_05_sldl.geojson", "state_house"),
    ("tl_2022_05_sldu.zip", "tl_2022_05_sldu.geojson", "state_senate"),
]


def zip_member(archive: zipfile.ZipFile, suffix: str) -> io.BytesIO:
    matches = [name for name in archive.namelist() if name.lower().endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {suffix} in {archive.filename}, found {len(matches)}")
    return io.BytesIO(archive.read(matches[0]))


def open_reader(archive_path: Path) -> shapefile.Reader:
    archive = zipfile.ZipFile(archive_path)
    reader = shapefile.Reader(
        shp=zip_member(archive, ".shp"),
        shx=zip_member(archive, ".shx"),
        dbf=zip_member(archive, ".dbf"),
        encoding="latin1",
    )
    reader._source_archive = archive
    return reader


def district_number(properties: dict, candidates: list[str]) -> str:
    for field in candidates:
        raw = str(properties.get(field, "")).strip()
        if raw:
            return str(int(raw)) if raw.isdigit() else raw
    return ""


def normalize_properties(kind: str, properties: dict, county_names: dict[str, str]) -> dict:
    out = dict(properties)
    if kind == "county":
        county_name = str(out.get("NAME20") or out.get("NAME") or "").strip()
        out["County"] = county_name
        out["county_nam"] = county_name
        out["COUNTYNAME"] = county_name
    elif kind == "vtd":
        county_fips = str(out.get("COUNTYFP20") or "").zfill(3)
        county_name = county_names.get(county_fips, county_fips)
        vtd_code = str(out.get("VTDST20") or "").strip()
        short_code = vtd_code.lstrip("0") or "0"
        census_name = str(out.get("NAME20") or out.get("NAMELSAD20") or short_code).strip()
        out["county_nam"] = county_name
        out["county_norm"] = county_name.upper()
        # Use the Census VTD name as the map key. Arkansas election exports
        # generally report human-readable precinct names (for example,
        # "Precinct 101" or "Wynne Township") rather than the six-digit
        # Census VTD code. The original code remains available as VTDST20.
        out["prec_id"] = census_name
        out["precinct_full_name"] = census_name
        out["precinct_name"] = f"{county_name} - {census_name}"
        out["precinct_norm"] = out["precinct_name"].upper()
    elif kind == "congressional":
        number = district_number(out, ["CD118FP", "CDSESSN"])
        out["DISTRICT"] = number
        out["district_id"] = number
    elif kind == "state_house":
        number = district_number(out, ["SLDLST"])
        out["DISTRICT"] = number
        out["district_id"] = number
    elif kind == "state_senate":
        number = district_number(out, ["SLDUST"])
        out["DISTRICT"] = number
        out["district_id"] = number
    return out


def convert_layer(
    archive_name: str,
    output_name: str,
    kind: str,
    county_names: dict[str, str],
) -> tuple[int, list[float], list[dict]]:
    archive_path = DATA_DIR / archive_name
    output_path = DATA_DIR / output_name
    if not archive_path.exists():
        raise FileNotFoundError(archive_path)

    reader = open_reader(archive_path)
    temporary = output_path.with_suffix(output_path.suffix + ".part")
    bounds = [float("inf"), float("inf"), float("-inf"), float("-inf")]
    count = 0
    centroid_features: list[dict] = []

    with temporary.open("w", encoding="utf-8", newline="") as output:
        output.write('{"type":"FeatureCollection","features":[')
        first = True
        for shape_record in reader.iterShapeRecords():
            shape = shape_record.shape
            if shape.shapeType == shapefile.NULL:
                continue
            properties = normalize_properties(kind, shape_record.record.as_dict(), county_names)
            feature = {
                "type": "Feature",
                "properties": properties,
                "geometry": shape.__geo_interface__,
            }
            if not first:
                output.write(",")
            output.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")))
            first = False
            count += 1

            bbox = shape.bbox
            bounds[0] = min(bounds[0], bbox[0])
            bounds[1] = min(bounds[1], bbox[1])
            bounds[2] = max(bounds[2], bbox[2])
            bounds[3] = max(bounds[3], bbox[3])

            if kind == "vtd":
                lon = float(properties.get("INTPTLON20") or ((bbox[0] + bbox[2]) / 2))
                lat = float(properties.get("INTPTLAT20") or ((bbox[1] + bbox[3]) / 2))
                centroid_features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "county_nam": properties["county_nam"],
                            "county_norm": properties["county_norm"],
                            "prec_id": properties["prec_id"],
                            "precinct_name": properties["precinct_name"],
                            "precinct_norm": properties["precinct_norm"],
                            "precinct_full_name": properties["precinct_full_name"],
                            "has_polygon": True,
                        },
                        "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    }
                )

        output.write(f'],"bbox":{json.dumps(bounds, separators=(",", ":"))}}}')

    reader._source_archive.close()
    temporary.replace(output_path)
    return count, bounds, centroid_features


def main() -> int:
    county_reader = open_reader(DATA_DIR / "tl_2020_05_county20.zip")
    county_names = {
        str(record.get("COUNTYFP20") or record.get("COUNTYFP") or "").zfill(3):
        str(record.get("NAME20") or record.get("NAME") or "").strip()
        for record in (item.as_dict() for item in county_reader.iterRecords())
    }
    county_reader._source_archive.close()
    if len(county_names) != 75:
        raise RuntimeError(f"Expected 75 county names, found {len(county_names)}")

    expected_counts = {
        "county": 75,
        "congressional": 4,
        "state_house": 100,
        "state_senate": 35,
    }
    all_centroids: list[dict] = []
    summaries = []
    for archive_name, output_name, kind in LAYERS:
        count, bounds, centroids = convert_layer(archive_name, output_name, kind, county_names)
        if kind in expected_counts and count != expected_counts[kind]:
            raise RuntimeError(f"{kind}: expected {expected_counts[kind]} features, found {count}")
        all_centroids.extend(centroids)
        summaries.append((kind, output_name, count, bounds))

    centroid_path = DATA_DIR / "ar_vtd20_centroids.geojson"
    centroid_payload = {"type": "FeatureCollection", "features": all_centroids}
    centroid_path.write_text(
        json.dumps(centroid_payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    for kind, output_name, count, bounds in summaries:
        print(f"{kind}: {count} features -> {output_name}; bbox={bounds}")
    print(f"vtd_centroids: {len(all_centroids)} features -> {centroid_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
