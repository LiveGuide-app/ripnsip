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
    "price_min_eur": 450_000,
    "price_max_eur": 650_000,
    "min_rooms": 9,
    "must_have": [
        "at least 9 rooms (pièces) — ideally space for 12 bedrooms",
        "within 20 min drive of the Atlantic coast",
        "pool",
        "outbuildings or land for glamping",
    ],
    "notes": "Monitor widens price band to €400–900k to catch stretch candidates",
}

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_MARKDOWN_CHARS = 150_000  # Safety cap per page
PRICE_MIN_EUR = 400_000
PRICE_MAX_EUR = 900_000

# Communes within ~20 min drive of the Atlantic coast.
# Estuary-side wine villages (Pauillac, Margaux, Saint-Estèphe, Moulis,
# Listrac, Cissac, Bégadan, Blaignan, Prignac, Vertheuil, etc.) are
# intentionally excluded — they're 25–40 min from the ocean.
COASTAL_MEDOC_COMMUNES = [
    # Direct coast
    "soulac", "grayan", "vendays", "montalivet", "naujac",
    "hourtin", "carcans", "lacanau",
    # Central Médoc, within ~20 min of the coast
    "lesparre", "gaillan", "saint-vivien", "queyrac", "vensac",
    "jau-dignac", "saint-laurent-médoc", "saint-laurent-medoc",
]

SYSTEM_PROMPT = """You extract French real-estate listings from scraped search-page markdown for a boutique surf & wine retreat in the coastal Médoc, SW France.

LOCATION — keep only listings within ~20 min drive of the Atlantic coast.
In practice that means these Médoc communes:
- Direct coast: Soulac-sur-Mer, Grayan-et-l'Hôpital, Vendays-Montalivet,
  Naujac-sur-Mer, Hourtin, Carcans, Lacanau.
- Central Médoc (~20 min to coast): Lesparre-Médoc, Gaillan-en-Médoc,
  Saint-Vivien-de-Médoc, Queyrac, Vensac, Jau-Dignac-et-Loirac,
  Saint-Laurent-Médoc.
Exclude estuary-side Médoc wine villages (Pauillac, Margaux, Saint-Estèphe,
Moulis, Listrac, Cissac, Bégadan, Blaignan, Prignac, Vertheuil) — they are
25–40 min from the ocean. Also exclude anywhere else in Gironde (Bordeaux,
Cap Ferret, Arcachon, Bassin d'Arcachon, Libourne, Saint-Émilion, etc.).

PRICE — €400,000 – €900,000. Skip listings clearly priced above €900k or
below €400k. If price is not stated, keep the listing (unknown is fine).

SIZE — we need capacity for ~12 bedrooms. Prefer listings with 9+ rooms
("pièces") or 6+ bedrooms ("chambres"). If the listing is clearly tiny
(e.g. 2-4 rooms, studio, apartment, flat), skip it. If size isn't stated,
keep the listing.

Rules:
- Emit one object per distinct listing.
- `url` must be the absolute URL of the individual listing page — never a
  search, category or filter page. If the only URL you can find is a search
  page, skip the listing.
- If a numeric field isn't stated, omit it. Do not guess.
- `features` — at most 6 items highlighting: pool, outbuildings, gîte,
  barn, hectares of land, proximity to beach/sea, room/bedroom counts.
- If a listing is marked sold / under offer / sous compromis, set `status`
  to "sold".
- If nothing on the page matches all three criteria (region + price + size),
  return an empty list.

Return everything via the save_listings tool."""

EXTRACTION_TOOL = {
    "name": "save_listings",
    "description": "Save the Médoc property listings extracted from the page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "listings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "location": {"type": "string", "description": "Commune name, ideally with 'Gironde' suffix"},
                        "price_eur": {"type": ["integer", "null"]},
                        "bedrooms": {"type": ["integer", "null"]},
                        "area_m2": {"type": ["integer", "null"], "description": "Building area"},
                        "land_m2": {"type": ["integer", "null"]},
                        "features": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                        "status": {"type": "string", "enum": ["active", "sold", "unknown"]},
                        "url": {"type": "string", "description": "Absolute URL of the individual listing page"},
                        "notes": {"type": "string"},
                    },
                    "required": ["title", "location", "url"],
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
            site = site_by_url.get(url, urlparse(url).netloc)

            if not content.strip():
                errors.append(f"tavily:{url}: empty content")
                continue

            valid = _extract_and_filter(claude, url, content, site, errors)
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
                timeout=60,
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"tavily-search:{site}: {e}")
            print(f"[warn] search failed for {site}: {e}", file=sys.stderr)
            continue

        results = search_response.get("results", []) or []
        kept = 0
        for result in results:
            url = (result.get("url") or "").strip()
            content = result.get("raw_content") or result.get("content") or ""
            if not url or not content.strip():
                continue
            valid = _extract_and_filter(claude, url, content, site, errors)
            all_listings.extend(valid)
            kept += len(valid)
        print(f"[search]  {site}: {len(results)} candidate URLs → {kept} kept")

    # ─── Dedupe by listing URL ────────────────────────────────────────────
    seen: set[str] = set()
    deduped: list[dict] = []
    for l in all_listings:
        if l["url"] in seen:
            continue
        seen.add(l["url"])
        deduped.append(l)

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
    claude: Anthropic, source_url: str, content: str, site: str, errors: list[str]
) -> list[dict]:
    """Run Claude extraction on one page's markdown and return valid listings."""
    if len(content) > MAX_MARKDOWN_CHARS:
        content = content[:MAX_MARKDOWN_CHARS]

    try:
        raw_listings = _extract_with_claude(claude, source_url, content)
    except Exception as e:  # noqa: BLE001
        errors.append(f"claude:{source_url}: {e}")
        print(f"[warn] claude extraction failed for {source_url}: {e}", file=sys.stderr)
        return []

    valid: list[dict] = []
    for l in raw_listings:
        listing_url = (l.get("url") or "").strip()
        if not listing_url or not listing_url.startswith("http"):
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


def _extract_with_claude(claude: Anthropic, url: str, markdown: str) -> list[dict]:
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
        messages=[
            {
                "role": "user",
                "content": f"Source URL: {url}\n\n--- page markdown ---\n{markdown}",
            }
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "save_listings":
            return block.input.get("listings", []) or []
    return []


def _is_coastal_medoc(listing: dict) -> bool:
    haystack = " ".join([
        str(listing.get("location") or ""),
        str(listing.get("title") or ""),
        str(listing.get("notes") or ""),
    ]).lower()
    return any(c in haystack for c in COASTAL_MEDOC_COMMUNES)


if __name__ == "__main__":
    raise SystemExit(main())
