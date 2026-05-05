"""
Multi-page recipe crawler.

Two-stage crawl:
  Stage 1: walk listing/sitemap pages and collect recipe URLs.
  Stage 2: visit each recipe URL, extract structured fields, persist.

Politeness:
  - robots.txt is consulted before every URL.
  - 2-second sleep between requests (overridable via --delay).
  - Bounded by --pages and --max-recipes hard caps.
  - HTML cached in data/raw so re-parsing is free.

Run:
    python -m crawler.crawl --pages 25 --max-recipes 500
    python -m crawler.crawl --source bbc --pages 10
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .robots import RobotsGuard, USER_AGENT
from .parse import extract_recipe

# ---- Paths ------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
RECIPES_DIR = DATA_DIR / "recipes"
INDEX_PATH = DATA_DIR / "index.json"

for d in (RAW_DIR, RECIPES_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---- Source registry --------------------------------------------------
# Each "source" defines:
#   listing_url(page) -> URL to fetch for a given listing page
#   recipe_link_filter(href) -> True if href looks like a recipe detail page
SOURCES = {
    "allrecipes": {
        "name": "AllRecipes",
        "host": "www.allrecipes.com",
        "listing_url": (
            lambda page: f"https://www.allrecipes.com/recipes-a-z-6735880?page={page}"
            if page > 1 else "https://www.allrecipes.com/recipes-a-z-6735880"
        ),
        "recipe_link_filter": lambda href: (
            href and "/recipe/" in href and href.startswith("https://www.allrecipes.com/")
        ),
    },
    "bbc": {
        "name": "BBC Food",
        "host": "www.bbc.co.uk",
        "listing_url": (
            lambda page: f"https://www.bbc.co.uk/food/recipes/a-z/all/{page}"
        ),
        "recipe_link_filter": lambda href: (
            href and "/food/recipes/" in href
            # Detail pages have a slug after /recipes/, not /a-z/ or /collections/
            and "/a-z/" not in href and "/collections/" not in href
        ),
    },
    "foodnetwork": {
        "name": "Food Network",
        "host": "www.foodnetwork.com",
        "listing_url": (
            lambda page: f"https://www.foodnetwork.com/recipes/recipes-a-z/a/p/{page}"
        ),
        "recipe_link_filter": lambda href: (
            href and "/recipes/" in href and "-recipe-" in href.lower()
        ),
    },
}


# ---- HTTP fetch with cache --------------------------------------------
def _cache_path(url: str) -> Path:
    """Stable path under data/raw based on the URL hash."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    host = urllib.parse.urlparse(url).netloc.replace(":", "_")
    return RAW_DIR / host / f"{digest}.html"


def fetch(
    url: str,
    session: requests.Session,
    guard: RobotsGuard,
    delay: float = 2.0,
    use_cache: bool = True,
) -> str | None:
    """
    Fetch a URL with politeness checks.
    Returns the HTML body, or None if disallowed / errored.
    """
    if not guard.can_fetch(url):
        print(f"[skip] robots.txt disallows {url}")
        return None

    cache = _cache_path(url)
    if use_cache and cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")

    cache.parent.mkdir(parents=True, exist_ok=True)

    try:
        resp = session.get(url, timeout=20)
    except requests.RequestException as e:
        print(f"[err ] {url}: {e}")
        return None
    finally:
        # Polite delay AFTER the network call, regardless of outcome
        time.sleep(delay)

    if resp.status_code != 200:
        print(f"[http] {resp.status_code} {url}")
        return None

    cache.write_text(resp.text, encoding="utf-8")
    return resp.text


# ---- Listing page → recipe URLs ---------------------------------------
def collect_recipe_urls(html: str, source_key: str) -> list[str]:
    """Pull all recipe-detail URLs out of a listing page."""
    soup = BeautifulSoup(html, "lxml")
    is_recipe = SOURCES[source_key]["recipe_link_filter"]
    urls = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Make absolute if relative
        if href.startswith("/"):
            host = SOURCES[source_key]["host"]
            href = f"https://{host}{href}"
        if is_recipe(href) and href not in seen:
            seen.add(href)
            urls.append(href)
    return urls


# ---- Index management --------------------------------------------------
def load_index() -> dict:
    if INDEX_PATH.exists():
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return {"records": {}}


def save_index(index: dict) -> None:
    INDEX_PATH.write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def next_id(index: dict) -> str:
    n = len(index["records"]) + 1
    return f"r_{n:05d}"


# ---- Crawl orchestration -----------------------------------------------
def crawl(
    sources: list[str],
    pages_per_source: int,
    max_recipes: int,
    delay: float,
) -> None:
    guard = RobotsGuard()
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept-Language"] = "en"

    index = load_index()
    saved_count = sum(1 for _ in index["records"])
    print(f"[init] {saved_count} records already in index")

    for source_key in sources:
        if saved_count >= max_recipes:
            break
        cfg = SOURCES[source_key]
        print(f"\n=== {cfg['name']} ===")

        recipe_urls = []
        # Stage 1: listing pages
        for page in range(1, pages_per_source + 1):
            listing_url = cfg["listing_url"](page)
            print(f"[list] page {page}: {listing_url}")
            html = fetch(listing_url, session, guard, delay=delay)
            if not html:
                continue
            urls = collect_recipe_urls(html, source_key)
            print(f"       found {len(urls)} recipe links")
            recipe_urls.extend(urls)
            if len(recipe_urls) >= max_recipes - saved_count:
                break

        # De-dup while preserving order
        seen = set()
        recipe_urls = [u for u in recipe_urls if not (u in seen or seen.add(u))]

        # Drop any URLs we have already saved
        already = {rec["source_url"] for rec in index["records"].values()}
        recipe_urls = [u for u in recipe_urls if u not in already]

        print(f"[plan] {len(recipe_urls)} new recipes to fetch from {cfg['name']}")

        # Stage 2: recipe pages
        for url in recipe_urls:
            if saved_count >= max_recipes:
                print("[stop] hit --max-recipes cap")
                break
            html = fetch(url, session, guard, delay=delay)
            if not html:
                continue
            record = extract_recipe(html, url)
            if record is None:
                print(f"[miss] no recipe found at {url}")
                continue

            rid = next_id(index)
            record["id"] = rid
            record["schema_version"] = 1
            record["source"] = source_key

            out = RECIPES_DIR / f"{rid}.json"
            out.write_text(
                json.dumps(record, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            index["records"][rid] = {
                "id": rid,
                "title": record["title"],
                "source": source_key,
                "source_url": url,
                "file": str(out.relative_to(ROOT)),
            }
            save_index(index)
            saved_count += 1
            print(f"[ok  ] {rid} {record['title']!r}  ({saved_count}/{max_recipes})")


# ---- CLI --------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Polite multi-page recipe crawler")
    p.add_argument(
        "--source",
        action="append",
        choices=list(SOURCES.keys()),
        help="Restrict to one or more sources (default: all)",
    )
    p.add_argument("--pages", type=int, default=10,
                   help="Listing pages per source (default 10)")
    p.add_argument("--max-recipes", type=int, default=500,
                   help="Hard cap on total recipes saved (default 500)")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Seconds to sleep between requests (default 2.0)")
    args = p.parse_args()

    sources = args.source or list(SOURCES.keys())
    if args.delay < 1.0:
        print("Refusing --delay < 1.0 (be polite)")
        sys.exit(2)

    crawl(sources, args.pages, args.max_recipes, args.delay)


if __name__ == "__main__":
    main()
