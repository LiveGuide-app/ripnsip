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
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from anthropic import Anthropic
from tavily import TavilyClient

from sources import (
    EXTRACT_SOURCES,
    SEARCH_MAX_RESULTS,
    SEARCH_QUERIES,
    SEARCH_SOURCES,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
LISTINGS_PATH = REPO_ROOT / "properties" / "data" / "listings.json"

CRITERIA = {
    "region": "France's Atlantic southwest — coastal Gironde (Médoc + Cap-Ferret peninsula) plus the Born plateau in northern Landes (Biscarrosse / Sanguinet / Parentis lakes + Biscarrosse-Plage surf). All within ~20 min of ocean surf or kite-friendly water; all within ~1-1.5 hr of Bordeaux-Mérignac airport.",
    "price_min_eur": 400_000,
    "price_max_eur": 1_300_000,
    "scoring_max": 100,
    "scoring_components": {
        "size_potential": 25,
        "pool": 15,
        "glamping_land": 15,
        "coast_proximity": 15,
        "character": 15,
        "renovation_state": 15,
    },
    "notes": "Hard filters: 23 communes across coastal Gironde and northern Landes + €400k-1.3M. Everything else is scored 0-100 by Claude Haiku.",
}

CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_MARKDOWN_CHARS = 40_000  # Safety cap per page. Most listing pages are
                             # <15K chars; this trims nav/footer/related-listings
                             # bloat that costs tokens without adding signal.
PRICE_MIN_EUR = 400_000
PRICE_MAX_EUR = 1_300_000
SITE_URL = "https://liveguide-app.github.io/ripnsip/properties/"
TELEGRAM_MAX_LISTINGS_IN_MESSAGE = 5

# Allowed communes — within ~20 min drive of Atlantic surf or kite-friendly
# water (ocean or the Carcans-Hourtin / Lacanau / Arcachon basins). Match is
# on the LLM's `commune` field (exact, case-insensitive), so spurious hits
# from other Médoc place-names (e.g. "Le Pian-Médoc" south of Bordeaux)
# can no longer slip through via location/title substring.
ALLOWED_COMMUNES = {
    # Direct coast / surf-kite zone (Gironde)
    "soulac-sur-mer", "grayan-et-l'hôpital", "grayan-et-l'hopital",
    "vendays-montalivet", "naujac-sur-mer", "hourtin", "carcans", "lacanau",
    "le porge",
    "lège-cap-ferret", "lege-cap-ferret",
    # Central Médoc, ~20 min to coast
    "lesparre-médoc", "lesparre-medoc",
    "gaillan-en-médoc", "gaillan-en-medoc",
    "saint-vivien-de-médoc", "saint-vivien-de-medoc",
    "queyrac", "vensac",
    "jau-dignac-et-loirac",
    "saint-laurent-médoc", "saint-laurent-medoc",
    # Inland southern Gironde, ~20 min to surf coast (Le Porge / Lacanau)
    "saumos",
    "sainte-hélène", "sainte-helene",
    # Born plateau, northern Landes — ocean surf at Biscarrosse-Plage,
    # kite-friendly lakes (Cazaux-Sanguinet, Biscarrosse-Parentis), ~1 hr
    # to Bordeaux-Mérignac airport
    "biscarrosse", "sanguinet", "parentis-en-born", "gastes", "ychoux",
}

SYSTEM_PROMPT = """You extract French real-estate listings from scraped pages for a boutique surf & wine retreat on France's Atlantic southwest coast (coastal Gironde + northern Landes), then SCORE each kept listing's fit.

LISTING TYPE — sales only (vente / for sale / à vendre). SKIP rentals: anything labelled "à louer", "location", "monthly rent", "/locations/" in the URL, or with a price like "€X / mois".

LOCATION (hard filter) — accept ONLY these 23 communes and reject any other:
- Direct coast / surf-kite zone (Gironde): Soulac-sur-Mer, Grayan-et-l'Hôpital, Vendays-Montalivet, Naujac-sur-Mer, Hourtin, Carcans, Lacanau, Le Porge, Lège-Cap-Ferret.
- Central Médoc (~20 min to coast): Lesparre-Médoc, Gaillan-en-Médoc, Saint-Vivien-de-Médoc, Queyrac, Vensac, Jau-Dignac-et-Loirac, Saint-Laurent-Médoc.
- Inland southern Gironde (~20 min to surf coast): Saumos, Sainte-Hélène.
- Born plateau, northern Landes (ocean surf + kite lakes, ~1 hr to Bordeaux airport): Biscarrosse, Sanguinet, Parentis-en-Born, Gastes, Ychoux.

Note on Lège-Cap-Ferret: the official commune covers the whole Cap Ferret peninsula. If a listing says "Cap Ferret", "Claouey", "Le Canon", "L'Herbe", "Piraillan", "Petit Piquey", "Grand Piquey", or "Lège", set `commune` to "Lège-Cap-Ferret".

Note on Biscarrosse: the commune contains both Biscarrosse-Bourg (town centre, on the lake) and Biscarrosse-Plage (the surf beach). If a listing mentions "Biscarrosse-Plage" or "Biscarrosse-Bourg", set `commune` to "Biscarrosse".

EXPLICITLY REJECT (common confusables): Le Pian-Médoc (south of Bordeaux, NOT coastal), Castelnau-de-Médoc, Macau, Margaux, Pauillac, Saint-Estèphe, Saint-Seurin-de-Cadourne, Moulis-en-Médoc, Listrac-Médoc, Cissac-Médoc, Bégadan, Blaignan, Prignac-en-Médoc, Vertheuil, Saint-Germain-d'Esteuil, Saint-Yzans-de-Médoc, Couquèques, Ordonnac, Valeyrac, Saint-Christoly-Médoc — other Bassin d'Arcachon communes (Arès, Andernos-les-Bains, Lanton, Audenge, Biganos, Le Teich, Gujan-Mestras, La Teste-de-Buch, Arcachon) — other Landes communes south of the Born plateau (Mimizan, Bias, Aureilhan, Lit-et-Mixe, Léon, Vieux-Boucau, Soustons, Seignosse, Hossegor / Soorts-Hossegor, Capbreton) — and the rest of Gironde / Landes / Pays Basque (Bordeaux, Libourne, Saint-Émilion, Mont-de-Marsan, Dax, Biarritz, Anglet, etc.).

Set the `commune` field to the exact commune name only (e.g. "Lesparre-Médoc", not "Lesparre-Médoc, Gironde"). If you cannot identify the exact commune from the listing, skip it — do not guess.

PRICE (hard filter) — €400,000 to €1,300,000. Skip outside this range. If price isn't stated, keep.

DEPARTMENT NOTE — listings will come from both Gironde (33) and Landes (40). Don't auto-reject Landes listings; only the 5 Born-plateau communes above are eligible there.

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

    previous_by_url = _load_previous_listings()
    previous_urls = set(previous_by_url)
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

        # Run every query, dedupe URLs across queries before sending to Claude
        # — saves tokens on listings that match multiple search angles.
        unique_results: dict[str, dict] = {}
        for q in SEARCH_QUERIES:
            try:
                search_response = tavily.search(
                    query=q["query"],
                    include_domains=[domain],
                    max_results=SEARCH_MAX_RESULTS,
                    search_depth="advanced",
                    include_raw_content="markdown",
                    chunks_per_source=3,
                    timeout=60,
                )
            except Exception as e:  # noqa: BLE001
                errors.append(f"tavily-search:{site}:{q['label']}: {e}")
                print(f"[warn] search '{q['label']}' failed for {site}: {e}", file=sys.stderr)
                continue

            for result in search_response.get("results", []) or []:
                url = (result.get("url") or "").strip()
                if url and url not in unique_results:
                    unique_results[url] = result

        # Tavily /search returns images at the top level (one shared list per
        # search), so they can't be safely attributed to any specific result —
        # pass [] and let Haiku pick from the per-result markdown instead.
        kept = 0
        for url, result in unique_results.items():
            content = result.get("raw_content") or result.get("content") or ""
            if not content.strip():
                continue
            if _is_rental_url(url):
                continue
            valid = _extract_and_filter(claude, url, content, [], site, errors)
            all_listings.extend(valid)
            kept += len(valid)
        print(f"[search]  {site}: {len(unique_results)} unique URLs across {len(SEARCH_QUERIES)} queries → {kept} kept")

    # ─── Dedupe by listing URL, keep highest-scored variant ───────────────
    by_url: dict[str, dict] = {}
    for l in all_listings:
        existing = by_url.get(l["url"])
        if existing is None or (l.get("score") or 0) > (existing.get("score") or 0):
            by_url[l["url"]] = l
    deduped = sorted(by_url.values(), key=lambda x: x.get("score") or 0, reverse=True)

    # ─── Diff against previous run: verify missing URLs are genuinely sold ─
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    current_urls = {l["url"] for l in deduped}
    missing_urls = previous_urls - current_urls

    # Don't re-verify already-sold listings — just carry them forward
    already_sold_missing = [u for u in missing_urls if previous_by_url[u].get("status") == "sold"]
    to_verify = [u for u in missing_urls if previous_by_url[u].get("status") != "sold"]
    print(f"[diff] {len(current_urls)} current, {len(missing_urls)} missing "
          f"({len(to_verify)} to verify, {len(already_sold_missing)} already-sold carried)")

    newly_sold, carried_active = _verify_missing(
        tavily, to_verify, previous_by_url, now_iso, errors
    )
    carried_already_sold = [dict(previous_by_url[u]) for u in already_sold_missing]
    print(f"[diff] {len(newly_sold)} newly sold, {len(carried_active)} carried active")

    # Merge: current run + previously-active that weren't seen + newly-sold +
    # already-sold carried. Sorted by score; UI fades sold listings.
    merged = deduped + carried_active + newly_sold + carried_already_sold
    merged.sort(key=lambda x: x.get("score") or 0, reverse=True)

    output = {
        "updated_at": now_iso,
        "criteria": CRITERIA,
        "errors": errors,
        "listings": merged,
    }

    LISTINGS_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[done] {len(merged)} listings total "
          f"({len(deduped)} active-now, {len(newly_sold)} newly sold), "
          f"{len(errors)} error(s) → {LISTINGS_PATH.relative_to(REPO_ROOT)}")

    # Notify on either new or newly-sold listings
    new_listings = [l for l in deduped if l["url"] not in previous_urls]
    if new_listings or newly_sold:
        _notify_telegram(new_listings, newly_sold, total_active=len(deduped))
    else:
        print("[telegram] no new or newly-sold listings — skipping notification")

    return 0


def _load_previous_listings() -> dict[str, dict]:
    """URL → full listing dict from the existing listings.json, before this
    run overwrites it. Used to diff new/gone URLs and carry sold listings
    forward as historical records."""
    if not LISTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(LISTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {l["url"]: l for l in data.get("listings", []) if l.get("url")}


# Phrases on a listing page that indicate it's off the market.
_SOLD_MARKERS = (
    "vendu", "sold", "under offer", "sous compromis", "sous-compromis",
    "under contract", "no longer available", "plus disponible",
    "annonce retirée", "annonce retiree", "listing removed",
    "cette annonce n'est plus", "this listing is no longer",
    "page not found", "404",
)


def _looks_sold(page_content: str) -> bool:
    lowered = page_content.lower()
    return any(m in lowered for m in _SOLD_MARKERS)


def _verify_missing(
    tavily: TavilyClient,
    missing_urls: list[str],
    previous_by_url: dict[str, dict],
    now_iso: str,
    errors: list[str],
) -> tuple[list[dict], list[dict]]:
    """Re-fetch each missing URL and split into (newly_sold, carried_forward).

    A URL is newly_sold if the page 404s or contains a sold/withdrawn marker.
    Otherwise it's just search-shuffle noise and we carry it forward active.
    """
    if not missing_urls:
        return [], []

    try:
        resp = tavily.extract(
            urls=missing_urls,
            extract_depth="basic",
            format="markdown",
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        errors.append(f"tavily-verify: {e}")
        # Safer default: carry all forward as active rather than falsely mark sold
        return [], [dict(previous_by_url[u]) for u in missing_urls if u in previous_by_url]

    newly_sold: list[dict] = []
    carried: list[dict] = []

    for r in resp.get("results", []) or []:
        url = r.get("url")
        prior = previous_by_url.get(url)
        if not prior:
            continue
        content = r.get("raw_content") or ""
        if _looks_sold(content):
            sold = dict(prior)
            sold["status"] = "sold"
            sold["sold_detected_at"] = now_iso
            newly_sold.append(sold)
        else:
            carried.append(dict(prior))

    # Tavily couldn't fetch — typically a 404, treat as sold
    for f in resp.get("failed_results", []) or []:
        url = f.get("url")
        prior = previous_by_url.get(url)
        if not prior:
            continue
        sold = dict(prior)
        sold["status"] = "sold"
        sold["sold_detected_at"] = now_iso
        newly_sold.append(sold)

    return newly_sold, carried


def _notify_telegram(
    new_listings: list[dict],
    sold_listings: list[dict],
    total_active: int,
) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — skipping")
        return

    parts: list[str] = []

    if new_listings:
        n = len(new_listings)
        parts.append(f"🏠 <b>{n} new Médoc listing{'s' if n != 1 else ''}</b>")
        for l in new_listings[:TELEGRAM_MAX_LISTINGS_IN_MESSAGE]:
            score = l.get("score", "?")
            commune = html.escape(str(l.get("commune") or "?"))
            price = html.escape(str(l.get("price") or "Price on request"))
            verdict = html.escape((l.get("fit_verdict") or "")[:180])
            url = l.get("url") or ""
            line = f"\n<b>Score {score}</b> · {commune} · {price}"
            if verdict:
                line += f"\n<i>{verdict}</i>"
            if url:
                line += f"\n<a href=\"{html.escape(url)}\">View listing →</a>"
            parts.append(line)
        if n > TELEGRAM_MAX_LISTINGS_IN_MESSAGE:
            parts.append(f"\n…and {n - TELEGRAM_MAX_LISTINGS_IN_MESSAGE} more new listing(s).")

    if sold_listings:
        m = len(sold_listings)
        if parts:
            parts.append("")  # blank line separator
        parts.append(f"🏷️ <b>{m} gone from market</b>")
        for l in sold_listings[:TELEGRAM_MAX_LISTINGS_IN_MESSAGE]:
            commune = html.escape(str(l.get("commune") or "?"))
            price = html.escape(str(l.get("price") or "?"))
            title = html.escape((l.get("title") or "")[:80])
            parts.append(f"\n<s>{commune} · {price}</s>  {title}")
        if m > TELEGRAM_MAX_LISTINGS_IN_MESSAGE:
            parts.append(f"\n…and {m - TELEGRAM_MAX_LISTINGS_IN_MESSAGE} more.")

    parts.append(f"\n<a href=\"{SITE_URL}\">See all {total_active} active →</a>")

    payload = {
        "chat_id": chat_id,
        "text": "\n".join(parts),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"[telegram] network error: {e}", file=sys.stderr)
        return

    if resp.ok:
        print(f"[telegram] notified: {len(new_listings)} new, {len(sold_listings)} gone")
        return

    # Log Telegram's error description so we can debug chat-id / parse issues
    try:
        err = resp.json()
        desc = err.get("description", "(no description)")
        code = err.get("error_code", resp.status_code)
    except ValueError:
        desc = resp.text[:500]
        code = resp.status_code
    print(f"[telegram] send failed: code={code} description={desc!r}", file=sys.stderr)


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
        if not isinstance(l, dict):
            continue
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
