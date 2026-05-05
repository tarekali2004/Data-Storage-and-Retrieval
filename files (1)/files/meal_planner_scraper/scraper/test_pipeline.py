"""
Smoke test for the full pipeline.

Generates a small set of realistic synthetic recipe pages with proper
JSON-LD markup, runs them through extract -> clean -> enrich -> quality
-> EDA, and finally exercises the CLI scoring engine.

This validates that every module works end-to-end without hitting the
real network (which would take ~25 minutes).
"""
from __future__ import annotations
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from crawler.parse import extract_recipe
from pipeline.clean import clean_record
from pipeline.enrich import enrich, infer_diet_tags
from pipeline import quality as q
from analysis import eda
from app.cli import passes_hard_constraints, score_recipe

# ----------------------------------------------------------------------
# Synthetic recipe pages — JSON-LD wrapped in realistic HTML noise
# ----------------------------------------------------------------------
def make_page(recipe_jsonld: dict) -> str:
    return f"""<!DOCTYPE html>
<html><head>
<title>{recipe_jsonld['name']} | Recipe Site</title>
<script type="application/ld+json">{json.dumps(recipe_jsonld)}</script>
</head><body>
<header><nav>Home | Recipes | Login</nav></header>
<main>
  <h1>{recipe_jsonld['name']}</h1>
  <p>Lots of ad copy and SEO filler here that the scraper must ignore...</p>
  <div class="comments">user comments, social share buttons, ...</div>
</main>
<footer>(c) Recipe Site 2026</footer>
</body></html>"""


