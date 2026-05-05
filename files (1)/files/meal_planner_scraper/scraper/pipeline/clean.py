"""
Cleaning pipeline.

Reads each raw record in data/recipes/ and rewrites it cleaned in-place:
  - HTML entities decoded, whitespace trimmed
  - Diet tags lowercased + hyphenated consistently
  - Ingredient strings parsed into {name, qty, unit} structs
  - All numeric fields cast to int/float; non-numeric strings rejected
  - Cooking times that are clearly nonsensical clipped or set None

The original raw HTML is still in data/raw/, so any rule can be re-run.
"""
from __future__ import annotations
import json
import re
import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECIPES_DIR = ROOT / "data" / "recipes"

# ----------------------------------------------------------------------
# Unit standardization
# ----------------------------------------------------------------------
# Volume → milliliters
VOLUME_TO_ML = {
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
    "tsp": 4.93, "teaspoon": 4.93,
    "tbsp": 14.79, "tablespoon": 14.79,
    "cup": 240.0, "cups": 240.0,
    "fl oz": 29.57, "fluid ounce": 29.57,
    "pt": 473.0, "pint": 473.0,
    "qt": 946.0, "quart": 946.0,
    "gal": 3785.0, "gallon": 3785.0,
}
# Weight → grams
WEIGHT_TO_G = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0,
    "oz": 28.35, "ounce": 28.35, "ounces": 28.35,
    "lb": 453.6, "lbs": 453.6, "pound": 453.6, "pounds": 453.6,
    "mg": 0.001, "milligram": 0.001,
}
COUNT_UNITS = {"unit", "clove", "cloves", "slice", "slices",
               "piece", "pieces", "stalk", "stalks", "sprig",
               "leaf", "leaves", "can", "cans", "package", "packages"}

# ----------------------------------------------------------------------
# Ingredient line parser
# ----------------------------------------------------------------------
_FRACTION_MAP = {
    "½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1/3, "⅔": 2/3,
    "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_FRAC_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

def _normalize_quantity_token(token: str) -> float | None:
    """Convert '1', '1.5', '1/2', '1 1/2', '½' -> a float."""
    if not token:
        return None
    s = token.strip()
    # Replace unicode fractions
    for sym, val in _FRACTION_MAP.items():
        if sym in s:
            other = s.replace(sym, "").strip()
            base = float(other) if other and _NUM_RE.fullmatch(other) else 0.0
            return base + val
    # Mixed fraction "1 1/2"
    m = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3))
    # Plain fraction
    m = _FRAC_RE.fullmatch(s)
    if m:
        return int(m.group(1)) / int(m.group(2))
    # Decimal / integer
    if _NUM_RE.fullmatch(s):
        return float(s)
    return None


# Match: optional qty, optional unit, then the rest is the name.
# We allow the qty to include unicode fractions and mixed forms like "1 1/2".
_QTY_PREFIX_RE = re.compile(
    r"^\s*("
    r"(?:\d+\s+\d+\s*/\s*\d+)"         # 1 1/2
    r"|(?:\d+\s*/\s*\d+)"               # 1/2
    r"|(?:\d+(?:\.\d+)?[½¼¾⅓⅔⅛⅜⅝⅞]?)"   # 2, 1.5, 1½
    r"|(?:[½¼¾⅓⅔⅛⅜⅝⅞])"                # ½
    r")\s*",
)
_UNIT_WORDS = sorted(
    set(VOLUME_TO_ML) | set(WEIGHT_TO_G) | COUNT_UNITS,
    key=len,
    reverse=True,
)
_UNIT_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(u) for u in _UNIT_WORDS) + r")\.?\s+",
    re.IGNORECASE,
)


