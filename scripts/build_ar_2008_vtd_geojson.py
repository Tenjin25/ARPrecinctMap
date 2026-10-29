from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".vendor" / "pyshp"))

import shapefile  # noqa: E402


INPUT_DIR = ROOT / "data" / "2008_vtds"
OUTPUT_PATH = ROOT / "data" / "ar_vtds_2008.geojson"
SOURCE_URL = "https://www2.census.gov/geo/tiger/TIGER2008/05_ARKANSAS/"


def member_bytes(archive: zipfile.ZipFile, suffix: str) -> io.BytesIO:
    matches = [name for name in archive.namelist() if name.lower().endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {suffix} member, found {len(matches)} in {archive.filename}")
    return io.BytesIO(archive.read(matches[0]))


def json_default(value):
    if isinstance(value, bytes):
        return value.decode("latin1")
    return str(value)


def main() -> int:
    archives = sorted(INPUT_DIR.glob("tl_2008_05???_vtd00.zip"))
    if len(archives) != 75:
        raise RuntimeError(f"Expected 75 VTD archives, found {len(archives)} in {INPUT_DIR}")

    temporary = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".part")
    feature_count = 0
    county_fips_seen: set[str] = set()
    geoid_seen: set[str] = set()
    duplicate_geoids: set[str] = set()
    bounds = [float("inf"), float("inf"), float("-inf"), float("-inf")]

    with temporary.open("w", encoding="utf-8", newline="") as output:
        output.write(
            '{"type":"FeatureCollection","name":"Arkansas 2008 Voting Districts",'
            f'"source":{json.dumps(SOURCE_URL)},"source_crs":"EPSG:4269","features":['
        )
        first_feature = True

        for number, archive_path in enumerate(archives, start=1):
            county_fips_from_name = archive_path.stem.split("_")[2]
            with zipfile.ZipFile(archive_path) as archive:
                reader = shapefile.Reader(
                    shp=member_bytes(archive, ".shp"),
                    shx=member_bytes(archive, ".shx"),
                    dbf=member_bytes(archive, ".dbf"),
                    encoding="latin1",
                )
                county_features = 0
                for shape_record in reader.iterShapeRecords():
                    properties = dict(shape_record.record.as_dict())
                    state_fips = str(properties.get("STATEFP00", "05")).zfill(2)
                    county_fips = str(
                        properties.get("COUNTYFP00", county_fips_from_name[-3:])
                    ).zfill(3)
                    vtd_code = str(properties.get("VTDST00", "")).strip()
                    geoid = f"{state_fips}{county_fips}{vtd_code}"

                    properties.setdefault("STATEFP00", state_fips)
                    properties.setdefault("COUNTYFP00", county_fips)
                    properties["GEOID00"] = geoid
                    properties["SOURCE_ZIP"] = archive_path.name

                    county_fips_seen.add(county_fips)
                    if geoid in geoid_seen:
                        duplicate_geoids.add(geoid)
                    geoid_seen.add(geoid)

                    shape = shape_record.shape
                    if shape.shapeType == shapefile.NULL:
                        raise RuntimeError(f"Null geometry for {geoid} in {archive_path.name}")
                    shape_bounds = shape.bbox
                    bounds[0] = min(bounds[0], shape_bounds[0])
                    bounds[1] = min(bounds[1], shape_bounds[1])
                    bounds[2] = max(bounds[2], shape_bounds[2])
                    bounds[3] = max(bounds[3], shape_bounds[3])

                    feature = {
                        "type": "Feature",
                        "properties": properties,
                        "geometry": shape.__geo_interface__,
                    }
                    if not first_feature:
                        output.write(",")
                    output.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":"), default=json_default))
                    first_feature = False
                    feature_count += 1
                    county_features += 1

            print(f"[{number:02d}/75] {archive_path.name}: {county_features} features")

        output.write(f'],"bbox":{json.dumps(bounds, separators=(",", ":"))}}}')

    if len(county_fips_seen) != 75:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Expected 75 counties in features, found {len(county_fips_seen)}")
    if duplicate_geoids:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Duplicate GEOID00 values: {sorted(duplicate_geoids)[:10]}")
    if not (-95.0 < bounds[0] < -89.0 and 32.0 < bounds[1] < 37.0):
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"Unexpected Arkansas bounds: {bounds}")

    temporary.replace(OUTPUT_PATH)
    print(
        f"Wrote {feature_count} features from {len(county_fips_seen)} counties "
        f"to {OUTPUT_PATH} with bbox {bounds}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
