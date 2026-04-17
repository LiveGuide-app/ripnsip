"""Property monitor — Tavily scrape + Claude Haiku structured extraction.

Flow:
  1. Tavily /extract fetches clean markdown from each search URL in SEARCH_URLS.
  2. Claude Haiku extracts structured listings from each page's markdown using
     the save_listings tool, filtering to Médoc communes.
  3. Results are deduped by URL and written to properties/data/listings.json
     as a fresh snapshot.

Requires env:
  TAVILY_API_KEY, ANTHROPIC_API_KEY

Run locally:
  pip install -r scripts/requirements.txt
  TAVILY_API_KEY=... ANTHROPIC_API_KEY=... python scripts/property_monitor.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from anthropic import Anthropic
from tavily import TavilyClient

from sources import (
    EXTRACT_SOURCES,
    SEARCH_MAX_RESULTS,
    SEARCH_QUERY,
    SEARCH_SOURCES,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LISTINGS_PATH = REPO_ROOT / "properties" / "data" / "listings.json"

CRITERIA = {
    "region": "Coastal Médoc, Gironde (within ~20 min of the Atlantic)",
    "price_min_eur": 400_000,
    "price_max_eur": 900_000,
    "scoring_max": 100,
    "scoring_components": {
        "size_potential": 25,
        "pool": 15,
        "glamping_land": 15,
        "coast_proximity": 15,
        "character": 15,
        "renovation_state": 15,
    },
    "notes": "Hard filters: coastal Médoc + €400-900k. Everything else is scored 0-100 by Claude Haiku.",
}

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_MARKDOWN_CHARS = 150_000  # Safety cap per page
PRICE_MIN_EUR = 400_000
PRICE_MAX_EUR = 900_000

# Allowed communes — within ~20 min drive of the Atlantic. Match is on
# the LLM's `commune` field (exact, case-insensitive), so spurious hits
# from other Médoc place-names (e.g. "Le Pian-Médoc" south of Bordeaux)
# can no longer slip through via location/title substring.
ALLOWED_COMMUNES = {
    # Direct coast
    "soulac-sur-mer", "grayan-et-l'hôpital", "grayan-et-l'hopital",
    "vendays-montalivet", "naujac-sur-mer", "hourtin", "carcans", "lacanau",
    # Central Médoc, ~20 min to coast
    "lesparre-médoc", "lesparre-medoc",
    "gaillan-en-médoc", "gaillan-en-medoc",
    "saint-vivien-de-médoc", "saint-vivien-de-medoc",
    "queyrac", "vensac",
    "jau-dignac-et-loirac",
    "saint-laurent-médoc", "saint-laurent-medoc",
}

SYSTEM_PROMPT = """You extract French real-estate listings from scraped pages for a boutique surf & wine retreat in coastal Médoc, then SCORE each kept listing's fit.

LISTING TYPE — sales only (vente / for sale / à vendre). SKIP rentals: anything labelled "à louer", "location", "monthly rent", "/locations/" in the URL, or with a price like "€X / mois".

LOCATION (hard filter) — accept ONLY these 14 communes and reject any other:
- Direct coast: Soulac-sur-Mer, Grayan-et-l'Hôpital, Vendays-Montalivet, Naujac-sur-Mer, Hourtin, Carcans, Lacanau.
- Central Médoc (~20 min to coast): Lesparre-Médoc, Gaillan-en-Médoc, Saint-Vivien-de-Médoc, Queyrac, Vensac, Jau-Dignac-et-Loirac, Saint-Laurent-Médoc.

EXPLICITLY REJECT (common confusables): Le Pian-Médoc (south of Bordeaux, NOT coastal), Castelnau-de-Médoc, Macau, Margaux, Pauillac, Saint-Estèphe, Saint-Seurin-de-Cadourne, Moulis-en-Médoc, Listrac-Médoc, Cissac-Médoc, Bégadan, Blaignan, Prignac-en-Médoc, Vertheuil, Saint-Germain-d'Esteuil, Saint-Yzans-de-Médoc, Couquèques, Ordonnac, Valeyrac, Saint-Christoly-Médoc — and the entire rest of Gironde (Bordeaux, Cap Ferret, Bassin d'Arcachon, Libourne, Saint-Émilion, etc.).

Set the `commune` field to the exact commune name only (e.g. "Lesparre-Médoc", not "Lesparre-Médoc, Gironde"). If you cannot identify the exact commune from the listing, skip it — do not guess.

PRICE (hard filter) — €400,000 to €900,000. Skip outside this range. If price isn't stated, keep.

SCORING — for each kept listing, score 0–100 by summing six components (max points bracketed):

- size_potential [25] — closeness to a 12-bedroom retreat.
    25 = ≥10 bed already; 20 = 6-8 bed + obvious conversion potential (attic, gîte, large outbuildings); 12 = 4-5 bed + some expansion; 5 = small/limited; 0 = impossible.
