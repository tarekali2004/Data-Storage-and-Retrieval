"""
Exploratory Data Analysis.

Prints corpus-level statistics and saves three plots to data/plots/:
  - top_ingredients.png     (bar chart, most common ingredients)
  - cuisine_distribution.png (pie/donut chart)
  - cooking_time_hist.png   (histogram of total cooking time)

Run after the cleaning + quality pipeline.
"""
from __future__ import annotations
import json
import statistics
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
RECIPES_DIR = ROOT / "data" / "recipes"
PLOTS_DIR = ROOT / "data" / "plots"
PLOTS_DIR.mkdir(parents=True, exist_ok=True)

# Project palette (matches the slides)
C_PRIMARY = "#2C5F2D"
C_SECONDARY = "#97BC62"
C_ACCENT = "#E07A5F"
PIE_COLORS = ["#2C5F2D", "#97BC62", "#E07A5F", "#5B7553",
              "#8AA899", "#C9A66B", "#B8B8AA"]


def _load_records() -> list[dict]:
    return [
        json.loads(fp.read_text(encoding="utf-8"))
        for fp in sorted(RECIPES_DIR.glob("r_*.json"))
    ]


def _ingredient_names(rec: dict) -> list[str]:
    out = []
    for ing in rec.get("ingredients") or []:
        if isinstance(ing, dict):
            name = (ing.get("name") or "").strip().lower()
        else:
            name = str(ing).strip().lower()
        # First token is usually the head noun (onion, garlic, ...)
        head = name.split(",", 1)[0].split()
        if head:
            out.append(head[-1])  # take last word: "olive oil" -> "oil"; we'll fix
    return out


def _ingredient_head(rec: dict) -> list[str]:
    """Better: take the most informative noun. We use simple heuristics."""
    keep = []
    for ing in rec.get("ingredients") or []:
        if isinstance(ing, dict):
            name = (ing.get("name") or "").strip().lower()
        else:
            name = str(ing).strip().lower()
        # Strip trailing modifiers after comma
        name = name.split(",", 1)[0]
        # Drop generic adjectives
        for adj in ("fresh", "dried", "ground", "chopped", "minced",
                    "sliced", "small", "medium", "large", "whole",
                    "raw", "cooked", "boneless", "skinless"):
            name = name.replace(f" {adj} ", " ").replace(f"{adj} ", "")
        name = name.strip()
        # Common compound names to keep intact
        if "olive oil" in name:
            keep.append("olive oil"); continue
        if "soy sauce" in name:
            keep.append("soy sauce"); continue
        if "black pepper" in name:
            keep.append("black pepper"); continue
        # Otherwise keep first 1-2 words
        words = name.split()
        if not words:
            continue
        keep.append(" ".join(words[:2]) if len(words) >= 2 else words[0])
    return keep


def plot_top_ingredients(records: list[dict], top_n: int = 8) -> None:
    counter: Counter = Counter()
    for r in records:
        counter.update(set(_ingredient_head(r)))   # set: count distinct per recipe
    top = counter.most_common(top_n)
    if not top:
        return
    labels = [k for k, _ in top][::-1]
    values = [v for _, v in top][::-1]
    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.barh(labels, values, color=C_PRIMARY, edgecolor="white")
    ax.set_title("Most frequent ingredients", fontsize=14, color="#1C2A1D", weight="bold")
    ax.set_xlabel("Number of recipes")
    ax.bar_label(bars, padding=4, color="#1C2A1D")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(PLOTS_DIR / "top_ingredients.png", dpi=150)
    plt.close(fig)


def plot_cuisine_distribution(records: list[dict]) -> None:
    counter: Counter = Counter()
    for r in records:
        c = r.get("cuisine") or "Unknown"
        counter[c] += 1
    if not counter:
        return
    items = counter.most_common(6)
    others = sum(v for _, v in counter.most_common()[6:])
    if others > 0:
        items.append(("Other", others))
    labels = [k for k, _ in items]
    values = [v for _, v in items]

    fig, ax = plt.subplots(figsize=(7, 5))
    wedges, _, autotexts = ax.pie(
        values,
        labels=labels,
        colors=PIE_COLORS[: len(values)],
        autopct="%1.0f%%",
        wedgeprops=dict(width=0.42, edgecolor="white"),
        textprops=dict(color="#1C2A1D"),
    )
    for at in autotexts:
        at.set_color("white")
        at.set_fontweight("bold")
    ax.set_title("Cuisine distribution", fontsize=14, color="#1C2A1D", weight="bold")
    fig.tight_layout()
    fig.savefig(PLOTS_DIR / "cuisine_distribution.png", dpi=150)
    plt.close(fig)


def plot_cooking_time_histogram(records: list[dict]) -> None:
    bins = [0, 15, 30, 45, 60, 90, 600]
    labels = ["≤15", "16–30", "31–45", "46–60", "61–90", "90+"]
    counts = [0] * (len(bins) - 1)
    for r in records:
        t = r.get("total_time_min")
        if t is None:
            continue
        for i in range(len(bins) - 1):
            if bins[i] < t <= bins[i + 1]:
                counts[i] += 1
                break
    # Edge-case: t == 0 won't be counted; that's intentional (filters bad data)
    if t := bins[0]:
        pass

    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.bar(labels, counts, color=C_SECONDARY, edgecolor="white")
    ax.set_title("Cooking time distribution (minutes)",
                 fontsize=14, color="#1C2A1D", weight="bold")
    ax.set_ylabel("Number of recipes")
    ax.bar_label(bars, padding=4, color="#1C2A1D")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(PLOTS_DIR / "cooking_time_hist.png", dpi=150)
    plt.close(fig)


def print_summary(records: list[dict]) -> None:
    n = len(records)
    cuisines = {r.get("cuisine") for r in records if r.get("cuisine")}
    diet_tags = set()
    for r in records:
        diet_tags.update(r.get("diet_tags") or [])
    distinct_ing = set()
    times, calories, ing_counts = [], [], []
    veg_count = 0
    for r in records:
        for ing in _ingredient_head(r):
            distinct_ing.add(ing)
        if r.get("total_time_min"):
            times.append(r["total_time_min"])
        if r.get("calories_per_serving"):
            calories.append(r["calories_per_serving"])
        ing_counts.append(len(r.get("ingredients") or []))
        if "vegetarian" in (r.get("diet_tags") or []):
            veg_count += 1

    print("\n" + "=" * 50)
    print("CORPUS-LEVEL STATISTICS")
    print("=" * 50)
    print(f"  Total recipes:        {n}")
    print(f"  Cuisines covered:     {len(cuisines)}")
    print(f"  Diet tags used:       {len(diet_tags)}")
    print(f"  Distinct ingredients: {len(distinct_ing)}")
    if times:
        print(f"  Mean total time:      {statistics.mean(times):.1f} min")
        print(f"  Median total time:    {statistics.median(times):.0f} min")
    if calories:
        print(f"  Mean calories/srv:    {statistics.mean(calories):.0f}")
        print(f"  Median calories/srv:  {statistics.median(calories):.0f}")
    if ing_counts:
        print(f"  Avg ingredients:      {statistics.mean(ing_counts):.1f} per recipe")
    if n:
        print(f"  Vegetarian share:     {100*veg_count/n:.0f}%")
    print()


def main() -> None:
    records = _load_records()
    if not records:
        print(f"No records in {RECIPES_DIR}")
        return
    print_summary(records)
    plot_top_ingredients(records)
    plot_cuisine_distribution(records)
    plot_cooking_time_histogram(records)
    print(f"Plots saved to {PLOTS_DIR.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
