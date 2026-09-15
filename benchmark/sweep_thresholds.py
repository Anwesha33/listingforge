#!/usr/bin/env python3
"""Measure the duplicate-similarity thresholds instead of guessing them.

The moderation service refuses a listing above one cosine-similarity threshold
and asks a human above a lower one. Those two numbers were originally picked by
intuition (0.97 / 0.92) and were wrong: a genuine self-duplicate scored 0.9475
and was only sent to review, while a true cross-seller duplicate scored 0.8793
and was missed entirely.

This script computes every pairwise similarity from the embeddings the pipeline
actually produced, labels each pair using the dup_group field in dataset.json,
and sweeps the threshold to find where precision and recall trade off.

Caveat worth stating plainly: the labelled set contains only a handful of
positive pairs, so the resulting numbers indicate roughly where the boundary
lies rather than pinning it precisely. Production calibration needs real
catalogue data.
"""
from __future__ import annotations

import argparse
import itertools
import json
import pathlib

import subprocess
import sys


def fetch_pairs() -> list[tuple[str, str, float]]:
    """Read every pairwise cosine similarity out of Postgres.

    Run through psql rather than a Python driver: the benchmark should not
    require a database library that the services themselves do not use.
    """
    sql = """
        SELECT a.seller_sku, b.seller_sku,
               round((1 - (ea.embedding <=> eb.embedding))::numeric, 6)
          FROM listings a
          JOIN listing_embeddings ea ON ea.listing_id = a.id
          JOIN listings b ON b.id < a.id
          JOIN listing_embeddings eb ON eb.listing_id = b.id
         ORDER BY 3 DESC
    """
    out = subprocess.run(
        ["docker", "exec", "listingforge-postgres-1",
         "psql", "-U", "listingforge", "-d", "listingforge", "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True, text=True, check=True).stdout

    pairs = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            pairs.append((parts[0].strip(), parts[1].strip(), float(parts[2])))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(pathlib.Path(__file__).parent / "dataset.json"))
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    rows = json.load(open(args.dataset))
    group_of = {r["sku"]: r.get("dup_group") for r in rows}

    pairs = fetch_pairs()
    if not pairs:
        print("no embeddings found; run the pipeline first", file=sys.stderr)
        return 1

    labelled = []
    for a, b, sim in pairs:
        ga, gb = group_of.get(a), group_of.get(b)
        is_dup = bool(ga) and ga == gb
        labelled.append((a, b, sim, is_dup))

    positives = [p for p in labelled if p[3]]
    negatives = [p for p in labelled if not p[3]]

    print(f"{len(labelled)} pairs from {len({p[0] for p in pairs} | {p[1] for p in pairs})} listings")
    print(f"  {len(positives)} labelled duplicate pairs, {len(negatives)} non-duplicate pairs\n")

    print("  true duplicate pairs and their similarity")
    for a, b, sim, _ in sorted(positives, key=lambda p: -p[2]):
        print(f"    {a:<10} {b:<10} {sim:.4f}")

    top_neg = sorted(negatives, key=lambda p: -p[2])[:5]
    print("\n  highest-scoring NON-duplicate pairs (these set the ceiling)")
    for a, b, sim, _ in top_neg:
        print(f"    {a:<10} {b:<10} {sim:.4f}")

    print("\n  threshold sweep")
    print(f"  {'thresh':>7} {'TP':>3} {'FP':>3} {'FN':>3} {'precision':>10} {'recall':>7} {'f1':>7}")
    sweep = []
    best = None
    for i in range(60, 100):
        t = i / 100
        tp = sum(1 for p in labelled if p[2] >= t and p[3])
        fp = sum(1 for p in labelled if p[2] >= t and not p[3])
        fn = sum(1 for p in labelled if p[2] < t and p[3])
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        sweep.append({"threshold": t, "tp": tp, "fp": fp, "fn": fn,
                      "precision": prec, "recall": rec, "f1": f1})
        if best is None or f1 > best["f1"]:
            best = sweep[-1]
        if i % 2 == 0 and tp + fp + fn:
            print(f"  {t:>7.2f} {tp:>3} {fp:>3} {fn:>3} {prec:>10.3f} {rec:>7.3f} {f1:>7.3f}")

    # The review threshold is placed just below the lowest true duplicate so
    # that every known duplicate is at least seen by a human; the reject
    # threshold is placed above the highest non-duplicate so that an automatic
    # refusal never fires on a pair known to be legitimate.
    lowest_positive = min(p[2] for p in positives) if positives else 0.0
    highest_negative = max(p[2] for p in negatives) if negatives else 0.0

    recommended_review = round(max(0.0, lowest_positive - 0.02), 2)
    recommended_reject = round(min(0.99, max(highest_negative + 0.01, lowest_positive + 0.01)), 2)

    print(f"\n  best F1 at threshold {best['threshold']:.2f} "
          f"(P={best['precision']:.3f} R={best['recall']:.3f} F1={best['f1']:.3f})")
    print(f"  lowest true-duplicate similarity : {lowest_positive:.4f}")
    print(f"  highest non-duplicate similarity : {highest_negative:.4f}")
    print(f"  separation margin                : {lowest_positive - highest_negative:+.4f}")
    print(f"\n  recommended DUPLICATE_REVIEW = {recommended_review:.2f}")
    print(f"  recommended DUPLICATE_REJECT = {recommended_reject:.2f}")

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps({
            "pairs": len(labelled),
            "positives": len(positives),
            "lowest_positive": lowest_positive,
            "highest_negative": highest_negative,
            "separation": lowest_positive - highest_negative,
            "best_f1": best,
            "recommended_review": recommended_review,
            "recommended_reject": recommended_reject,
            "sweep": sweep,
        }, indent=2))
        print(f"\n  written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
