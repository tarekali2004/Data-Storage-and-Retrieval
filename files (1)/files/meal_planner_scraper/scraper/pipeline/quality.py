"""
Data-quality management.

Three responsibilities, in order:

  1. Missing values
       - If a required field (title or ingredients) is missing → drop record.
       - Optional numeric (e.g. calories) → median imputation by cuisine,
         flagged with __imputed.
       - Optional categorical (e.g. cuisine) → leave None, no fabrication.

  2. Duplicates
       - Exact: identical source_url -> keep richest record, delete others.
       - Near: same normalized title and >= 80% ingredient overlap -> merge.

  3. Outliers / noise
       - Calorie outliers (> 1500/srv) flagged for manual review.
       - Cooking time > 6h flagged "slow_cook", record kept.
       - Records that fail multiple sanity checks are removed.

Run after clean.py and enrich.py.
"""
from __future__ import annotations
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RECIPES_DIR = DATA_DIR / "recipes"
INDEX_PATH = DATA_DIR / "index.json"
QUALITY_REPORT = DATA_DIR / "quality_report.json"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _ingredient_set(rec: dict) -> set[str]:
    out = set()
    for ing in rec.get("ingredients") or []:
        if isinstance(ing, dict):
            name = (ing.get("name") or "").lower().strip()
        else:
            name = str(ing).lower().strip()
        # Reduce to first noun-ish token for fuzzy compare
        first = re.split(r"[,\s]+", name, maxsplit=1)[0]
        if first:
            out.add(first)
    return out


def _title_norm(title: str | None) -> str:
    if not title:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _record_completeness(rec: dict) -> int:
    """Higher = more complete; used to break ties between duplicates."""
    score = 0
    for k in ("title", "cuisine", "calories_per_serving",
              "total_time_min", "servings"):
        if rec.get(k):
            score += 1
    score += min(len(rec.get("ingredients") or []), 20)
    score += min(len(rec.get("instructions") or []), 20)
    score += len(rec.get("diet_tags") or [])
    score += len(rec.get("macros") or {})
    return score


# ----------------------------------------------------------------------
# 1. Missing-value handling
# ----------------------------------------------------------------------
REQUIRED = ("title", "ingredients")

def _drop_records_missing_required(records: dict[str, dict], report: dict) -> dict[str, dict]:
    keep = {}
    for rid, rec in records.items():
        missing = [
            f for f in REQUIRED
            if not rec.get(f) or (isinstance(rec.get(f), list) and len(rec[f]) == 0)
        ]
        if missing:
            report["dropped_missing_required"].append({"id": rid, "missing": missing})
            continue
        keep[rid] = rec
    return keep


def _impute_calories_by_cuisine(records: dict[str, dict], report: dict) -> None:
    """Fill missing calories_per_serving with the median for the same cuisine."""
    by_cuisine: dict[str, list[int]] = defaultdict(list)
    for rec in records.values():
        cal = rec.get("calories_per_serving")
        cuisine = rec.get("cuisine") or "_unknown"
        if cal is not None:
            by_cuisine[cuisine].append(int(cal))

    medians = {c: int(statistics.median(v)) for c, v in by_cuisine.items() if v}
    global_median = int(statistics.median(
        [c for v in by_cuisine.values() for c in v]
    )) if by_cuisine else None

    for rid, rec in records.items():
        if rec.get("calories_per_serving") is None:
            cuisine = rec.get("cuisine") or "_unknown"
            imputed = medians.get(cuisine, global_median)
            if imputed is not None:
                rec["calories_per_serving"] = imputed
                rec.setdefault("__imputed", []).append("calories_per_serving")
                report["imputed_calories"] += 1


