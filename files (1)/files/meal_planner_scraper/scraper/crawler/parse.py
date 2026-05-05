"""
Recipe field extraction.

Strategy:
  1. Look for a JSON-LD <script type="application/ld+json"> block with a
     schema.org/Recipe entity. This is what most modern recipe sites
     publish for SEO and is the most stable extraction surface.
  2. Fall back to CSS selectors against known site templates (AllRecipes,
     BBC Food, Food Network) when JSON-LD is missing.

Returns a dict shaped like:
{
    "title": str,
    "source_url": str,
    "cuisine": str | None,
    "prep_time_min": int | None,
    "cook_time_min": int | None,
    "total_time_min": int | None,
    "servings": int | None,
    "calories_per_serving": int | None,
    "macros": {"protein_g": ..., "carbs_g": ..., "fat_g": ...},
    "diet_tags": [str, ...],
    "ingredients": [str, ...],         # raw strings; structured later
    "instructions": [str, ...],
    "image_url": str | None,
    "scraped_at": iso-8601 string,
}
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# JSON-LD extraction (primary path)
# ----------------------------------------------------------------------
def _iter_jsonld_objects(soup: BeautifulSoup):
    """Yield every JSON object found in <script type='application/ld+json'> tags."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        # Some pages embed multiple objects in an array, or wrap them in @graph
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some sites have minor issues (trailing commas, control chars).
            # Try a permissive cleanup.
            cleaned = re.sub(r",\s*([\]}])", r"\1", raw)
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                continue
        # Normalize to a flat list of dicts
        if isinstance(data, list):
            for item in data:
                yield from _walk_jsonld(item)
        else:
            yield from _walk_jsonld(data)


def _walk_jsonld(node):
    if isinstance(node, dict):
        if "@graph" in node and isinstance(node["@graph"], list):
            for item in node["@graph"]:
                yield from _walk_jsonld(item)
        else:
            yield node
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def _is_recipe(obj: dict) -> bool:
    t = obj.get("@type")
    if isinstance(t, str):
        return t == "Recipe"
    if isinstance(t, list):
        return "Recipe" in t
    return False


# ----------------------------------------------------------------------
# Value parsers
# ----------------------------------------------------------------------
ISO_DURATION_RE = re.compile(
    r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$"
)
DURATION_TEXT_RE = re.compile(
    r"(\d+)\s*(h|hr|hour|hours|m|min|mins|minute|minutes)", re.IGNORECASE
)


def parse_duration_minutes(value) -> int | None:
    """
    Convert a duration to minutes.
    Accepts ISO-8601 ('PT1H30M'), free text ('1 hour 30 min'), or a number.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    # ISO-8601 first
    m = ISO_DURATION_RE.match(s)
    if m:
        d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
        total = d * 24 * 60 + h * 60 + mi + (1 if sec >= 30 else 0)
        return total or None
    # Free text fallback
    total = 0
    for num, unit in DURATION_TEXT_RE.findall(s):
        n = int(num)
        u = unit.lower()
        if u.startswith("h"):
            total += n * 60
        else:
            total += n
    return total or None


_NUM_RE = re.compile(r"[-+]?\d*\.?\d+")


def parse_int(value) -> int | None:
    """Pull the first integer out of a string. '4 servings' -> 4, '420 kcal' -> 420."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = _NUM_RE.search(str(value))
    return int(float(m.group())) if m else None


def parse_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _NUM_RE.search(str(value))
    return float(m.group()) if m else None


# ----------------------------------------------------------------------
# Field extractors over a JSON-LD Recipe dict
# ----------------------------------------------------------------------
def _normalize_instruction(item) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, dict):
        if item.get("@type") == "HowToSection":
            # Sections contain itemListElement with HowToSteps inside
            steps = item.get("itemListElement") or []
            joined = " ".join(
                filter(None, (_normalize_instruction(s) for s in steps))
            )
            return joined or None
        # HowToStep
        text = item.get("text") or item.get("name")
        return text.strip() if text else None
    return None


def _normalize_ingredient(item) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, dict):
        # Sometimes structured as PropertyValue
        name = item.get("name")
        value = item.get("value")
        unit = item.get("unitCode") or item.get("unitText")
        parts = [str(p) for p in (value, unit, name) if p]
        return " ".join(parts).strip() or None
    return None