- pool [15] — 15 = existing pool; 8 = land suitable to add one; 0 = no pool, hard to add.
- glamping_land [15] — 15 = ≥1 ha with character/seclusion; 10 = ~5000 m²; 5 = small garden; 0 = no land.
- coast_proximity [15] — 15 = ≤5 km to ocean beach; 10 = ≤10 km; 7 = 10-20 km; 4 = 20+ km.
- character [15] — stone/beams/period features, light, layout. 15 = exceptional; 8 = decent; 3 = bland modern; 0 = charmless.
- renovation_state [15] — 15 = move-in ready; 10 = cosmetic only; 5 = significant works; 0 = gut renovation.

Add a one-line `fit_verdict` (≤120 chars) capturing the headline strength + biggest weakness.

Be honest. Don't inflate scores to please. Calibrate so a hypothetical perfect listing scores ~95; a typical decent match should land 55-75.

Other rules:
- Emit one object per distinct listing.
- `url` must be the absolute URL of the individual listing page — never a search/category/filter page.
- If a numeric field isn't stated, omit it. Do not guess.
- `features` — at most 6 items.
- Mark sold/under-offer listings with `status: "sold"`.
- `image_url` — pick the single best representative image of the property. Look in the candidate image list AND in the page markdown itself (markdown image syntax `![alt](url)` or `<img src=...>` tags). Prefer the hero/cover shot of the listing being described. Skip thumbnails, logos, agent headshots, floor plans, generic agency images, and images that obviously belong to a different listing on the same page. Omit the field if nothing usable applies.

