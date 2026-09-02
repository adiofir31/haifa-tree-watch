#!/usr/bin/env python
"""Build the flat block/parcel -> neighbourhood index the bot uses at runtime.

Companion to ``build_street_index.py``, same idea: all the spatial work happens
once, offline, and the bot reads a plain CSV. It never loads a geometry library.

Why this exists
---------------
The largest felling licences on Yeela have no street at all. They are
infrastructure and institutional projects — transport authorities, the railway,
the university, the port, utilities — identified only by block (גוש) and parcel
(חלקה). Without this index those licences land under "unknown neighbourhood",
which is exactly the wrong place for the biggest ones.

Two tiers, mirroring the street index's house-number ranges:

  1. block + parcel, from the parcel layer joined to the neighbourhood polygons
  2. block alone — the neighbourhood covering the largest share of the block's
     area, used when the licence gives no parcel or an unfamiliar one

Usage
-----
    python tools/build_block_index.py \
        --parcels       data/raw/parcels.shp \
        --neighborhoods data/raw/schunot.json \
        --out           data/block_index.csv \
        --neighborhood-aliases data/neighborhood_aliases.csv

The parcel layer needs a block field and (optionally) a parcel field. Field
names are auto-detected; the script prints what it picked. If your layer holds
blocks only, pass --no-parcels and you get tier 2 alone.

Requires geopandas. Tool-only; the bot does not.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent / "src", _HERE, _HERE.parent):
    if (_candidate / "tree_watch" / "normalize.py").exists() or (_candidate / "normalize.py").exists():
        sys.path.insert(0, str(_candidate))

try:
    from tree_watch.normalize import clean_text  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - same fallback as the street tool
    try:
        from normalize import clean_text  # noqa: E402
    except ModuleNotFoundError:
        sys.exit(
            "[!] could not import normalize.py.\n"
            "    Expected at src/tree_watch/normalize.py, or beside this script."
        )

BLOCK_FIELD_GUESSES = [
    "GUSH", "GUSH_NUM", "BLOCK", "BLOCK_NUM", "GUSH_SUFFIX", "gush", "block",
    "GUSH_ID", "REG_MUNI_G", "גוש", "מספר_גוש",
]
PARCEL_FIELD_GUESSES = [
    "PARCEL", "PARCEL_NUM", "HELKA", "HELKA_NUM", "CHELKA", "parcel", "helka",
    "PARCEL_ID", "חלקה", "מספר_חלקה",
]
NEIGH_FIELD_GUESSES = [
    "SchName", "SCHNAME", "NEIGH", "NEIGHBORHOOD", "SHCHUNA", "SHEM_SHCHUNA",
    "NEIGH_NAME", "neighbourhood", "neighborhood", "NAME", "Name", "שם_שכונה",
    "שכונה",
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def pick_field(frame, guesses, override, label):
    if override:
        if override not in frame.columns:
            sys.exit(f"[!] field {override!r} not found in {label}. "
                     f"available: {list(frame.columns)}")
        return override
    lowered = {c.lower(): c for c in frame.columns}
    for guess in guesses:
        if guess in frame.columns:
            return guess
        if guess.lower() in lowered:
            return lowered[guess.lower()]
    sys.exit(f"[!] could not detect the {label} field. Pass it explicitly. "
             f"available: {list(frame.columns)}")


def digits(value: object) -> str:
    """Normalise a block or parcel identifier to bare digits.

    Cadastral exports write blocks as ``10841``, ``10841.0``, ``'10841 '`` or
    even ``'גוש 10841'``. Yeela sends bare numbers. Both ends must agree, so
    everything is reduced to the digit run.
    """
    text = clean_text(value)
    if not text:
        return ""
    kept = "".join(ch for ch in text if ch.isdigit())
    return kept.lstrip("0") or ("0" if kept else "")


def load_aliases(path):
    """alias -> canonical, empty canonical means 'drop this area'."""
    if not path:
        return {}, set()
    mapping, dropped = {}, set()
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for record in csv.reader(handle):
            if not record or not record[0].strip() or record[0].lstrip().startswith("#"):
                continue
            if record[0].strip() == "alias":
                continue
            alias = clean_text(record[0])
            canonical = clean_text(record[1]) if len(record) > 1 else ""
            if not alias:
                continue
            if canonical:
                mapping[alias] = canonical
            else:
                dropped.add(alias)
    print(f"  loaded {len(mapping)} neighbourhood aliases, {len(dropped)} dropped names")
    return mapping, dropped


def canonicalise(name, aliases):
    name = clean_text(name)
    seen = set()
    while name in aliases and name not in seen:
        seen.add(name)
        name = aliases[name]
    return name


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def build(args):
    import geopandas as gpd

    print("loading layers...")
    parcels = gpd.read_file(args.parcels, encoding=args.encoding)
    if args.parcels_crs:
        parcels = parcels.set_crs(args.parcels_crs, allow_override=True)
    if parcels.crs is None:
        sys.exit("[!] the parcel layer has no CRS — pass --parcels-crs EPSG:2039")

    neighs = gpd.read_file(args.neighborhoods, encoding=args.encoding)
    if args.neigh_crs:
        neighs = neighs.set_crs(args.neigh_crs, allow_override=True)

    block_field = pick_field(parcels, BLOCK_FIELD_GUESSES, args.block_field, "parcels / block")
    parcel_field = None
    if not args.no_parcels:
        parcel_field = pick_field(parcels, PARCEL_FIELD_GUESSES, args.parcel_field,
                                  "parcels / parcel")
    neigh_field = pick_field(neighs, NEIGH_FIELD_GUESSES, args.neigh_field,
                             "neighbourhoods / name")
    print(f"  parcels        : block={block_field!r} "
          f"parcel={parcel_field!r}  ({len(parcels)} features)")
    print(f"  neighbourhoods : name={neigh_field!r}  ({len(neighs)} features)")

    if neighs.crs != parcels.crs:
        print(f"  reprojecting neighbourhoods {neighs.crs} -> {parcels.crs}")
        neighs = neighs.to_crs(parcels.crs)

    aliases, dropped = load_aliases(args.neighborhood_aliases)

    neighs = neighs[[neigh_field, "geometry"]].rename(columns={neigh_field: "neighborhood"})
    neighs["neighborhood"] = neighs["neighborhood"].map(
        lambda v: canonicalise(v, aliases)
    )
    neighs = neighs[neighs["neighborhood"].astype(bool)]
    neighs = neighs[~neighs["neighborhood"].isin(dropped)]

    keep = [block_field, "geometry"] + ([parcel_field] if parcel_field else [])
    parcels = parcels[keep].copy()
    parcels["block_key"] = parcels[block_field].map(digits)
    parcels = parcels[parcels["block_key"].astype(bool)]
    if parcel_field:
        parcels["parcel_key"] = parcels[parcel_field].map(digits)
    else:
        parcels["parcel_key"] = ""

    # Areal overlay: a parcel can straddle a neighbourhood boundary, and a block
    # very often does. Whoever covers the most area wins, and the share is kept
    # so a marginal call is visible in the report rather than silent.
    print("intersecting parcels with neighbourhoods...")
    parcels = parcels[parcels.geometry.notna() & ~parcels.geometry.is_empty]
    pieces = gpd.overlay(parcels, neighs, how="intersection", keep_geom_type=True)
    pieces["piece_area"] = pieces.geometry.area
    print(f"  {len(pieces)} parcel x neighbourhood pieces")

    rows = []
    ambiguous_parcels = []
    ambiguous_blocks = []

    # ---- tier 1: block + parcel -----------------------------------------
    if parcel_field:
        per_parcel: dict[tuple[str, str], Counter] = defaultdict(Counter)
        for block, parcel, neigh, area in zip(
            pieces["block_key"], pieces["parcel_key"],
            pieces["neighborhood"], pieces["piece_area"]
        ):
            if block and parcel:
                per_parcel[(block, parcel)][neigh] += float(area)

        for (block, parcel), counter in per_parcel.items():
            total = sum(counter.values())
            if total <= 0:
                continue
            neigh, area = counter.most_common(1)[0]
            share = area / total
            rows.append({
                "block": block,
                "parcel": parcel,
                "neighborhood": neigh,
                "priority": 1,
                "method": "parcel_area",
                "support": round(share, 3),
            })
            if share < args.ambiguous_share:
                ambiguous_parcels.append((block, parcel, neigh, share))
        print(f"  {len(per_parcel)} block+parcel rows")

    # ---- tier 2: block majority -----------------------------------------
    per_block: dict[str, Counter] = defaultdict(Counter)
    for block, neigh, area in zip(
        pieces["block_key"], pieces["neighborhood"], pieces["piece_area"]
    ):
        if block:
            per_block[block][neigh] += float(area)

    multi_block = 0
    for block, counter in per_block.items():
        total = sum(counter.values())
        if total <= 0:
            continue
        neigh, area = counter.most_common(1)[0]
        share = area / total
        if len(counter) > 1:
            multi_block += 1
        rows.append({
            "block": block,
            "parcel": "",
            "neighborhood": neigh,
            "priority": 2,
            "method": "block_area_majority",
            "support": round(share, 3),
        })
        if share < args.ambiguous_share:
            ambiguous_blocks.append((block, neigh, share, dict(counter)))
    print(f"  {len(per_block)} blocks ({multi_block} span more than one neighbourhood)")

    rows.sort(key=lambda r: (r["block"], r["priority"], r["parcel"]))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "block", "parcel", "neighborhood", "priority", "method", "support",
        ])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nwrote {len(rows)} rows -> {out_path}")

    write_report(args, rows, per_block, ambiguous_parcels, ambiguous_blocks, multi_block)


def write_report(args, rows, per_block, ambiguous_parcels, ambiguous_blocks, multi_block):
    path = Path(args.report or Path(args.out).with_suffix(".report.txt"))
    lines = [
        "block index QA report",
        "=" * 60,
        f"rows                        : {len(rows)}",
        f"distinct blocks             : {len(per_block)}",
        f"blocks spanning >1 area     : {multi_block}",
        f"distinct neighbourhoods     : {len({r['neighborhood'] for r in rows})}",
        f"ambiguous blocks            : {len(ambiguous_blocks)}",
        f"ambiguous parcels           : {len(ambiguous_parcels)}",
        "",
        "-- blocks whose dominant neighbourhood covers less than the threshold " + "-" * 3,
        "   (a licence given only a block number here will be placed roughly)",
    ]
    for block, neigh, share, counter in sorted(ambiguous_blocks, key=lambda x: x[2])[:200]:
        parts = ", ".join(
            f"{name} {value / sum(counter.values()):.0%}"
            for name, value in sorted(counter.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"  גוש {block}: kept {neigh} ({share:.0%}) — {parts}")

    lines += ["", "-- parcels split across a boundary " + "-" * 26]
    for block, parcel, neigh, share in sorted(ambiguous_parcels, key=lambda x: x[3])[:200]:
        lines.append(f"  גוש {block} חלקה {parcel}: kept {neigh} ({share:.0%})")

    Path(path).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote QA report          -> {path}")
    print("\nCheck the ambiguous-blocks section: those are the licences that will "
          "be placed only roughly when no parcel is given.")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--parcels", required=True,
                        help="parcel (חלקות) or block (גושים) polygon layer")
    parser.add_argument("--neighborhoods", required=True)
    parser.add_argument("--out", default="data/block_index.csv")
    parser.add_argument("--report")
    parser.add_argument("--neighborhood-aliases",
                        help="same alias file the street index uses")
    parser.add_argument("--encoding", default="utf-8",
                        help="attribute encoding; try cp1255 if Hebrew is mojibake")
    parser.add_argument("--block-field")
    parser.add_argument("--parcel-field")
    parser.add_argument("--neigh-field")
    parser.add_argument("--parcels-crs", default=None,
                        help="force a CRS on the parcel layer if it declares none")
    parser.add_argument("--neigh-crs", default=None)
    parser.add_argument("--no-parcels", action="store_true",
                        help="the layer holds blocks only; emit tier 2 alone")
    parser.add_argument("--ambiguous-share", type=float, default=0.8,
                        help="flag blocks whose dominant neighbourhood covers less "
                             "than this share of their area")
    build(parser.parse_args())


if __name__ == "__main__":
    main()