def _extract_diet_tags(recipe: dict) -> list[str]:
    """schema.org has 'suitableForDiet' (URI), plus 'keywords' often contains diet hints."""
    tags = set()
    sfd = recipe.get("suitableForDiet")
    if sfd:
        items = sfd if isinstance(sfd, list) else [sfd]
        for it in items:
            uri = it if isinstance(it, str) else it.get("@id", "")
            slug = uri.rsplit("/", 1)[-1]
            # GlutenFreeDiet -> gluten-free
            slug = re.sub(r"Diet$", "", slug)
            slug = re.sub(r"(?<!^)(?=[A-Z])", "-", slug).lower()
            if slug:
                tags.add(slug)
    # Keywords
    kw = recipe.get("keywords")
    if kw:
        kw_text = kw if isinstance(kw, str) else ", ".join(kw)
        for known in ("vegetarian", "vegan", "gluten-free", "gluten free",
                      "keto", "low-carb", "low carb", "dairy-free",
                      "paleo", "high-protein"):
            if known in kw_text.lower():
                tags.add(known.replace(" ", "-"))
    return sorted(tags)


def _extract_macros(nutrition) -> dict:
    if not isinstance(nutrition, dict):
        return {}
    return {
        "calories": parse_int(nutrition.get("calories")),
        "protein_g": parse_float(nutrition.get("proteinContent")),
        "carbs_g": parse_float(nutrition.get("carbohydrateContent")),
        "fat_g": parse_float(nutrition.get("fatContent")),
        "fiber_g": parse_float(nutrition.get("fiberContent")),
        "sugar_g": parse_float(nutrition.get("sugarContent")),
        "sodium_mg": parse_float(nutrition.get("sodiumContent")),
    }


# ----------------------------------------------------------------------
# Public extractor
# ----------------------------------------------------------------------
def extract_from_jsonld(recipe: dict, source_url: str) -> dict:
    """Map a schema.org/Recipe dict into our internal record schema."""
    instructions_raw = recipe.get("recipeInstructions") or []
    if isinstance(instructions_raw, str):
        instructions_raw = [instructions_raw]
    instructions = [
        s for s in (_normalize_instruction(i) for i in instructions_raw) if s
    ]

    ingredients_raw = recipe.get("recipeIngredient") or recipe.get("ingredients") or []
    if isinstance(ingredients_raw, str):
        ingredients_raw = [ingredients_raw]
    ingredients = [
        s for s in (_normalize_ingredient(i) for i in ingredients_raw) if s
    ]

    macros = _extract_macros(recipe.get("nutrition"))

    # Image can be a string, a list, or an ImageObject dict
    image = recipe.get("image")
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        image = image.get("url")

    return {
        "title": (recipe.get("name") or "").strip() or None,
        "source_url": source_url,
        "cuisine": recipe.get("recipeCuisine"),
        "category": recipe.get("recipeCategory"),
        "prep_time_min": parse_duration_minutes(recipe.get("prepTime")),
        "cook_time_min": parse_duration_minutes(recipe.get("cookTime")),
        "total_time_min": parse_duration_minutes(recipe.get("totalTime")),
        "servings": parse_int(recipe.get("recipeYield")),
        "calories_per_serving": macros.pop("calories", None),
        "macros": {k: v for k, v in macros.items() if v is not None},
        "diet_tags": _extract_diet_tags(recipe),
        "ingredients_raw": ingredients,
        "instructions": instructions,
        "image_url": image,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def extract_recipe(html: str, source_url: str) -> dict | None:
    """
    Top-level extractor. Returns a recipe dict if extraction succeeded,
    or None if the page does not contain a recognizable recipe.
    """
    soup = BeautifulSoup(html, "lxml")

    # Primary path: JSON-LD
    for obj in _iter_jsonld_objects(soup):
        if _is_recipe(obj):
            record = extract_from_jsonld(obj, source_url)
            if record["title"] and record["ingredients_raw"]:
                return record

    # Fallback: extremely conservative microdata sniff
    md_recipe = soup.find(attrs={"itemtype": re.compile(r"schema\.org/Recipe")})
    if md_recipe is not None:
        title_el = md_recipe.find(attrs={"itemprop": "name"})
        ing_els = md_recipe.find_all(attrs={"itemprop": "recipeIngredient"})
        if title_el and ing_els:
            return {
                "title": title_el.get_text(strip=True),
                "source_url": source_url,
                "cuisine": None,
                "category": None,
                "prep_time_min": None,
                "cook_time_min": None,
                "total_time_min": None,
                "servings": None,
                "calories_per_serving": None,
                "macros": {},
                "diet_tags": [],
                "ingredients_raw": [el.get_text(strip=True) for el in ing_els],
                "instructions": [
                    el.get_text(strip=True)
                    for el in md_recipe.find_all(
                        attrs={"itemprop": "recipeInstructions"}
                    )
                ],
                "image_url": None,
                "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

    return None