Return everything via the save_listings tool."""

EXTRACTION_TOOL = {
    "name": "save_listings",
    "description": "Save the coastal-Médoc property listings extracted from the page, with fit scores.",
    "input_schema": {
        "type": "object",
        "properties": {
            "listings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "location": {"type": "string", "description": "Full location string as shown on the listing"},
                        "commune": {"type": "string", "description": "Exact commune name only (e.g. 'Lesparre-Médoc'), used for map placement and the hard location filter"},
                        "price_eur": {"type": ["integer", "null"]},
                        "bedrooms": {"type": ["integer", "null"]},
                        "area_m2": {"type": ["integer", "null"], "description": "Building area"},
                        "land_m2": {"type": ["integer", "null"]},
                        "features": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                        "status": {"type": "string", "enum": ["active", "sold", "unknown"]},
                        "url": {"type": "string", "description": "Absolute URL of the individual listing page"},
                        "image_url": {"type": "string", "description": "Best representative image URL from the page; omit if none usable"},
                        "score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Sum of the six component scores"},
                        "score_breakdown": {
                            "type": "object",
                            "properties": {
                                "size_potential": {"type": "integer", "minimum": 0, "maximum": 25},
                                "pool": {"type": "integer", "minimum": 0, "maximum": 15},
                                "glamping_land": {"type": "integer", "minimum": 0, "maximum": 15},
                                "coast_proximity": {"type": "integer", "minimum": 0, "maximum": 15},
                                "character": {"type": "integer", "minimum": 0, "maximum": 15},
                                "renovation_state": {"type": "integer", "minimum": 0, "maximum": 15},
                            },
                            "required": [
                                "size_potential", "pool", "glamping_land",
                                "coast_proximity", "character", "renovation_state",
                            ],
                        },
                        "fit_verdict": {"type": "string", "maxLength": 200},
                        "notes": {"type": "string"},
                    },
                    "required": ["title", "location", "commune", "url", "score", "score_breakdown", "fit_verdict"],
                },
            }
        },
        "required": ["listings"],
    },
}


def main() -> int:
    tavily_key = os.environ.get("TAVILY_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not tavily_key or not anthropic_key:
        print("[fatal] TAVILY_API_KEY and ANTHROPIC_API_KEY must be set", file=sys.stderr)
        return 2

    tavily = TavilyClient(api_key=tavily_key)
    claude = Anthropic(api_key=anthropic_key)

    errors: list[str] = []
    all_listings: list[dict] = []

    # ─── Strategy 1: direct extract on search-results URLs ────────────────
    if EXTRACT_SOURCES:
        urls = [s["url"] for s in EXTRACT_SOURCES]
        site_by_url = {s["url"]: s["site"] for s in EXTRACT_SOURCES}

        try:
            extract_response = tavily.extract(
                urls=urls,
                extract_depth="advanced",
                format="markdown",
                include_images=True,
                timeout=90,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[fatal] tavily.extract failed: {e}", file=sys.stderr)
            return 1

        for failed in extract_response.get("failed_results", []) or []:
            errors.append(f"tavily:{failed.get('url', '?')}: {failed.get('error', 'unknown')}")

        for result in extract_response.get("results", []):
            url = result.get("url", "")
            content = result.get("raw_content") or ""
            images = result.get("images") or []
            site = site_by_url.get(url, urlparse(url).netloc)

            if not content.strip():
                errors.append(f"tavily:{url}: empty content")
                continue

            valid = _extract_and_filter(claude, url, content, images, site, errors)
            all_listings.extend(valid)
            print(f"[extract] {site}: {len(valid)} kept")

    # ─── Strategy 2: search → individual listing pages ────────────────────
    for source in SEARCH_SOURCES:
        site = source["site"]
        domain = source["domain"]

        try:
            search_response = tavily.search(
                query=SEARCH_QUERY,
                include_domains=[domain],
                max_results=SEARCH_MAX_RESULTS,
                search_depth="advanced",
                include_raw_content="markdown",
                include_images=True,
                timeout=60,
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"tavily-search:{site}: {e}")
            print(f"[warn] search failed for {site}: {e}", file=sys.stderr)
            continue

        results = search_response.get("results", []) or []
        # Tavily /search returns images at the top level (one shared list per
        # search), so they can't be safely attributed to any specific result —
        # pass [] and let Haiku pick from the per-result markdown instead.
        kept = 0
        for result in results:
            url = (result.get("url") or "").strip()
            content = result.get("raw_content") or result.get("content") or ""
            if not url or not content.strip():
                continue
            if _is_rental_url(url):
                continue
            valid = _extract_and_filter(claude, url, content, [], site, errors)
            all_listings.extend(valid)
            kept += len(valid)
        print(f"[search]  {site}: {len(results)} candidate URLs → {kept} kept")

    # ─── Dedupe by listing URL, keep highest-scored variant ───────────────
    by_url: dict[str, dict] = {}
    for l in all_listings:
        existing = by_url.get(l["url"])
        if existing is None or (l.get("score") or 0) > (existing.get("score") or 0):
            by_url[l["url"]] = l
    deduped = sorted(by_url.values(), key=lambda x: x.get("score") or 0, reverse=True)

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "criteria": CRITERIA,
        "errors": errors,
        "listings": deduped,
    }

    LISTINGS_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[done] {len(deduped)} listings, {len(errors)} error(s) → {LISTINGS_PATH.relative_to(REPO_ROOT)}")
    return 0


def _extract_and_filter(
    claude: Anthropic,
    source_url: str,
    content: str,
    images: list[str],
    site: str,
    errors: list[str],
) -> list[dict]:
    """Run Claude extraction on one page's markdown and return valid listings."""
    if len(content) > MAX_MARKDOWN_CHARS:
        content = content[:MAX_MARKDOWN_CHARS]

    try:
        raw_listings = _extract_with_claude(claude, source_url, content, images)
    except Exception as e:  # noqa: BLE001
        errors.append(f"claude:{source_url}: {e}")
        print(f"[warn] claude extraction failed for {source_url}: {e}", file=sys.stderr)
        return []

    valid: list[dict] = []
    for l in raw_listings:
        listing_url = (l.get("url") or "").strip()
        if not listing_url or not listing_url.startswith("http"):
            continue
        if _is_rental_url(listing_url):
            continue
        if not _is_coastal_medoc(l):
            continue
        price = l.get("price_eur")
        if isinstance(price, int) and not (PRICE_MIN_EUR <= price <= PRICE_MAX_EUR):
            continue
        l["id"] = "auto-" + hashlib.sha1(listing_url.encode()).hexdigest()[:10]
        l["source"] = "auto"
        l["site"] = site
        l.setdefault("status", "active")
        if l.get("price_eur") and not l.get("price"):
            l["price"] = f"€{int(l['price_eur']):,}"
        valid.append(l)
    return valid


def _extract_with_claude(
    claude: Anthropic, url: str, markdown: str, images: list[str]
) -> list[dict]:
    image_block = (
        "\n".join(f"- {u}" for u in images[:25]) if images else "(none)"
    )
    user_message = (
        f"Source URL: {url}\n\n"
        f"--- candidate image URLs (pick the best hero shot per listing) ---\n"
        f"{image_block}\n\n"
        f"--- page markdown ---\n{markdown}"
    )

    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        tools=[EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "save_listings"},
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "save_listings":
            return block.input.get("listings", []) or []
    return []


def _is_coastal_medoc(listing: dict) -> bool:
    commune = (listing.get("commune") or "").lower().strip().rstrip(",")
    return commune in ALLOWED_COMMUNES


# URL patterns that indicate a rental (we want sales only).
RENTAL_URL_FRAGMENTS = ("/locations/", "/location/", "/a-louer/", "/rental/", "/rent/")


def _is_rental_url(url: str) -> bool:
    return any(frag in url.lower() for frag in RENTAL_URL_FRAGMENTS)


if __name__ == "__main__":
    raise SystemExit(main())