SAMPLE_RECIPES = [
    {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Lemon Garlic Chicken",
        "recipeCuisine": "Mediterranean",
        "prepTime": "PT15M",
        "cookTime": "PT25M",
        "recipeYield": "4 servings",
        "nutrition": {"@type": "NutritionInformation",
                      "calories": "420 kcal", "proteinContent": "38 g",
                      "carbohydrateContent": "12 g", "fatContent": "24 g"},
        "suitableForDiet": "https://schema.org/GlutenFreeDiet",
        "recipeIngredient": ["500g chicken breast", "2 lemons, juiced",
                             "4 garlic cloves, minced", "2 tbsp olive oil",
                             "1 tsp salt", "½ tsp black pepper"],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": "Marinate chicken with lemon and garlic for 20 min"},
            {"@type": "HowToStep", "text": "Sear in pan 6 min per side until 75°C internal"},
            {"@type": "HowToStep", "text": "Rest 5 min and slice"},
        ],
    },
    {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Chickpea Tikka Masala",
        "recipeCuisine": "Indian",
        "prepTime": "PT10M",
        "cookTime": "PT25M",
        "recipeYield": "4",
        "nutrition": {"@type": "NutritionInformation", "calories": "510 calories",
                      "proteinContent": "16 g", "carbohydrateContent": "62 g",
                      "fatContent": "18 g"},
        "keywords": "vegetarian, vegan, weeknight",
        "recipeIngredient": ["2 cans chickpeas, drained", "1 large onion, diced",
                             "3 garlic cloves", "1 tbsp grated ginger",
                             "400g crushed tomatoes", "2 tbsp tomato paste",
                             "2 tsp garam masala", "1 tsp cumin",
                             "200ml coconut milk", "salt to taste"],
        "recipeInstructions": "Sauté onion. Add garlic, ginger, spices. Stir in tomatoes and chickpeas. Simmer 15 min. Finish with coconut milk.",
    },
    {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Beef & Broccoli Stir-fry",
        "recipeCuisine": "Chinese",
        "totalTime": "PT25M",
        "recipeYield": "3 servings",
        "nutrition": {"@type": "NutritionInformation", "calories": "480",
                      "proteinContent": "32 g", "carbohydrateContent": "28 g",
                      "fatContent": "26 g"},
        "recipeIngredient": ["400g flank steak, sliced thin", "300g broccoli florets",
                             "3 tbsp soy sauce", "1 tbsp oyster sauce",
                             "2 garlic cloves, minced", "1 tsp sesame oil",
                             "2 tbsp vegetable oil", "1 tsp cornstarch"],
        "recipeInstructions": ["Mix sauce ingredients.", "Sear beef in hot oil.",
                               "Add broccoli, stir-fry 3 min.", "Pour sauce, toss 1 min."],
    },
    {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Caprese Quinoa Bowl",
        "recipeCuisine": "Italian",
        "totalTime": "PT18M",
        "recipeYield": "2",
        "nutrition": {"@type": "NutritionInformation", "calories": "420",
                      "proteinContent": "18 g", "carbohydrateContent": "44 g",
                      "fatContent": "20 g"},
        "suitableForDiet": ["https://schema.org/VegetarianDiet",
                            "https://schema.org/GlutenFreeDiet"],
        "recipeIngredient": ["1 cup quinoa, cooked", "200g cherry tomatoes",
                             "150g fresh mozzarella balls", "1 handful basil",
                             "2 tbsp olive oil", "1 tbsp balsamic vinegar",
                             "salt and black pepper to taste"],
        "recipeInstructions": ["Cook quinoa per package directions.",
                               "Combine all ingredients in a bowl.",
                               "Drizzle with oil and vinegar."],
    },
    {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Spinach & Feta Pasta",
        "recipeCuisine": "Mediterranean",
        "prepTime": "PT5M",
        "cookTime": "PT17M",
        "recipeYield": "4",
        "nutrition": {"@type": "NutritionInformation", "calories": "480",
                      "proteinContent": "18 g", "carbohydrateContent": "62 g",
                      "fatContent": "16 g"},
        "keywords": "vegetarian",
        "recipeIngredient": ["350g pasta", "200g baby spinach",
                             "150g feta cheese, crumbled", "3 garlic cloves",
                             "3 tbsp olive oil", "1 lemon, zested", "salt"],
        "recipeInstructions": ["Boil pasta.", "Sauté garlic in oil.",
                               "Add spinach, wilt.", "Toss with pasta and feta."],
    },
    # An edge case: missing nutrition (we'll impute later)
    {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Simple Tomato Soup",
        "recipeCuisine": "Italian",
        "totalTime": "PT30M",
        "recipeYield": "4",
        "recipeIngredient": ["800g canned tomatoes", "1 onion", "2 garlic cloves",
                             "2 tbsp olive oil", "1 tsp salt", "basil"],
        "recipeInstructions": "Sauté onion and garlic. Add tomatoes. Simmer 20 min. Blend.",
    },
    # Duplicate of the first one with a slightly different URL — quality stage should catch it
    {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": "Lemon Garlic Chicken",
        "recipeCuisine": "Mediterranean",
        "prepTime": "PT15M",
        "cookTime": "PT25M",
        "recipeYield": "4",
        "recipeIngredient": ["500g chicken breast", "2 lemons", "4 garlic cloves",
                             "2 tbsp olive oil", "salt", "pepper"],
        "recipeInstructions": "Marinate. Sear. Rest. Slice.",
    },
]


def reset_data_dirs():
    """Wipe data/recipes and data/raw for a clean test."""
    data = ROOT / "data"
    for sub in ("recipes", "raw", "plots"):
        d = data / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    idx = data / "index.json"
    if idx.exists():
        idx.unlink()
    qr = data / "quality_report.json"
    if qr.exists():
        qr.unlink()


