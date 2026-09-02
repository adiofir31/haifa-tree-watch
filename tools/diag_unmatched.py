#!/usr/bin/env python
"""Find out WHY address points fell outside every neighbourhood polygon.

Writes unmatched_points.geojson (open it in ArcGIS Pro over the neighbourhood
layer — the pattern is usually obvious at a glance) and prints the streets and
distances involved.

    python tools/diag_unmatched.py ^
        --points        data\\raw\\addres_haifa.csv ^
        --neighborhoods data\\raw\\schunot.json
"""

from __future__ import annotations

import argparse
from collections import Counter

import geopandas as gpd
import pandas as pd


def read_points(path, x_field, y_field, street_field, crs):
    for enc in ("utf-8-sig", "cp1255", "utf-8"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    df[x_field] = pd.to_numeric(df[x_field], errors="coerce")
    df[y_field] = pd.to_numeric(df[y_field], errors="coerce")
    df = df.dropna(subset=[x_field, y_field])
    return gpd.GeoDataFrame(
        df[[street_field, x_field, y_field]],
        geometry=gpd.points_from_xy(df[x_field], df[y_field]),
        crs=crs,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--points", required=True)
    p.add_argument("--neighborhoods", required=True)
    p.add_argument("--points-x-field", default="X")
    p.add_argument("--points-y-field", default="Y")
    p.add_argument("--points-street-field", default="street")
    p.add_argument("--neigh-field", default="SchName")
    p.add_argument("--points-crs", default="EPSG:2039")
    p.add_argument("--out", default="unmatched_points.geojson")
    args = p.parse_args()

    pts = read_points(args.points, args.points_x_field, args.points_y_field,
                      args.points_street_field, args.points_crs)
    neighs = gpd.read_file(args.neighborhoods).to_crs(pts.crs)
    neighs = neighs[[args.neigh_field, "geometry"]].rename(
        columns={args.neigh_field: "neighborhood"})

    joined = gpd.sjoin(pts, neighs, how="left", predicate="within")
    miss = joined[joined["neighborhood"].isna()].drop(columns=["index_right"])
    print(f"points            : {len(pts)}")
    print(f"unmatched         : {len(miss)}  ({len(miss) / len(pts):.0%})")

    if miss.empty:
        return

    # how far is each unmatched point from the nearest polygon?
    union = neighs.geometry.union_all()
    miss = miss.copy()
    miss["dist_m"] = miss.geometry.distance(union).round(1)

    print("\ndistance to the nearest neighbourhood polygon:")
    bins = [0, 25, 100, 500, 2000, 10_000, 1e9]
    labels = ["<25m", "25-100m", "100-500m", "0.5-2km", "2-10km", ">10km"]
    buckets = pd.cut(miss["dist_m"], bins=bins, labels=labels, right=False)
    for label, count in buckets.value_counts().sort_index().items():
        print(f"  {label:>9}: {count}")

    print("\nstreets with the most unmatched points:")
    for name, count in Counter(miss[args.points_street_field]).most_common(25):
        sample = miss[miss[args.points_street_field] == name]["dist_m"].median()
        print(f"  {count:>5}  {name}   (median {sample}m from cover)")

    print("\nbounding boxes (same CRS):")
    print(f"  address points : {[round(v) for v in pts.total_bounds]}")
    print(f"  neighbourhoods : {[round(v) for v in neighs.total_bounds]}")

    miss.to_file(args.out, driver="GeoJSON")
    print(f"\nwrote {args.out} — open it over the neighbourhood layer in ArcGIS Pro")


if __name__ == "__main__":
    main()
