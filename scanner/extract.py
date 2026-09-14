#!/usr/bin/env python3
"""Extract compact item/promo tables from a raw dd-cli scan directory.

Usage:
    python3 extract.py <scan_dir> [-o OUT_DIR]

Reads menu_<store_id>.json and promo_<store_id>.json (dd-cli --json-output
envelopes) and writes items.tsv and promos.tsv.

Zero AI involvement - deterministic, reusable every scan.
"""
import argparse
import glob
import json
import os
import sys

ITEM_COLS = ["store_id", "item_id", "name", "category", "price", "orderable", "req_mods"]
PROMO_COLS = ["store_id", "campaign_id", "title", "description", "source"]


def clean(value, limit):
    """Collapse TSV-hostile whitespace and truncate.

    Item names in the real data contain trailing newlines (e.g. 'Cheese\\n'),
    which would otherwise split a single record across two TSV lines.
    """
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def load(path):
    """Return the structuredContent payload, or None if this file is unusable.

    Returns a (payload, reason) pair so the caller can report *why* a store
    is missing rather than silently dropping it.
    """
    try:
        if os.path.getsize(path) == 0:
            return None, "empty file (tool call produced no output)"
        with open(path, encoding="utf-8") as f:
            envelope = json.load(f)
    except OSError as e:
        return None, f"unreadable: {e}"
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"

    if not isinstance(envelope, dict):
        return None, "envelope is not an object"
    if envelope.get("isError"):
        return None, "envelope isError=true"

    payload = envelope.get("structuredContent")
    if not isinstance(payload, dict):
        return None, "missing structuredContent"
    if payload.get("success") is False:
        return None, f"success=false: {clean(payload.get('message'), 120)}"
    return payload, None


def store_id_from(path, prefix):
    return os.path.basename(path)[len(prefix):-len(".json")]


def extract_items(scan_dir, out_path):
    ok = skipped = rows = 0
    failures = []
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("\t".join(ITEM_COLS) + "\n")
        for path in sorted(glob.glob(os.path.join(scan_dir, "menu_*.json"))):
            store_id = store_id_from(path, "menu_")
            payload, reason = load(path)
            if payload is None:
                skipped += 1
                failures.append((store_id, reason))
                continue
            items = payload.get("items")
            if not items:
                skipped += 1
                failures.append((store_id, "no items in payload"))
                continue
            ok += 1
            for item in items:
                price = item.get("price")
                if not isinstance(price, (int, float)) or isinstance(price, bool):
                    continue
                out.write("\t".join([
                    store_id,
                    clean(item.get("item_id"), 40),
                    clean(item.get("name"), 80),
                    clean(item.get("category_name"), 40),
                    f"{price:.2f}",
                    "1" if item.get("is_orderable") else "0",
                    "1" if item.get("has_required_modifiers") else "0",
                ]) + "\n")
                rows += 1
    return ok, skipped, rows, failures


def extract_promos(scan_dir, out_path):
    ok = skipped = rows = 0
    failures = []
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("\t".join(PROMO_COLS) + "\n")
        for path in sorted(glob.glob(os.path.join(scan_dir, "promo_*.json"))):
            store_id = store_id_from(path, "promo_")
            payload, reason = load(path)
            if payload is None:
                skipped += 1
                failures.append((store_id, reason))
                continue
            ok += 1
            # A store with zero promotions is a valid, meaningful result.
            for promo in payload.get("promotions") or []:
                out.write("\t".join([
                    store_id,
                    clean(promo.get("campaign_id"), 40),
                    clean(promo.get("title"), 80),
                    clean(promo.get("description"), 120),
                    clean(promo.get("source"), 20),
                ]) + "\n")
                rows += 1
    return ok, skipped, rows, failures


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scan_dir", help="directory holding menu_*.json / promo_*.json")
    ap.add_argument("-o", "--out-dir", default=None,
                    help="where to write the TSVs (default: scan_dir)")
    args = ap.parse_args(argv)

    scan_dir = os.path.expanduser(args.scan_dir)
    out_dir = os.path.expanduser(args.out_dir or scan_dir)
    if not os.path.isdir(scan_dir):
        sys.exit(f"scan_dir not found: {scan_dir}")
    os.makedirs(out_dir, exist_ok=True)

    m_ok, m_skip, m_rows, m_fail = extract_items(
        scan_dir, os.path.join(out_dir, "items.tsv"))
    p_ok, p_skip, p_rows, p_fail = extract_promos(
        scan_dir, os.path.join(out_dir, "promos.tsv"))

    print(f"items.tsv : {m_rows} rows from {m_ok} stores ({m_skip} skipped)")
    print(f"promos.tsv: {p_rows} rows from {p_ok} stores ({p_skip} skipped)")
    for label, failures in (("menu", m_fail), ("promo", p_fail)):
        for store_id, reason in failures:
            print(f"  SKIP {label} {store_id}: {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