def populate_corpus():
    """Run the extractor over each synthetic page and save raw JSON records."""
    out_dir = ROOT / "data" / "recipes"
    saved = 0
    for i, recipe_jsonld in enumerate(SAMPLE_RECIPES, 1):
        html = make_page(recipe_jsonld)
        url = f"https://example.com/recipe/{i}"
        record = extract_recipe(html, url)
        assert record is not None, f"extractor failed on sample {i}"
        rid = f"r_{i:05d}"
        record["id"] = rid
        record["schema_version"] = 1
        record["source"] = "synthetic"
        (out_dir / f"{rid}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        saved += 1
    # Build a tiny index too
    (ROOT / "data" / "index.json").write_text(json.dumps({
        "records": {
            f"r_{i:05d}": {"id": f"r_{i:05d}",
                           "title": SAMPLE_RECIPES[i-1]["name"],
                           "source": "synthetic",
                           "source_url": f"https://example.com/recipe/{i}",
                           "file": f"data/recipes/r_{i:05d}.json"}
            for i in range(1, len(SAMPLE_RECIPES) + 1)
        }
    }, indent=2), encoding="utf-8")
    print(f"[extract] {saved} synthetic recipes extracted")


def run_clean_step():
    from pipeline.clean import main as clean_main
    clean_main()


def run_enrich_step():
    from pipeline.enrich import main as enrich_main
    enrich_main()


def run_quality_step():
    q.main()


def run_eda_step():
    eda.main()


def test_cli_engine():
    """Exercise the rule engine without interactive input."""
    from app.cli import load_corpus
    corpus = load_corpus()
    prefs = {
        "diet": "vegetarian",
        "max_calories": 550,
        "max_time": 30,
        "avoid": ["mushroom"],
        "pantry": ["tomato", "garlic", "olive oil"],
        "top_n": 3,
    }
    candidates = []
    for rec in corpus:
        ok, trace = passes_hard_constraints(rec, prefs)
        if ok:
            candidates.append((rec, trace))
    ranked = sorted(
        ((rec, trace, score_recipe(rec, prefs)) for rec, trace in candidates),
        key=lambda x: x[2], reverse=True
    )
    print(f"\n[cli ] vegetarian + ≤550 cal + ≤30 min, avoid=mushroom")
    print(f"[cli ] {len(corpus)} → {len(candidates)} candidates")
    for i, (rec, trace, score) in enumerate(ranked[:3], 1):
        cal = rec.get("calories_per_serving")
        t = rec.get("total_time_min")
        print(f"   {i}. {rec['title']}  ({cal} cal · {t} min, score={score:.2f})")
    return ranked


if __name__ == "__main__":
    print("=" * 60)
    print("PIPELINE SMOKE TEST")
    print("=" * 60)

    print("\n[1/6] Reset data directories")
    reset_data_dirs()

    print("\n[2/6] Extract from synthetic pages (parse.py)")
    populate_corpus()

    print("\n[3/6] Clean (pipeline/clean.py)")
    run_clean_step()

    print("\n[4/6] Enrich diet tags (pipeline/enrich.py)")
    run_enrich_step()

    print("\n[5/6] Quality (pipeline/quality.py)")
    run_quality_step()

    print("\n[6/6] EDA (analysis/eda.py)")
    run_eda_step()

    print("\n[cli ] Test rule engine")
    ranked = test_cli_engine()

    # Final assertions
    print("\n" + "=" * 60)
    print("ASSERTIONS")
    print("=" * 60)
    files = list((ROOT / "data" / "recipes").glob("r_*.json"))
    print(f"  recipes saved: {len(files)}")
    assert len(files) >= 5, f"expected at least 5 records after quality, got {len(files)}"
    # Open one record to confirm shape
    sample = json.loads(files[0].read_text(encoding="utf-8"))
    for required_field in ("id", "title", "ingredients", "instructions", "diet_tags"):
        assert required_field in sample, f"missing field {required_field}"
    print(f"  schema check: ok (sample={sample['title']!r})")
    # Confirm cleaning actually parsed ingredients into structs
    first_ing = sample["ingredients"][0]
    assert isinstance(first_ing, dict), "ingredients should be parsed dicts after clean"
    assert "qty" in first_ing or "name" in first_ing
    print(f"  ingredient parsed: {first_ing}")
    # Confirm CLI returned at least one match
    assert len(ranked) >= 1, "CLI should have returned at least one match"
    print(f"  cli matches: {len(ranked)}")

    print("\n✅  ALL CHECKS PASSED")
