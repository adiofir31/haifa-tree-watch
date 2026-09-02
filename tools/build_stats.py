#!/usr/bin/env python
"""Build the dataset behind the public dashboard.

Runs offline, like the index builders. It pulls the **whole** history from both
sources — not just the licences whose appeal window is still open — resolves
each address to a neighbourhood with the same ``geo.py`` the bot uses, and
writes one flat record per licence.

    python tools/build_stats.py                      # live pull -> docs/data/stats.json
    python tools/build_stats.py --self-contained     # one HTML file, data inlined
    python tools/build_stats.py --from-fixtures      # no network, uses tests/fixtures

Why not read ``data/state.jsonl``
---------------------------------
State only holds what the bot has reported since the cutover, and the 267
records imported from ``sent_licenses.txt`` carry no content at all. Going back
to the sources gives the municipal table from 2009 and Yeela from late 2024.

A note on dates
---------------
The two sources date things differently and there is no honest way to merge
them: the municipal table records when a request was *filed*, Yeela records
when a licence was *approved*. Both are emitted, along with ``date_kind``, and
the dashboard says which it is grouping by.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _candidate in (_ROOT / "src", _HERE, _ROOT):
    if (_candidate / "tree_watch").is_dir() or (_candidate / "normalize.py").exists():
        sys.path.insert(0, str(_candidate))

try:
    from tree_watch import config
    from tree_watch.geo import GeoIndex
    from tree_watch.sources import haifa_pdf
    from tree_watch.sources import yeela as yeela_source
except ModuleNotFoundError as exc:  # pragma: no cover
    sys.exit(f"[!] could not import the tree_watch package ({exc}).\n"
             f"    Run this from the project root, or set PYTHONPATH to src/.")

log = logging.getLogger("build_stats")


# --------------------------------------------------------------------------
# collection — deliberately without the "still open" date filter
# --------------------------------------------------------------------------

def load_pdf_pages_live() -> list:
    import pdfplumber
    import requests

    url = getattr(config, "PDF_URL")
    log.info("downloading the municipal PDF")
    response = requests.get(url, timeout=(10, 120))
    response.raise_for_status()
    pages = []
    with pdfplumber.open(io.BytesIO(response.content)) as pdf:
        log.info("municipal PDF: %d pages", len(pdf.pages))
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                pages.append([[("" if c is None else str(c)) for c in row] for row in table])
    return pages


def load_pdf_pages_fixture() -> list:
    path = _ROOT / "tests" / "fixtures" / "diag_pdf_raw.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [page["table"] for page in raw if page.get("table")]


def load_yeela_rows_live() -> list:
    log.info("pulling every Yeela page")
    return yeela_source.fetch_all()


def load_yeela_rows_fixture() -> list:
    path = _ROOT / "tests" / "fixtures" / "yeela_full.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# geo, tolerant of the block/parcel arguments landing in a later version
# --------------------------------------------------------------------------

def make_resolver(geo: GeoIndex):
    import inspect

    accepted = set(inspect.signature(geo.lookup).parameters)

    def resolve(record):
        kwargs = {}
        if "full_text" in accepted:
            kwargs["full_text"] = getattr(record, "address", "") or ""
        for name in ("block", "parcel"):
            if name in accepted:
                kwargs[name] = getattr(record, name, None)
        try:
            return geo.lookup(getattr(record, "street", "") or "",
                              getattr(record, "house", None), **kwargs)
        except TypeError:
            return geo.lookup(getattr(record, "street", "") or "")

    return resolve


# --------------------------------------------------------------------------
# serialisation
# --------------------------------------------------------------------------

def iso(value) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def to_record(licence, match, date_kind: str) -> dict:
    species = dict(getattr(licence, "species", {}) or {})
    street = (getattr(licence, "street", "") or "").strip()
    house = str(getattr(licence, "house", "") or "").strip()
    return {
        "id": getattr(licence, "key", None) or getattr(licence, "source_id", ""),
        "source": getattr(getattr(licence, "source", None), "value", "unknown"),
        "date": iso(getattr(licence, "published", None)),
        "date_kind": date_kind,
        "appeal_last": iso(getattr(licence, "appeal_last", None)),
        "neighborhood": getattr(match, "neighborhood", "") or "",
        "geo_method": getattr(match, "method", ""),
        "street": street,
        "house": house,
        "block": str(getattr(licence, "block", "") or ""),
        "parcel": str(getattr(licence, "parcel", "") or ""),
        "fell": int(getattr(licence, "fell_count", 0) or 0),
        "relocate": int(getattr(licence, "relocate_count", 0) or 0),
        "preserve": int(getattr(licence, "preserve_count", 0) or 0),
        "species": species,
        "applicant": (getattr(licence, "applicant", "") or "").strip(),
    }


def build_dataset(args) -> dict:
    geo = GeoIndex()
    resolve = make_resolver(geo)
    records: list[dict] = []
    problems: list[str] = []

    if not args.skip_pdf:
        try:
            pages = load_pdf_pages_fixture() if args.from_fixtures else load_pdf_pages_live()
            parsed = haifa_pdf.parse_tables(pages)
            log.info("municipal: %d requests", len(parsed))
            for licence in parsed:
                records.append(to_record(licence, resolve(licence), "requested"))
        except Exception as exc:  # noqa: BLE001
            log.exception("municipal source failed")
            problems.append(f"municipal: {exc}")

    if not args.skip_yeela:
        try:
            rows = load_yeela_rows_fixture() if args.from_fixtures else load_yeela_rows_live()
            parsed = yeela_source.parse_rows(rows)
            log.info("yeela: %d requests", len(parsed))
            for licence in parsed:
                records.append(to_record(licence, resolve(licence), "approved"))
        except Exception as exc:  # noqa: BLE001
            log.exception("Yeela source failed")
            problems.append(f"yeela: {exc}")

    if problems and not records:
        sys.exit("[!] both sources failed:\n  " + "\n  ".join(problems))

    dated = [r for r in records if r["date"]]
    undated = len(records) - len(dated)
    if undated:
        log.info("%d records have no date and are kept but excluded from the timeline",
                 undated)

    years = sorted({r["date"][:4] for r in dated})
    by_source = Counter(r["source"] for r in records)
    log.info("total %d records, %s, years %s-%s",
             len(records), dict(by_source), years[0] if years else "?",
             years[-1] if years else "?")

    return {
        "generated": date.today().isoformat(),
        "counts": {
            "records": len(records),
            "undated": undated,
            "by_source": dict(by_source),
            "fell_total": sum(r["fell"] for r in records),
        },
        "problems": problems,
        "records": records,
    }


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

DATA_MARKER = "/*__STATS_DATA__*/null"


def write_outputs(dataset: dict, args) -> None:
    payload = json.dumps(dataset, ensure_ascii=False, separators=(",", ":"))

    json_path = Path(args.out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(payload, encoding="utf-8")
    size_mb = json_path.stat().st_size / 1_048_576
    print(f"wrote {len(dataset['records'])} records -> {json_path} ({size_mb:.1f} MB)")

    if args.self_contained:
        template = Path(args.template)
        if not template.exists():
            sys.exit(f"[!] template not found: {template}")
        html = template.read_text(encoding="utf-8")
        if DATA_MARKER not in html:
            sys.exit(f"[!] {template} has no {DATA_MARKER} marker to inline into")
        # </script> inside the JSON would close the tag early.
        safe = payload.replace("</", "<\\/")
        out = Path(args.self_contained)
        out.write_text(html.replace(DATA_MARKER, safe), encoding="utf-8")
        print(f"wrote a single-file version   -> {out} "
              f"({out.stat().st_size / 1_048_576:.1f} MB) — open it in a browser")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="docs/data/stats.json")
    parser.add_argument("--template", default="docs/index.html")
    parser.add_argument("--self-contained", nargs="?", const="tree_stats.html",
                        help="also write one HTML file with the data inlined, "
                             "for looking at it before publishing anything")
    parser.add_argument("--from-fixtures", action="store_true",
                        help="use tests/fixtures instead of the network")
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--skip-yeela", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(levelname)-7s %(name)s  %(message)s")
    write_outputs(build_dataset(args), args)


if __name__ == "__main__":
    main()
