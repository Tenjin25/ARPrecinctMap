from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCE_PATH = DATA_DIR / "ar_elections_aggregated.json"
OUTPUT_DIR = DATA_DIR / "contests"


def ui_contest_type(value: str) -> str:
    return "president" if value == "presidential" else value


def main() -> int:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    coverage_by_year = payload.get("coverage_by_year", {})
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []

    for year_text, contests in sorted(payload.get("results_by_year", {}).items()):
        year = int(year_text)
        for source_type, contest_node in sorted(contests.items()):
            contest_type = ui_contest_type(source_type)
            results = contest_node.get("general", {}).get("results", {})
            rows = []
            for precinct_key, result in sorted(results.items()):
                rows.append(
                    {
                        "county": precinct_key,
                        "dem_votes": int(result.get("dem_votes") or 0),
                        "rep_votes": int(result.get("rep_votes") or 0),
                        "other_votes": int(result.get("other_votes") or 0),
                        "total_votes": int(result.get("total_votes") or 0),
                        "dem_candidate": result.get("dem_candidate") or "",
                        "rep_candidate": result.get("rep_candidate") or "",
                        "margin": int(result.get("margin") or 0),
                        "margin_pct": float(result.get("margin_pct") or 0),
                        "winner": result.get("winner") or "",
                        "color": (result.get("competitiveness") or {}).get("color") or "",
                        **(
                            {
                                "source_scope": result.get("source_scope") or "",
                                "source_label": result.get("source_label") or "",
                                "source_url": result.get("source_url") or "",
                            }
                            if result.get("source_scope")
                            else {}
                        ),
                    }
                )

            file_name = f"{contest_type}_{year}.json"
            slice_payload = {
                "year": year,
                "contest_type": contest_type,
                "rows": rows,
            }
            (OUTPUT_DIR / file_name).write_text(
                json.dumps(slice_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
            manifest.append(
                {
                    "year": year,
                    "contest_type": contest_type,
                    "file": file_name,
                    "rows": len(rows),
                    "partial": not bool(coverage_by_year.get(str(year), {}).get("complete", True)),
                    "counties_present": coverage_by_year.get(str(year), {}).get("counties_present", 75),
                    "counties_expected": coverage_by_year.get(str(year), {}).get("counties_expected", 75),
                    "missing_counties": coverage_by_year.get(str(year), {}).get("missing_counties", []),
                    **(
                        {
                            "county_coverage_complete": coverage_by_year.get(str(year), {}).get("county_coverage_complete", True),
                            "precinct_coverage_complete": coverage_by_year.get(str(year), {}).get("precinct_coverage_complete", True),
                            "official_county_fallbacks": coverage_by_year.get(str(year), {}).get("official_county_fallbacks", []),
                            "official_county_controls": coverage_by_year.get(str(year), {}).get("official_county_controls", []),
                        }
                        if (
                            coverage_by_year.get(str(year), {}).get("official_county_fallbacks")
                            or coverage_by_year.get(str(year), {}).get("official_county_controls")
                        )
                        else {}
                    ),
                }
            )

    (OUTPUT_DIR / "manifest.json").write_text(
        json.dumps({"state": "Arkansas", "files": manifest}, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {len(manifest)} contest slices to {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