# ----------------------------------------------------------------------
# 2. Duplicate handling
# ----------------------------------------------------------------------
def _resolve_duplicates(records: dict[str, dict], report: dict) -> dict[str, dict]:
    # Exact: same source_url
    by_url: dict[str, list[str]] = defaultdict(list)
    for rid, rec in records.items():
        url = rec.get("source_url")
        if url:
            by_url[url].append(rid)

    drop_ids: set[str] = set()
    for url, ids in by_url.items():
        if len(ids) <= 1:
            continue
        ids_sorted = sorted(ids, key=lambda i: _record_completeness(records[i]), reverse=True)
        kept = ids_sorted[0]
        for d in ids_sorted[1:]:
            drop_ids.add(d)
            report["duplicates_exact"].append({"kept": kept, "dropped": d, "url": url})

    # Near: same normalized title and >= 80% ingredient overlap
    items = [(rid, rec) for rid, rec in records.items() if rid not in drop_ids]
    by_title: dict[str, list[str]] = defaultdict(list)
    for rid, rec in items:
        by_title[_title_norm(rec.get("title"))].append(rid)
    for title, ids in by_title.items():
        if len(ids) <= 1 or not title:
            continue
        ids_sorted = sorted(ids, key=lambda i: _record_completeness(records[i]), reverse=True)
        kept = ids_sorted[0]
        kept_ings = _ingredient_set(records[kept])
        for d in ids_sorted[1:]:
            if d in drop_ids:
                continue
            d_ings = _ingredient_set(records[d])
            if not kept_ings or not d_ings:
                continue
            overlap = len(kept_ings & d_ings) / max(len(kept_ings), len(d_ings))
            if overlap >= 0.80:
                drop_ids.add(d)
                report["duplicates_near"].append(
                    {"kept": kept, "dropped": d, "title": title, "overlap": round(overlap, 2)}
                )

    return {rid: rec for rid, rec in records.items() if rid not in drop_ids}


# ----------------------------------------------------------------------
# 3. Outliers / noise
# ----------------------------------------------------------------------
def _flag_outliers(records: dict[str, dict], report: dict) -> dict[str, dict]:
    keep = {}
    for rid, rec in records.items():
        bad = 0
        cal = rec.get("calories_per_serving") or 0
        if cal > 1500:
            rec.setdefault("__flags", []).append("high_calories")
            report["flagged_high_calories"] += 1
            bad += 1
        ttime = rec.get("total_time_min") or 0
        if ttime > 360:
            rec.setdefault("__flags", []).append("slow_cook")
            report["flagged_slow_cook"] += 1
        # Garbled title (mostly non-letters)
        title = rec.get("title") or ""
        if title and len(title) > 0:
            letters = sum(1 for c in title if c.isalpha() or c.isspace())
            if letters / len(title) < 0.5:
                rec.setdefault("__flags", []).append("garbled_title")
                bad += 1
        if bad >= 2:
            report["dropped_unsalvageable"].append({"id": rid, "title": title})
            continue
        keep[rid] = rec
    return keep


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------
def _load_all() -> dict[str, dict]:
    out = {}
    for fp in sorted(RECIPES_DIR.glob("r_*.json")):
        rec = json.loads(fp.read_text(encoding="utf-8"))
        out[rec["id"]] = rec
    return out


def _persist(records: dict[str, dict], deleted_ids: set[str]) -> None:
    # Delete files for dropped records
    for rid in deleted_ids:
        fp = RECIPES_DIR / f"{rid}.json"
        if fp.exists():
            fp.unlink()
    # Rewrite remaining (in case __flags / __imputed were added)
    for rid, rec in records.items():
        (RECIPES_DIR / f"{rid}.json").write_text(
            json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    # Update master index
    if INDEX_PATH.exists():
        idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        idx["records"] = {
            rid: meta for rid, meta in idx["records"].items() if rid in records
        }
        INDEX_PATH.write_text(
            json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def main() -> None:
    records = _load_all()
    initial_count = len(records)
    if not records:
        print(f"No records to process in {RECIPES_DIR}")
        return

    report = {
        "initial_count": initial_count,
        "dropped_missing_required": [],
        "imputed_calories": 0,
        "duplicates_exact": [],
        "duplicates_near": [],
        "flagged_high_calories": 0,
        "flagged_slow_cook": 0,
        "dropped_unsalvageable": [],
    }

    records = _drop_records_missing_required(records, report)
    _impute_calories_by_cuisine(records, report)
    after_dedup = _resolve_duplicates(records, report)
    after_outliers = _flag_outliers(after_dedup, report)

    final_count = len(after_outliers)
    deleted_ids = (
        set(_load_all().keys()) - set(after_outliers.keys())
    )
    _persist(after_outliers, deleted_ids)

    report["final_count"] = final_count
    QUALITY_REPORT.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"Quality pass complete: {initial_count} → {final_count}")
    print(f"  dropped (missing): {len(report['dropped_missing_required'])}")
    print(f"  imputed calories:  {report['imputed_calories']}")
    print(f"  duplicates exact:  {len(report['duplicates_exact'])}")
    print(f"  duplicates near:   {len(report['duplicates_near'])}")
    print(f"  flagged calories:  {report['flagged_high_calories']}")
    print(f"  flagged slow-cook: {report['flagged_slow_cook']}")
    print(f"  dropped unsalvageable: {len(report['dropped_unsalvageable'])}")
    print(f"Report written to {QUALITY_REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
