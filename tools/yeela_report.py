#!/usr/bin/env python
"""Pull the full Yeela licence list for one city and report yearly totals.

The bot reads only the first page; this walks all pages so the numbers are
complete. It writes the raw JSON alongside the report so the pull can be
re-analysed later without hitting the server again.

    python tools/yeela_report.py --year 2026
    python tools/yeela_report.py --year 2026 --out yeela_full.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict

import requests

URL = "https://yeela-trees.moag.gov.il/api/Fo/FOServiceRequest/getFOGridPublicityLicenses"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "userOrgRoleId": "",
    "Origin": "https://yeela-trees.moag.gov.il",
    "Referer": "https://yeela-trees.moag.gov.il/FoPublic/FoLicence",
}


def fetch_page(city_id: int, page: int, page_size: int) -> dict:
    payload = {
        "orderDetails": None,
        "pageDetails": {"pageNumber": page, "pageSize": page_size},
        "parameters": {"cityId": city_id, "appealLastDate": None},
    }
    resp = requests.post(URL, json=payload, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_all(city_id: int, page_size: int, max_pages: int) -> list[dict]:
    first = fetch_page(city_id, 1, page_size)
    pagination = first.get("pagination") or {}
    total_pages = min(int(pagination.get("totalPages", 1)), max_pages)
    print(f"total rows: {pagination.get('totalCount')}  pages: {total_pages}")

    rows = list(first.get("result") or [])
    for page in range(2, total_pages + 1):
        print(f"  page {page}/{total_pages}", end="\r", flush=True)
        rows.extend(fetch_page(city_id, page, page_size).get("result") or [])
        time.sleep(0.3)  # be polite to the ministry's server
    print(f"\nfetched {len(rows)} rows")
    return rows


def report(rows: list[dict], year: int) -> None:
    issued = [r for r in rows if r.get("licenseId")]
    pending = [r for r in rows if not r.get("licenseId")]

    in_year = [r for r in issued if (r.get("approvedDate") or "").startswith(str(year))]
    if not in_year:
        sys.exit(f"no licences approved in {year}")

    fell = sum(r.get("unproot") or 0 for r in in_year)
    move = sum(r.get("copying") or 0 for r in in_year)
    keep = sum(r.get("conservation") or 0 for r in in_year)

    print("\n" + "=" * 58)
    print(f"Yeela — approved licences, {year}")
    print("=" * 58)
    print(f"licences            : {len({r['licenseId'] for r in in_year})}")
    print(f"rows (licence x species): {len(in_year)}")
    print(f"trees to fell       : {fell}")
    print(f"trees to relocate   : {move}")
    print(f"trees to preserve   : {keep}")

    monthly: Counter = Counter()
    for row in in_year:
        monthly[(row["approvedDate"] or "")[5:7]] += row.get("unproot") or 0
    print("\nfelling by month:")
    for month in sorted(monthly):
        print(f"  {month}: {monthly[month]:>5}")

    species: Counter = Counter()
    for row in in_year:
        if (row.get("unproot") or 0) > 0:
            species[row.get("treeName") or "unknown"] += row["unproot"]
    print("\ntop species felled:")
    for name, count in species.most_common(15):
        print(f"  {count:>5}  {name}")

    by_status: Counter = Counter(r.get("licenseStatusDesc") or "" for r in in_year)
    print("\nby status:")
    for status, count in by_status.most_common():
        print(f"  {count:>5}  {status}")

    all_years: dict = defaultdict(int)
    for row in issued:
        stamp = row.get("approvedDate") or ""
        if stamp:
            all_years[stamp[:4]] += row.get("unproot") or 0
    print("\nfelling by year (whole dataset):")
    for stamp in sorted(all_years):
        print(f"  {stamp}: {all_years[stamp]:>6}")

    print(f"\npending requests with no licence yet: {len(pending)} rows, "
          f"{len({r['requestId'] for r in pending})} requests "
          f"({sum(r.get('unproot') or 0 for r in pending)} trees flagged for felling)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city-id", type=int, default=4000, help="4000 = Haifa")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--out", default="yeela_full.json")
    args = parser.parse_args()

    rows = fetch_all(args.city_id, args.page_size, args.max_pages)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    print(f"raw data -> {args.out}")
    report(rows, args.year)


if __name__ == "__main__":
    main()
