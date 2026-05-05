# Meal Planning Assistant — Web Scraper

A real, working multi-page web scraper that builds a structured recipe corpus
from public recipe websites. Used as the data-collection layer for the
Information Retrieval Phase 1 project.

## Folder layout

```
meal_planner/
├── crawler/
│   ├── robots.py        robots.txt parser & guard
│   ├── parse.py         JSON-LD + HTML field extractors
│   └── crawl.py         multi-page crawler (entry point)
├── pipeline/
│   ├── clean.py         unit conversion, normalization, type coercion
│   ├── enrich.py        diet-tag inference from ingredient list
│   └── quality.py       missing values, duplicates, outliers
├── analysis/
│   └── eda.py           exploratory plots & corpus statistics
├── app/
│   └── cli.py           rule-based recommendation interface
├── data/
│   ├── raw/             cached HTML pages (re-parse without re-crawling)
│   ├── recipes/         one JSON file per recipe (the corpus)
│   └── index.json       master id → file path index
└── requirements.txt
```

## How to run

```bash
pip install -r requirements.txt

# 1. Crawl ~500 recipes (takes ~25 min at the polite 2-second delay)
python -m crawler.crawl --pages 25 --max-recipes 500

# 2. Clean the raw extractions
python -m pipeline.clean

# 3. Add inferred diet tags
python -m pipeline.enrich

# 4. Run quality checks (handle missing, duplicates, noise)
python -m pipeline.quality

# 5. Generate EDA plots and stats
python -m analysis.eda

# 6. Use the assistant
python -m app.cli
```

## Ethical commitments

- Reads and obeys `robots.txt` before every session
- 2-second sleep between requests (twice the typical Crawl-delay)
- Identifies itself with a real `User-Agent` containing a contact
- Bounded pagination (`--pages` and `--max-recipes` are hard caps)
- No personal data collected (no users, comments, or auth-walled pages)
- Source URL stored with every record for full attribution

## Why JSON-LD?

Modern recipe sites (AllRecipes, BBC Food, Food Network, NYT Cooking, etc.)
embed structured `schema.org/Recipe` data inside a
`<script type="application/ld+json">` tag for SEO. This is the most reliable
and least brittle way to extract recipes — far more stable than CSS selectors
that break whenever the site redesigns. We use JSON-LD as the primary path and
fall back to CSS selectors only when JSON-LD is missing.