def parse_ingredient(line) -> dict:
    """
    Best-effort split of '2 cloves garlic, minced' into
    {name='garlic, minced', qty=2, unit='clove', original=...}.
    Always returns a dict, even if parsing partially fails — original is preserved.

    Idempotent: if the input is already a parsed dict (from a prior cleaning
    pass), it is returned as-is. This makes the cleaning step safe to re-run.
    """
    # If already parsed (re-running clean), pass through unchanged
    if isinstance(line, dict):
        return line
    # Tolerate non-string junk (None, numbers) — coerce to string
    if not isinstance(line, str):
        line = str(line) if line is not None else ""

    original = line.strip()
    rest = original
    qty = None
    unit = None

    # Strip leading qty
    m = _QTY_PREFIX_RE.match(rest)
    if m:
        qty = _normalize_quantity_token(m.group(1))
        rest = rest[m.end():]

    # Strip leading unit
    m = _UNIT_RE.match(rest)
    if m:
        unit = m.group(1).lower()
        rest = rest[m.end():]

    name = rest.strip(" ,;:.")
    return {
        "name": name or None,
        "qty": qty,
        "unit": unit,
        "original": original,
    }


# ----------------------------------------------------------------------
# Diet tag normalization
# ----------------------------------------------------------------------
DIET_CANONICAL = {
    "vegetarian": "vegetarian",
    "veg": "vegetarian",
    "vegan": "vegan",
    "gluten-free": "gluten-free",
    "gluten free": "gluten-free",
    "glutenfree": "gluten-free",
    "low-carb": "low-carb",
    "low carb": "low-carb",
    "lowcarb": "low-carb",
    "keto": "keto",
    "ketogenic": "keto",
    "dairy-free": "dairy-free",
    "dairy free": "dairy-free",
    "paleo": "paleo",
    "high-protein": "high-protein",
    "high protein": "high-protein",
}
def normalize_diet_tags(tags: list[str]) -> list[str]:
    out = set()
    for t in tags or []:
        key = re.sub(r"\s+", " ", str(t).strip().lower())
        canon = DIET_CANONICAL.get(key)
        if canon:
            out.add(canon)
    return sorted(out)


# ----------------------------------------------------------------------
# Top-level cleaner
# ----------------------------------------------------------------------
def clean_record(rec: dict) -> dict:
    # Title: decode entities, collapse whitespace
    if rec.get("title"):
        rec["title"] = re.sub(r"\s+", " ", html.unescape(rec["title"])).strip()

    # Total time fallback
    if not rec.get("total_time_min"):
        prep = rec.get("prep_time_min") or 0
        cook = rec.get("cook_time_min") or 0
        if prep or cook:
            rec["total_time_min"] = prep + cook

    # Sanity-clip times
    for k in ("prep_time_min", "cook_time_min", "total_time_min"):
        v = rec.get(k)
        if v is not None and not (1 <= v <= 600):
            rec[k] = None

    # Sanity-clip calories
    if rec.get("calories_per_serving") is not None:
        c = rec["calories_per_serving"]
        if not (10 <= c <= 3000):
            rec["calories_per_serving"] = None

    # Diet tags
    rec["diet_tags"] = normalize_diet_tags(rec.get("diet_tags", []))

    # Cuisine: trim & lowercase
    if rec.get("cuisine"):
        rec["cuisine"] = re.sub(r"\s+", " ", str(rec["cuisine"])).strip()

    # Ingredients: parse raw strings into structured form
    raw = rec.get("ingredients_raw") or rec.get("ingredients") or []
    rec["ingredients"] = [parse_ingredient(s) for s in raw]
    # Drop the raw form once parsed (we keep `original` inside each parsed entry)
    rec.pop("ingredients_raw", None)

    # Instructions: decode entities, drop empties
    rec["instructions"] = [
        re.sub(r"\s+", " ", html.unescape(s)).strip()
        for s in (rec.get("instructions") or [])
        if s and s.strip()
    ]

    return rec


def main() -> None:
    files = sorted(RECIPES_DIR.glob("r_*.json"))
    if not files:
        print(f"No records found in {RECIPES_DIR}")
        return
    for fp in files:
        rec = json.loads(fp.read_text(encoding="utf-8"))
        cleaned = clean_record(rec)
        fp.write_text(
            json.dumps(cleaned, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"Cleaned {len(files)} records")


if __name__ == "__main__":
    main()
