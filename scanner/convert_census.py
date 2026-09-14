#!/usr/bin/env python3
"""Convert the raw store census list into stores.tsv.

Usage:
    python3 convert_census.py <census_list.txt> <stores.tsv>

Input is a 5-column TSV with no header, as produced by the store census step:
    store_id  name  distance_m  distance_mi  cuisines
e.g.  592083\tQQ Express\t435m\t0.27mi\tchinese

Output columns: store_id, name, distance_km

distance_km is derived from the metres column (the higher-precision of the two
distance fields) rather than by converting the rounded miles value.
"""
import argparse
import sys

OUT_COLS = ["store_id", "name", "distance_km"]


def parse_metres(text):
    text = text.strip()
    if not text.endswith("m") or text.endswith("mi"):
        raise ValueError(f"expected a metres value like '435m', got {text!r}")
    return float(text[:-1])


def convert(in_path, out_path):
    rows, bad = [], []
    with open(in_path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 5:
                bad.append((lineno, f"expected 5 columns, got {len(fields)}"))
                continue
            store_id, name, metres = fields[0].strip(), fields[1].strip(), fields[2]
            try:
                km = parse_metres(metres) / 1000.0
            except ValueError as e:
                bad.append((lineno, str(e)))
                continue
            rows.append((store_id, " ".join(name.split()), f"{km:.2f}"))

    seen, dupes = set(), []
    for store_id, _, _ in rows:
        if store_id in seen:
            dupes.append(store_id)
        seen.add(store_id)

    rows.sort(key=lambda r: float(r[2]))
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("\t".join(OUT_COLS) + "\n")
        for row in rows:
            out.write("\t".join(row) + "\n")

    print(f"stores.tsv: {len(rows)} stores written to {out_path}")
    for lineno, reason in bad:
        print(f"  SKIP line {lineno}: {reason}", file=sys.stderr)
    for store_id in dupes:
        print(f"  WARN duplicate store_id: {store_id}", file=sys.stderr)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("census")
    ap.add_argument("out", nargs="?", default="stores.tsv")
    args = ap.parse_args()
    convert(args.census, args.out)


if __name__ == "__main__":
    main()
