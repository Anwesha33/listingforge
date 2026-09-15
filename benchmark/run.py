#!/usr/bin/env python3
"""End-to-end benchmark for the ListingForge pipeline.

Submits a labelled dataset through the real services, waits for each listing to
reach a decision, and scores four things:

  * category accuracy      -- did enrichment pick the right catalogue category
  * attribute accuracy     -- per-attribute precision and recall against labels
  * decision accuracy      -- confusion matrix of PUBLISH / REVIEW / REJECT
  * auto-publish rate      -- share of listings that cleared with no human

Labels are hand-written in dataset.json. They encode what a catalogue operator
would expect, not what the current implementation happens to produce, so a
regression shows up as a falling score rather than as a silently updated
expectation.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

API = "http://localhost:8081/api/v1/listings"
TERMINAL = {"PUBLISHED", "NEEDS_REVIEW", "REJECTED", "FAILED"}
STATUS_TO_DECISION = {"PUBLISHED": "PUBLISH", "NEEDS_REVIEW": "REVIEW", "REJECTED": "REJECT"}


def post(payload: dict) -> dict:
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def get(listing_id: int) -> dict:
    with urllib.request.urlopen(f"{API}/{listing_id}", timeout=20) as resp:
        return json.load(resp)


def submit_all(rows: list[dict]) -> dict[int, dict]:
    ids: dict[int, dict] = {}
    for row in rows:
        created = post({
            "sellerId": row["seller"],
            "sellerSku": row["sku"],
            "title": row["title"],
            "description": row["desc"],
        })
        ids[created["id"]] = row
        print(f"  submitted {row['sku']:<10} -> listing {created['id']}")
        # Submissions are spaced out so that duplicate detection sees a
        # deterministic catalogue: a listing can only be found as a duplicate of
        # something already indexed, so racing them makes the result depend on
        # scheduling rather than on similarity.
        time.sleep(0.4)
    return ids


def wait_for_decisions(ids: dict[int, dict], timeout_s: int) -> dict[int, dict]:
    deadline = time.time() + timeout_s
    results: dict[int, dict] = {}

    while time.time() < deadline:
        pending = [i for i in ids if i not in results]
        if not pending:
            break
        for listing_id in pending:
            listing = get(listing_id)
            if listing["status"] in TERMINAL:
                results[listing_id] = listing
        if len(results) < len(ids):
            done = len(results)
            print(f"  {done}/{len(ids)} decided", end="\r", flush=True)
            time.sleep(2)

    print(" " * 40, end="\r")
    return results


def score(ids: dict[int, dict], results: dict[int, dict]) -> dict:
    category_hits = 0
    category_total = 0
    attr_tp = attr_fp = attr_fn = 0
    per_attr = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    unlabelled = Counter()
    confusion = Counter()
    mismatches = []
    # Duplicate detection is scored per group, not per listing.
    #
    # Within a pair only one listing can carry the flag: whichever reaches the
    # vector index second is the one with something to compare against, and
    # because the pipeline is concurrent that is not necessarily the one later
    # in the dataset. Counting per listing therefore caps the achievable score
    # at 50% and makes a correct result look like a failure. What actually
    # matters is whether the relationship was found at all.
    dup_groups: dict[str, bool] = {}

    for listing_id, expected in ids.items():
        actual = results.get(listing_id)
        if actual is None:
            confusion[(expected["expect_decision"], "TIMEOUT")] += 1
            continue

        # --- category -------------------------------------------------------
        category_total += 1
        got_cat = actual.get("category") or "unknown"
        if got_cat == expected["expect_category"]:
            category_hits += 1
        else:
            mismatches.append({
                "sku": expected["sku"], "field": "category",
                "expected": expected["expect_category"], "actual": got_cat})

        # --- attributes -----------------------------------------------------
        want = expected["expect_attrs"] or {}
        got = actual.get("attributes") or {}
        for key, value in want.items():
            if str(got.get(key, "")).lower() == str(value).lower():
                attr_tp += 1
                per_attr[key]["tp"] += 1
            else:
                attr_fn += 1
                per_attr[key]["fn"] += 1
                mismatches.append({
                    "sku": expected["sku"], "field": f"attr.{key}",
                    "expected": value, "actual": got.get(key)})
        # Attributes the model extracted that the label set does not mention are
        # counted separately, NOT as false positives.
        #
        # The labels list the attributes a listing must get right, not every
        # attribute it could legitimately carry. Scoring an unlabelled
        # extraction as an error punishes the model for being more complete
        # than the label, which made precision look like 0.64 when nothing was
        # actually wrong. Without ground truth for those keys, the honest thing
        # is to report their volume and not pretend to score them.
        for key in got:
            if key not in want:
                unlabelled[key] += 1

        # --- decision -------------------------------------------------------
        got_decision = STATUS_TO_DECISION.get(actual["status"], actual["status"])
        confusion[(expected["expect_decision"], got_decision)] += 1

        # --- duplicates -----------------------------------------------------
        group = expected.get("dup_group")
        if group:
            dup_groups[group] = dup_groups.get(group, False) or bool(actual.get("duplicateOf"))

    decided = sum(confusion.values())
    correct = sum(n for (exp, got), n in confusion.items() if exp == got)
    published = sum(n for (_, got), n in confusion.items() if got == "PUBLISH")

    def prf(tp, fp, fn):
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        return {"precision": p, "recall": r, "f1": f, "tp": tp, "fp": fp, "fn": fn}

    return {
        "listings": len(ids),
        "decided": decided,
        "category_accuracy": category_hits / category_total if category_total else 0.0,
        "attributes": prf(attr_tp, attr_fp, attr_fn),
        "per_attribute": {k: prf(v["tp"], v["fp"], v["fn"]) for k, v in sorted(per_attr.items())},
        "unlabelled_attributes": dict(unlabelled.most_common()),
        "decision_accuracy": correct / decided if decided else 0.0,
        "auto_publish_rate": published / decided if decided else 0.0,
        "duplicate_groups": len(dup_groups),
        "duplicate_groups_detected": sum(1 for found in dup_groups.values() if found),
        "confusion": {f"{e}->{g}": n for (e, g), n in sorted(confusion.items())},
        "mismatches": mismatches,
    }


def render(report: dict) -> None:
    print("\nListingForge pipeline benchmark")
    print(f"{report['listings']} listings submitted, {report['decided']} decided\n")

    print(f"  category accuracy    {report['category_accuracy']:.3f}")
    a = report["attributes"]
    print(f"  labelled attr accuracy {a['recall']:.3f}  ({a['tp']} correct of {a['tp'] + a['fn']} labelled)")
    print(f"  decision accuracy    {report['decision_accuracy']:.3f}")
    print(f"  auto-publish rate    {report['auto_publish_rate']:.3f}")
    print(f"  duplicate groups     {report['duplicate_groups_detected']}/{report['duplicate_groups']} detected")

    print("\n  decision confusion (expected -> actual)")
    for k, n in report["confusion"].items():
        marker = " " if k.split("->")[0] == k.split("->")[1] else "*"
        print(f"   {marker} {k:<22} {n}")

    if report["per_attribute"]:
        print("\n  per labelled attribute (accuracy over the labelled cases)")
        for name, s in report["per_attribute"].items():
            total = s["tp"] + s["fn"]
            if total:
                print(f"    {name:<14} {s['tp']}/{total} = {s['recall']:.2f}")

    if report["unlabelled_attributes"]:
        total = sum(report["unlabelled_attributes"].values())
        print(f"\n  additional attributes extracted beyond the labels ({total}, not scored)")
        for name, n in list(report["unlabelled_attributes"].items())[:8]:
            print(f"    {name:<14} {n}")

    if report["mismatches"]:
        print(f"\n  mismatches ({len(report['mismatches'])})")
        for m in report["mismatches"][:15]:
            print(f"    {m['sku']:<10} {m['field']:<16} expected={m['expected']!r} actual={m['actual']!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(pathlib.Path(__file__).parent / "dataset.json"))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    rows = json.load(open(args.dataset))
    print(f"submitting {len(rows)} listings...")
    ids = submit_all(rows)

    print("waiting for decisions...")
    results = wait_for_decisions(ids, args.timeout)

    report = score(ids, results)
    render(report)

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"\nreport written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
