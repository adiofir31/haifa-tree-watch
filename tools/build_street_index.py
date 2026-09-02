#!/usr/bin/env python
"""Build the flat street->neighbourhood index used by the bot at runtime.

This script does all the spatial work ONCE, offline. The bot itself never loads
a geometry library: it reads the CSV this produces into a dict.

Three tiers, in priority order:

  1. House-number ranges, derived from the address-point layer joined to the
     neighbourhood polygons. This is what solves long streets that cross several
     neighbourhoods (Moriah, Horev, Abba Hushi...).
  2. Street-level majority, also from the address points, used when a licence
     gives no house number.
  3. Street-line fallback, for streets that have no address points at all
     (new construction — Givat Zamer, Neot Peres). The street centreline is
     intersected with the neighbourhood polygons and the longest share wins.

Usage
-----
    python tools/build_street_index.py \
        --points        data/raw/addresses.csv \
        --neighborhoods data/raw/neighborhoods.geojson \
        --streets       data/raw/osm_streets.geojson \
        --out           data/street_index.csv

Any layer may be a shapefile, GeoJSON, GeoPackage, or — for the address points —
a plain CSV with X/Y columns. CSV coordinates are assumed to be Israeli TM Grid
(EPSG:2039); change with --points-crs.

Field names are auto-detected; override with --points-street-field etc. if the
guess is wrong (the script prints what it picked).

Requires: geopandas, pandas, shapely. Not needed by the bot at runtime.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

# normalize.py is shared with the bot at runtime so both sides normalise street
# names identically. Look for it in the package layout first, then next to this
# script, so the tool works whether or not the full project tree is set up.
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parent / "src", _HERE, _HERE.parent):
    if (_candidate / "tree_watch" / "normalize.py").exists() or (_candidate / "normalize.py").exists():
        sys.path.insert(0, str(_candidate))

try:
    from tree_watch.normalize import (  # noqa: E402
        clean_text,
        normalize_street,
        parse_house_number,
        street_keys,
    )
except ModuleNotFoundError:
    try:
        import normalize as _normalize  # noqa: E402
        clean_text = _normalize.clean_text
        normalize_street = _normalize.normalize_street
        parse_house_number = _normalize.parse_house_number
        street_keys = _normalize.street_keys
    except (ModuleNotFoundError, AttributeError):
        sys.exit(
            "[!] could not import normalize.py.\n"
            "    Put it at  src/tree_watch/normalize.py  (preferred), or next to this\n"
            "    script in tools/. Note there is an unrelated PyPI package called\n"
            "    'normalize'; if it is installed in your venv it can be picked up\n"
            "    instead, which is why the src/tree_watch/ layout is safer."
        )

HOUSE_MAX = 99999

# Values that appear in the street column but are not street names.
PLACEHOLDER_STREETS = {"?", "??", "---", "לא ידוע", "לא צוין", "0"}

STREET_FIELD_GUESSES = [
    "STREET_NAM", "STREET_NAME", "SHEM_RECHOV", "STREETNAME", "ST_NAME",
    "STREET", "REHOV", "shem_rechov", "street_nam", "street", "שם_רחוב", "רחוב",
    # OpenStreetMap
    "name:he", "name_he", "name", "NAME",
]
X_FIELD_GUESSES = ["X", "x", "POINT_X", "ITM_X", "EASTING", "EAST", "MIZRACH", "מזרח", "קואורדינטת_x"]
Y_FIELD_GUESSES = ["Y", "y", "POINT_Y", "ITM_Y", "NORTHING", "NORTH", "TZAFON", "צפון", "קואורדינטת_y"]

# Rough Israeli TM Grid envelope, used to catch swapped X/Y columns.
ITM_X_RANGE = (100_000, 320_000)
ITM_Y_RANGE = (350_000, 830_000)
HOUSE_FIELD_GUESSES = [
    "HOUSE_NUM", "HOUSENUM", "MISPAR_BAIT", "BUILDING_N", "HOUSE", "BLD_NUM",
    "ADDR_HOUSE", "house_num", "mispar_bait", "מספר_בית", "מספר",
    "number", "num", "house_number", "bait", "BAIT",
]
NEIGH_FIELD_GUESSES = [
    "NEIGH", "NEIGHBORHOOD", "SHCHUNA", "SHEM_SHCHUNA", "SHEM_SHECHUNA",
    "NEIGH_NAME", "SHM_SHCUNA", "SchName", "SCHNAME", "neighbourhood",
    "neighborhood", "neigh", "shchuna", "NAME", "Name", "שם_שכונה",
    "שכונה", "שם",
]


# --------------------------------------------------------------------------
# field detection
# --------------------------------------------------------------------------

def pick_field(gdf, guesses, override, label):
    if override:
        if override not in gdf.columns:
            sys.exit(f"[!] field {override!r} not found in {label}. "
                     f"available: {list(gdf.columns)}")
        return override
    lowered = {c.lower(): c for c in gdf.columns}
    for guess in guesses:
        if guess in gdf.columns:
            return guess
        if guess.lower() in lowered:
            return lowered[guess.lower()]
    sys.exit(f"[!] could not detect the {label} field. "
             f"pass it explicitly. available: {list(gdf.columns)}")


# --------------------------------------------------------------------------
# range building (pure — unit-testable without any geodata)
# --------------------------------------------------------------------------

def build_ranges(house_to_neigh: dict[int, str], min_run: int = 2, min_support: int = 1):
    """Compress {house_number: neighbourhood} into contiguous ranges.

    Runs shorter than ``min_run`` that are surrounded by the same neighbourhood
    on both sides are absorbed into it — a single mis-tagged address point
    should not carve a hole out of a street.

    Boundaries are widened so that unseen house numbers still land somewhere:
    the first range starts at 0, the last ends at HOUSE_MAX, and each range
    starts right after the previous one ends.
    """
    if not house_to_neigh:
        return []

    ordered = sorted(house_to_neigh.items())

    runs: list[list] = []  # [neighbourhood, first_house, last_house, support]
    for house, neigh in ordered:
        if runs and runs[-1][0] == neigh:
            runs[-1][2] = house
            runs[-1][3] += 1
        else:
            runs.append([neigh, house, house, 1])

    # absorb noise runs
    changed = True
    while changed and len(runs) > 2:
        changed = False
        for i in range(1, len(runs) - 1):
            if runs[i][3] < min_run and runs[i - 1][0] == runs[i + 1][0]:
                runs[i - 1][2] = runs[i + 1][2]
                runs[i - 1][3] += runs[i][3] + runs[i + 1][3]
                del runs[i:i + 2]
                changed = True
                break

    # Absorb runs backed by too few address points into whichever neighbour has
    # more support. min_run only helps when the SAME area sits on both sides; a
    # single mis-tagged point between two different areas survives it and slices
    # the street in three.
    changed = True
    while changed and len(runs) > 1:
        changed = False
        weakest = min(range(len(runs)), key=lambda i: runs[i][3])
        if runs[weakest][3] >= min_support:
            break
        left = runs[weakest - 1] if weakest > 0 else None
        right = runs[weakest + 1] if weakest + 1 < len(runs) else None
        target = max((r for r in (left, right) if r), key=lambda r: r[3])
        target[1] = min(target[1], runs[weakest][1])
        target[2] = max(target[2], runs[weakest][2])
        target[3] += runs[weakest][3]
        del runs[weakest]
        runs.sort(key=lambda r: r[1])
        changed = True

    # merge neighbours that became identical after absorption
    merged: list[list] = []
    for run in runs:
        if merged and merged[-1][0] == run[0]:
            merged[-1][2] = run[2]
            merged[-1][3] += run[3]
        else:
            merged.append(list(run))

    out = []
    for i, (neigh, first, last, support) in enumerate(merged):
        low = 0 if i == 0 else out[-1][1] + 1
        high = HOUSE_MAX if i == len(merged) - 1 else last
        out.append((low, high, neigh, support))
    return out


# --------------------------------------------------------------------------
# main build
# --------------------------------------------------------------------------

def load_neighborhood_aliases(path):
    """alias -> canonical name. An empty canonical means "drop this area"."""
    if not path:
        return {}
    mapping = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        # Proper CSV parsing: several Haifa neighbourhood names contain a comma
        # ("עיר תחתית מזרח, ואדי סאליב"), so those fields must be quoted.
        for record in csv.reader(fh):
            if not record or not record[0].strip() or record[0].lstrip().startswith("#"):
                continue
            if record[0].strip() == "alias":
                continue
            alias = clean_text(record[0])
            canonical = clean_text(record[1]) if len(record) > 1 else ""
            if alias:
                mapping[alias] = canonical
    print(f"  loaded {len(mapping)} neighbourhood aliases")
    return mapping


def load(path, encoding, crs_override=None):
    """Read any vector layer. Missing/overridden CRS is applied, not reprojected."""
    import geopandas as gpd

    gdf = gpd.read_file(path, encoding=encoding)
    if gdf.empty:
        sys.exit(f"[!] {path} is empty")
    if crs_override:
        gdf = gdf.set_crs(crs_override, allow_override=True)
    elif gdf.crs is None:
        sys.exit(f"[!] {path} has no CRS. pass it explicitly (e.g. --neigh-crs EPSG:2039)")
    return gdf


def read_csv_any(path):
    """CSV with a BOM, or exported from ArcGIS in cp1255."""
    import pandas as pd

    for enc in ("utf-8-sig", "cp1255", "utf-8"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    sys.exit(f"[!] could not decode {path} as utf-8 or cp1255")


def load_points(args):
    """Address layer: a CSV with X/Y columns, or any normal vector file."""
    import geopandas as gpd
    import pandas as pd

    path = Path(args.points)
    if path.suffix.lower() not in {".csv", ".txt", ".tsv"}:
        return load(path, args.encoding, args.points_crs)

    df = read_csv_any(path)
    x_field = pick_field(df, X_FIELD_GUESSES, args.points_x_field, "address CSV / X")
    y_field = pick_field(df, Y_FIELD_GUESSES, args.points_y_field, "address CSV / Y")

    df[x_field] = pd.to_numeric(df[x_field], errors="coerce")
    df[y_field] = pd.to_numeric(df[y_field], errors="coerce")
    dropped = int(df[[x_field, y_field]].isna().any(axis=1).sum())
    df = df.dropna(subset=[x_field, y_field])
    if dropped:
        print(f"  dropped {dropped} rows with unusable coordinates")

    # Exports commonly write a missing coordinate as 0, which lands on null
    # island thousands of km away and silently poisons every spatial join.
    zeros = (df[x_field] == 0) & (df[y_field] == 0)
    if zeros.any():
        print(f"  [!] {int(zeros.sum())} rows have X=0,Y=0 — treating as missing, not as coordinates")
        street_col = args.points_street_field
        if not street_col:
            lowered = {c.lower(): c for c in df.columns}
            street_col = next((g if g in df.columns else lowered.get(g.lower())
                               for g in STREET_FIELD_GUESSES
                               if g in df.columns or g.lower() in lowered), None)
        if street_col:
            lost = sorted(set(df.loc[zeros, street_col].dropna())
                          - set(df.loc[~zeros, street_col].dropna()))
            if lost:
                print(f"      {len(lost)} streets exist ONLY in those rows and cannot be placed:")
                print("      " + ", ".join(lost[:12]) + (" ..." if len(lost) > 12 else ""))
        df = df[~zeros]
    if df.empty:
        sys.exit("[!] no usable coordinates in the address CSV")

    crs = args.points_crs or "EPSG:2039"
    if crs.upper().endswith("2039"):
        mx, my = df[x_field].median(), df[y_field].median()
        in_range = ITM_X_RANGE[0] <= mx <= ITM_X_RANGE[1] and ITM_Y_RANGE[0] <= my <= ITM_Y_RANGE[1]
        swapped = ITM_X_RANGE[0] <= my <= ITM_X_RANGE[1] and ITM_Y_RANGE[0] <= mx <= ITM_Y_RANGE[1]
        if not in_range and swapped:
            print(f"  [!] {x_field!r}/{y_field!r} look swapped for ITM "
                  f"(median {mx:.0f}, {my:.0f}) — swapping them")
            x_field, y_field = y_field, x_field
        elif not in_range:
            print(f"  [!] median coordinate ({mx:.0f}, {my:.0f}) is outside the "
                  f"Israeli TM Grid envelope — check --points-crs")

    print(f"  coordinates    : x={x_field!r} y={y_field!r} crs={crs}")
    return gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df[x_field], df[y_field]), crs=crs
    )


def build(args):
    import geopandas as gpd

    print("loading layers...")
    points = load_points(args)
    neighs = load(args.neighborhoods, args.encoding, args.neigh_crs)

    p_street = pick_field(points, STREET_FIELD_GUESSES, args.points_street_field, "address points / street")
    p_house = pick_field(points, HOUSE_FIELD_GUESSES, args.points_house_field, "address points / house number")
    n_name = pick_field(neighs, NEIGH_FIELD_GUESSES, args.neigh_field, "neighbourhoods / name")
    print(f"  address points : street={p_street!r} house={p_house!r}  ({len(points)} features)")
    print(f"  neighbourhoods : name={n_name!r}  ({len(neighs)} features)")

    if neighs.crs != points.crs:
        print(f"  reprojecting neighbourhoods {neighs.crs} -> {points.crs}")
        neighs = neighs.to_crs(points.crs)

    neighs = neighs[[n_name, "geometry"]].rename(columns={n_name: "neighborhood"})
    neighs["neighborhood"] = neighs["neighborhood"].map(clean_text)
    neighs = neighs[neighs["neighborhood"].astype(bool)]

    # ---- tier 1 + 2: address points -------------------------------------
    print("joining address points to neighbourhoods...")
    pts = points[[p_street, p_house, "geometry"]].copy()
    joined = gpd.sjoin(pts, neighs, how="left", predicate="within")

    unmatched = joined["neighborhood"].isna()
    n_unmatched = int(unmatched.sum())
    if n_unmatched and args.snap_distance > 0:
        print(f"  {n_unmatched} points fell outside every polygon — "
              f"snapping to nearest within {args.snap_distance}m")
        rescued = gpd.sjoin_nearest(
            pts.loc[unmatched.values], neighs, how="left",
            max_distance=args.snap_distance,
        )
        joined.loc[unmatched.values, "neighborhood"] = rescued["neighborhood"].values
    still_unmatched = int(joined["neighborhood"].isna().sum())

    # {street_key: {house_number: Counter(neighbourhood)}}
    per_street: dict[str, dict[int, Counter]] = defaultdict(lambda: defaultdict(Counter))
    display: dict[str, str] = {}
    street_totals: dict[str, Counter] = defaultdict(Counter)

    for street_raw, house_raw, neigh in zip(
        joined[p_street], joined[p_house], joined["neighborhood"]
    ):
        if not neigh or (isinstance(neigh, float) and neigh != neigh):
            continue
        key = normalize_street(street_raw)
        if not key or key in PLACEHOLDER_STREETS:
            continue
        display.setdefault(key, clean_text(street_raw))
        street_totals[key][neigh] += 1
        house = parse_house_number(house_raw)
        if house is not None:
            per_street[key][house][neigh] += 1

    print(f"  {len(street_totals)} distinct streets from address points "
          f"({still_unmatched} points still unmatched)")

    rows: list[dict] = []
    multi_neigh: list[tuple[str, list]] = []

    for key, houses in per_street.items():
        modal = {h: c.most_common(1)[0][0] for h, c in houses.items()}
        ranges = build_ranges(modal, min_run=args.min_run, min_support=args.min_support)
        if len(ranges) > 1:
            multi_neigh.append((display[key], ranges))
        for low, high, neigh, support in ranges:
            rows.append({
                "street_key": key,
                "street_display": display[key],
                "house_from": low,
                "house_to": high,
                "neighborhood": neigh,
                "priority": 1,
                "method": "points_range",
                "support": support,
            })

    single_range = {k for k, v in per_street.items()
                    if len(build_ranges({h: c.most_common(1)[0][0] for h, c in v.items()},
                                        min_run=args.min_run,
                                        min_support=args.min_support)) == 1}
    for key, counter in street_totals.items():
        if key in single_range:
            continue  # the tier-1 row already covers 0..HOUSE_MAX
        neigh, support = counter.most_common(1)[0]
        rows.append({
            "street_key": key,
            "street_display": display.get(key, key),
            "house_from": 0,
            "house_to": HOUSE_MAX,
            "neighborhood": neigh,
            "priority": 2,
            "method": "points_street",
            "support": support,
        })

    # ---- tier 3: street centrelines -------------------------------------
    segment_only: list[str] = []
    ambiguous: list[tuple[str, float, str]] = []

    if args.streets:
        print("processing street centrelines (fallback)...")
        streets = load(args.streets, args.encoding, args.streets_crs)
        s_street = pick_field(streets, STREET_FIELD_GUESSES, args.streets_street_field,
                              "street lines / street")
        print(f"  street lines   : street={s_street!r}  ({len(streets)} features)")
        if streets.crs != points.crs:
            streets = streets.to_crs(points.crs)

        streets = streets[[s_street, "geometry"]].copy()
        streets = streets[streets[s_street].notna()]
        streets["street_key"] = streets[s_street].map(normalize_street)
        streets = streets[streets["street_key"].astype(bool)]
        streets = streets[~streets["street_key"].isin(PLACEHOLDER_STREETS)]

        # OSM exports closed ways (roundabouts, pedestrian squares, car parks) as
        # polygons, and gpd.overlay refuses a layer with mixed geometry types.
        # Replace each polygon with its outline, then keep line geometries only.
        streets = streets[streets.geometry.notna() & ~streets.geometry.is_empty]
        kinds = streets.geometry.geom_type
        areal = kinds.isin(["Polygon", "MultiPolygon"])
        if areal.any():
            print(f"  converting {int(areal.sum())} closed ways to outlines")
            streets.loc[areal, "geometry"] = streets.loc[areal, "geometry"].boundary
        linear = streets.geometry.geom_type.isin(["LineString", "MultiLineString"])
        dropped_geom = int((~linear).sum())
        if dropped_geom:
            other = sorted(set(streets.geometry.geom_type[~linear]))
            print(f"  dropping {dropped_geom} non-line features ({', '.join(other)})")
        streets = streets[linear]
        if streets.empty:
            print("  [!] no usable line geometries in the street layer — skipping tier 3")

        covered = set(street_totals)
        missing = streets[~streets["street_key"].isin(covered)] if not streets.empty \
            else streets
        if len(missing):
            pieces = gpd.overlay(missing, neighs, how="intersection", keep_geom_type=False)
            pieces["seg_len"] = pieces.geometry.length
            lengths: dict[str, Counter] = defaultdict(Counter)
            names: dict[str, str] = {}
            for key, name, neigh, length in zip(
                pieces["street_key"], pieces[s_street],
                pieces["neighborhood"], pieces["seg_len"]
            ):
                lengths[key][neigh] += float(length)
                names.setdefault(key, clean_text(name))

            for key, counter in lengths.items():
                total = sum(counter.values())
                if total <= 0:
                    continue
                neigh, length = counter.most_common(1)[0]
                share = length / total
                rows.append({
                    "street_key": key,
                    "street_display": names[key],
                    "house_from": 0,
                    "house_to": HOUSE_MAX,
                    "neighborhood": neigh,
                    "priority": 3,
                    "method": "segment_majority",
                    "support": round(share, 3),
                })
                segment_only.append(names[key])
                if share < args.ambiguous_share:
                    ambiguous.append((names[key], share, neigh))
        print(f"  {len(segment_only)} streets recovered from centrelines only")

    # ---- tier 4: the legacy hand-made table ------------------------------
    # Last resort for streets with no address points and no centreline: the old
    # street->neighbourhood CSV. Coarser labels, but better than "unknown".
    legacy_only: list[str] = []
    if args.legacy_csv and args.use_legacy_fallback:
        covered = {r["street_key"] for r in rows}
        with open(args.legacy_csv, encoding="utf-8-sig") as fh:
            for record in csv.DictReader(fh):
                name = clean_text(record.get("STREET_NAM", ""))
                neigh = clean_text(record.get("NEIGH", ""))
                key = normalize_street(name)
                if not key or not neigh or neigh == "0" or key in covered:
                    continue
                if key in PLACEHOLDER_STREETS or key.isdigit():
                    continue
                covered.add(key)
                rows.append({
                    "street_key": key,
                    "street_display": name,
                    "house_from": 0,
                    "house_to": HOUSE_MAX,
                    "neighborhood": neigh,
                    "priority": 4,
                    "method": "legacy_table",
                    "support": 0,
                })
                legacy_only.append(name)
        print(f"  {len(legacy_only)} streets filled in from the legacy table")

    # ---- aliases ---------------------------------------------------------
    # "(החידא) ר חיים יוסף" must also be findable as "החידא".
    alias_rows = []
    seen = {(r["street_key"], r["house_from"], r["house_to"]) for r in rows}
    for row in rows:
        for alias in street_keys(row["street_display"])[1:]:
            marker = (alias, row["house_from"], row["house_to"])
            if alias != row["street_key"] and marker not in seen:
                seen.add(marker)
                clone = dict(row)
                clone["street_key"] = alias
                clone["priority"] = row["priority"] + 3  # aliases lose ties
                clone["method"] = row["method"] + "_alias"
                alias_rows.append(clone)
    rows.extend(alias_rows)

    # ---- canonicalise neighbourhood names --------------------------------
    aliases = load_neighborhood_aliases(args.neighborhood_aliases)
    if aliases:
        before = len(rows)
        canonical_rows = []
        for row in rows:
            target = aliases.get(row["neighborhood"], row["neighborhood"])
            if not target:
                continue  # explicitly dropped (road corridors)
            row["neighborhood"] = target
            canonical_rows.append(row)
        rows = canonical_rows
        print(f"  canonicalised names, dropped {before - len(rows)} rows")

    # ---- merge word-order variants of the same street --------------------
    # The address layer writes "חזן יעקב", OSM writes "יעקב חזן". Same street,
    # two keys, and sometimes two different answers. Group keys by their token
    # set and let the best-evidenced tier win, then republish that answer under
    # every spelling so a licence matches whichever form it happens to use.
    if args.merge_word_order:
        groups: dict[frozenset, list[dict]] = defaultdict(list)
        for row in rows:
            tokens = frozenset(row["street_key"].split())
            if len(tokens) > 1:
                groups[tokens].append(row)

        merged_rows = []
        n_groups = 0
        conflicts: list[tuple[list[str], list[str], str]] = []
        drop = set()
        for tokens, members in groups.items():
            keys = {r["street_key"] for r in members}
            if len(keys) < 2:
                continue
            n_groups += 1
            best = min(r["priority"] for r in members)
            authoritative = [r for r in members if r["priority"] == best]
            canonical = authoritative[0]["street_key"]
            kept = {r["neighborhood"] for r in authoritative}
            discarded = {r["neighborhood"] for r in members
                         if r["priority"] != best} - kept
            if discarded:
                conflicts.append((sorted(keys), sorted(kept), sorted(discarded)))
            for row in members:
                if row["street_key"] != canonical or row["priority"] != best:
                    drop.add(id(row))
            for key in keys - {canonical}:
                for row in authoritative:
                    clone = dict(row)
                    clone["street_key"] = key
                    clone["method"] = row["method"] + "_wordorder"
                    merged_rows.append(clone)

        rows = [r for r in rows if id(r) not in drop] + merged_rows
        print(f"  merged {n_groups} word-order variant groups "
              f"({len(conflicts)} of them disagreed on the neighbourhood)")
        word_order_conflicts = conflicts
    else:
        word_order_conflicts = []

    rows.sort(key=lambda r: (r["street_key"], r["priority"], r["house_from"]))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "street_key", "street_display", "house_from", "house_to",
            "neighborhood", "priority", "method", "support",
        ])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nwrote {len(rows)} rows -> {out_path}")

    write_report(args, rows, multi_neigh, segment_only, ambiguous, still_unmatched,
                 legacy_only, word_order_conflicts)


# --------------------------------------------------------------------------
# QA report
# --------------------------------------------------------------------------

def write_report(args, rows, multi_neigh, segment_only, ambiguous, unmatched_points,
                 legacy_only=(), word_order_conflicts=()):
    path = Path(args.report or Path(args.out).with_suffix(".report.txt"))
    keys = {r["street_key"] for r in rows}
    lines = [
        "street index QA report",
        "=" * 60,
        f"rows                        : {len(rows)}",
        f"distinct street keys        : {len(keys)}",
        f"address points unmatched    : {unmatched_points}",
        f"streets crossing >1 area    : {len(multi_neigh)}",
        f"streets from centrelines    : {len(segment_only)}",
        f"streets from legacy table   : {len(legacy_only)}",
        f"distinct neighbourhoods     : {len({r['neighborhood'] for r in rows})}",
        f"word-order conflicts        : {len(word_order_conflicts)}",
        f"ambiguous centreline splits : {len(ambiguous)}",
        "",
        "-- streets crossing more than one neighbourhood " + "-" * 14,
    ]
    for name, ranges in sorted(multi_neigh)[:400]:
        parts = " | ".join(
            f"{low}-{'∞' if high >= HOUSE_MAX else high}: {neigh} (n={support})"
            for low, high, neigh, support in ranges
        )
        lines.append(f"  {name}: {parts}")

    lines += ["", "-- same street spelled two ways, tiers disagreed " + "-" * 13,
              "   (resolved in favour of the best-evidenced tier; verify these)"]
    for variants, kept, discarded in sorted(word_order_conflicts)[:100]:
        lines.append(f"  {' / '.join(variants)}"
                     f"\n      kept      : {', '.join(kept)}"
                     f"\n      discarded : {', '.join(discarded)}")

    lines += ["", "-- centreline fallback, dominant share below threshold " + "-" * 6]
    for name, share, neigh in sorted(ambiguous, key=lambda x: x[1])[:200]:
        lines.append(f"  {name}: {neigh} covers only {share:.0%} of the length")

    if args.legacy_csv:
        lines += ["", "-- coverage vs the legacy street->neighbourhood table " + "-" * 6]
        legacy = {}
        with open(args.legacy_csv, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                key = normalize_street(row.get("STREET_NAM", ""))
                if key:
                    legacy[key] = clean_text(row.get("NEIGH", ""))
        missing = sorted(set(legacy) - keys)
        lines.append(f"  in legacy table but not in the new index: {len(missing)}")
        lines += [f"    {k}" for k in missing[:200]]

    Path(path).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote QA report          -> {path}")
    print("\nread the report before shipping the index — the "
          "'crossing >1 neighbourhood' section is where the value is.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--points", required=True, help="address point layer (shp/geojson/gpkg)")
    p.add_argument("--neighborhoods", required=True, help="neighbourhood polygon layer")
    p.add_argument("--streets", help="street centreline layer, used as fallback")
    p.add_argument("--out", default="data/street_index.csv")
    p.add_argument("--report", help="path for the QA report (default: alongside --out)")
    p.add_argument("--legacy-csv", help="old streer_to_neigh.csv, for a coverage diff")
    p.add_argument("--use-legacy-fallback", action="store_true",
                   help="also use --legacy-csv as a last-resort tier for streets "
                        "with no address points and no centreline")
    p.add_argument("--encoding", default="utf-8",
                   help="shapefile attribute encoding (try cp1255 if Hebrew is mojibake)")
    p.add_argument("--points-street-field")
    p.add_argument("--points-house-field")
    p.add_argument("--points-x-field")
    p.add_argument("--points-y-field")
    p.add_argument("--points-crs", default="EPSG:2039",
                   help="CRS of the address layer (default: Israeli TM Grid)")
    p.add_argument("--neigh-field")
    p.add_argument("--neigh-crs", help="force a CRS on the neighbourhood layer if it declares none")
    p.add_argument("--streets-street-field")
    p.add_argument("--streets-crs", help="force a CRS on the street layer if it declares none")
    p.add_argument("--no-merge-word-order", dest="merge_word_order",
                   action="store_false",
                   help="keep 'חזן יעקב' and 'יעקב חזן' as separate streets "
                        "(merging them is the default)")
    p.add_argument("--neighborhood-aliases",
                   help="CSV mapping legacy neighbourhood names onto the polygon "
                        "layer's names (alias,canonical). Empty canonical drops the row.")
    p.add_argument("--min-support", type=int, default=1,
                   help="merge house-number ranges backed by fewer than this many "
                        "address points into their strongest neighbour (1 = off)")
    p.add_argument("--min-run", type=int, default=2,
                   help="house-number runs shorter than this are absorbed by their neighbours")
    p.add_argument("--snap-distance", type=float, default=25.0,
                   help="snap points falling outside every polygon, in layer units (0 to disable)")
    p.add_argument("--ambiguous-share", type=float, default=0.6,
                   help="flag centreline streets whose dominant area covers less than this")
    build(p.parse_args())


if __name__ == "__main__":
    main()
