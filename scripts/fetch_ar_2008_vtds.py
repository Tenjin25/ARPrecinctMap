from __future__ import annotations

import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


BASE_URL = "https://www2.census.gov/geo/tiger/TIGER2008/05_ARKANSAS/"
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "2008_vtds"
COUNTY_RE = re.compile(r'href="(05\d{3}_[^"/]+_County/)"', re.IGNORECASE)
VTD_RE = re.compile(r'href="(tl_2008_05\d{3}_vtd00\.zip)"', re.IGNORECASE)


def read_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ARPrecinctMap data fetcher"})
    for attempt in range(7):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 6:
                raise
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else min(60, 5 * (2**attempt))
            print(f"Rate limited; retrying in {delay:.0f}s: {url}")
            time.sleep(delay)
    raise RuntimeError(f"Unable to download {url}")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    index_html = read_url(BASE_URL).decode("utf-8", errors="replace")
    county_dirs = sorted(set(COUNTY_RE.findall(index_html)))
    if len(county_dirs) != 75:
        raise RuntimeError(f"Expected 75 Arkansas county directories, found {len(county_dirs)}")

    downloaded = 0
    reused = 0
    archives: list[Path] = []

    for number, county_dir in enumerate(county_dirs, start=1):
        county_url = urllib.parse.urljoin(BASE_URL, county_dir)
        county_fips = county_dir.split("_", 1)[0]
        filename = f"tl_2008_{county_fips}_vtd00.zip"
        destination = OUTPUT_DIR / filename
        archives.append(destination)

        if destination.exists() and zipfile.is_zipfile(destination):
            reused += 1
            print(f"[{number:02d}/75] exists     {filename}")
            continue

        archive_url = urllib.parse.urljoin(county_url, filename)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(read_url(archive_url))
        if not zipfile.is_zipfile(temporary):
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Downloaded file is not a valid ZIP: {archive_url}")
        temporary.replace(destination)
        downloaded += 1
        print(f"[{number:02d}/75] downloaded {filename}")
        time.sleep(0.25)

    invalid = [path.name for path in archives if not zipfile.is_zipfile(path)]
    if invalid:
        raise RuntimeError(f"Invalid ZIP archives: {', '.join(invalid)}")

    print(
        f"Verified {len(archives)} Arkansas 2008 VTD archives "
        f"({downloaded} downloaded, {reused} already present) in {OUTPUT_DIR}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
