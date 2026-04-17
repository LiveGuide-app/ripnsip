"""Sources for the property monitor.

Two strategies:

- EXTRACT_SOURCES: Tavily /extract on a search-results URL. Best when the
  site renders listing summaries server-side. One API call → many listings.

- SEARCH_SOURCES: Tavily /search restricted to a domain. Returns individual
  listing URLs along with their markdown content. Used for sites whose
  search pages are JS-only or bot-blocked, but whose individual listing
  pages render OK. Also useful as a "pagination catch" for sites already in
  EXTRACT_SOURCES — search surfaces listings beyond search-page 1.

The search query is shared across SEARCH_SOURCES.
"""

SEARCH_MAX_RESULTS = 8

# Multiple queries per source — different angles surface different niches
# (a wine estate, a maison de maître, a large family compound). URLs are
# deduped across queries before being sent to Claude.
SEARCH_QUERIES: list[dict[str, str]] = [
    {
        "label": "general",
        "query": "vente maison propriété médoc piscine 9 pièces 6 chambres",
    },
    {
        "label": "wine-estate",
        "query": "domaine viticole médoc gironde vente dépendances chai",
    },
    {
        "label": "character",
        "query": "maison de maître médoc atlantique vente grand terrain caractère",
    },
]

EXTRACT_SOURCES: list[dict[str, str]] = [
    {
        "site": "green-acres.fr",
        "url": "https://www.green-acres.fr/property-for-sale/gironde",
    },
    {
        "site": "frenchestateagents.com",
        "url": "https://www.frenchestateagents.com/french-property-for-sale/department/gironde/33",
    },
    {
        "site": "proprietes.lefigaro.fr",
        "url": "https://proprietes.lefigaro.fr/annonces/maison-gironde-aquitaine-france/",
    },
    {
        "site": "bellesdemeures.com",
        "url": "https://www.bellesdemeures.com/en/sale/france/aquitaine/gironde/tt-2-tb-0-pl-256/",
    },
    {
        "site": "paruvendu.fr",
        "url": "https://www.paruvendu.fr/immobilier/vente/maison/gironde-33/",
    },
    # Goldwelling: Joomla site, no usable GET filter — extract the unfiltered
    # all-listings index and rely on the Médoc commune filter downstream.
    {
        "site": "goldwelling.com",
        "url": "https://www.goldwelling.com/index.php/en/all-listing",
    },
]

SEARCH_SOURCES: list[dict[str, str]] = [
    {"site": "leboncoin.fr", "domain": "leboncoin.fr"},
    {"site": "seloger.com", "domain": "seloger.com"},
    {"site": "bienici.com", "domain": "bienici.com"},
    {"site": "lesiteimmo.com", "domain": "lesiteimmo.com"},
    # Also crawl Leggett via search to catch listings beyond search-page 1.
    {"site": "frenchestateagents.com (deep)", "domain": "frenchestateagents.com"},
]
