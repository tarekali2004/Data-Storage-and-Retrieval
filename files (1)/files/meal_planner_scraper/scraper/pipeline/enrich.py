"""
Diet-tag enrichment.

Some recipes don't publish suitableForDiet. We can often infer it from
the ingredient list with simple keyword rules.

Conservative: we only ADD tags, never remove. Inferred tags are flagged
with a parallel `diet_tags_inferred` list so downstream code knows the
provenance.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPES_DIR = ROOT / "data" / "recipes"

MEAT_KEYWORDS = {
    # Mammals
    "chicken", "beef", "steak", "flank", "sirloin", "brisket", "ribeye",
    "pork", "lamb", "veal", "bacon", "ham", "prosciutto", "pancetta",
    "sausage", "chorizo", "salami", "pepperoni", "meatball",
    # Poultry
    "turkey", "duck", "goose", "quail",
    # Fish & seafood
    "anchovy", "anchovies", "fish", "salmon", "tuna", "cod", "haddock",
    "trout", "mackerel", "sardine", "sardines", "halibut",
    "shrimp", "prawn", "prawns", "crab", "lobster",
    "scallop", "scallops", "mussel", "mussels", "clam", "clams",
    "squid", "calamari", "octopus",
    # Animal-derived
    "gelatin", "lard",
}
DAIRY_KEYWORDS = {
    "milk", "butter", "cheese", "cream", "yogurt", "yoghurt",
    "ghee", "buttermilk", "whey", "casein", "curd", "paneer",
    "ricotta", "mozzarella", "cheddar", "parmesan", "feta",
}
EGG_KEYWORDS = {"egg", "eggs"}
GLUTEN_KEYWORDS = {
    "wheat", "flour", "bread", "pasta", "noodle", "noodles",
    "couscous", "barley", "rye", "spelt", "semolina", "bulgur",
    "soy sauce",  # most contains wheat
}
HIGH_CARB_KEYWORDS = {
    "rice", "pasta", "noodle", "bread", "potato", "sugar", "honey",
    "flour", "tortilla", "corn", "oat", "oats",
}


def _ingredient_text(rec: dict) -> str:
    """Concatenate every ingredient name into one lowercase string for keyword matching."""
    parts = []
    for ing in rec.get("ingredients") or []:
        if isinstance(ing, dict):
            parts.append(ing.get("name") or ing.get("original") or "")
        else:
            parts.append(str(ing))
    return " | ".join(parts).lower()


def _has_any(text: str, keywords: set[str]) -> bool:
    """
    Word-boundary keyword search to avoid false positives like
    'creamy' matching 'cream' incorrectly. We escape multi-word keywords
    and rely on \\b for whole-word matching.
    """
    for kw in keywords:
        # Multi-word keywords need a slightly different approach
        if " " in kw:
            if kw in text:
                return True
        else:
            if re.search(rf"\b{re.escape(kw)}\b", text):
                return True
    return False


def infer_diet_tags(rec: dict) -> list[str]:
    text = _ingredient_text(rec)
    inferred: set[str] = set()

    has_meat = _has_any(text, MEAT_KEYWORDS)
    has_dairy = _has_any(text, DAIRY_KEYWORDS)
    has_egg = _has_any(text, EGG_KEYWORDS)
    has_gluten = _has_any(text, GLUTEN_KEYWORDS)

    if not has_meat:
        inferred.add("vegetarian")
        if not has_dairy and not has_egg:
            inferred.add("vegan")
    if not has_gluten:
        inferred.add("gluten-free")
    if not has_dairy:
        inferred.add("dairy-free")
    # High-protein heuristic: macros tell us better, fall back to keyword
    macros = rec.get("macros") or {}
    protein_g = macros.get("protein_g")
    if protein_g and protein_g >= 25:
        inferred.add("high-protein")

    return sorted(inferred)


def enrich(rec: dict) -> dict:
    existing = set(rec.get("diet_tags") or [])
    inferred = set(infer_diet_tags(rec))
    new_tags = inferred - existing
    if new_tags:
        rec["diet_tags"] = sorted(existing | new_tags)
        rec["diet_tags_inferred"] = sorted(new_tags)
    else:
        rec.setdefault("diet_tags_inferred", [])
    return rec


def main() -> None:
    files = sorted(RECIPES_DIR.glob("r_*.json"))
    if not files:
        print(f"No records found in {RECIPES_DIR}")
        return
    enriched_count = 0
    for fp in files:
        rec = json.loads(fp.read_text(encoding="utf-8"))
        before = len(rec.get("diet_tags") or [])
        rec = enrich(rec)
        after = len(rec.get("diet_tags") or [])
        if after > before:
            enriched_count += 1
        fp.write_text(
            json.dumps(rec, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"Enriched {enriched_count}/{len(files)} records with inferred diet tags")


if __name__ == "__main__":
    main()
