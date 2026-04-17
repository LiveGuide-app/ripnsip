"""Search URLs to monitor, one per real-estate site.

Each URL is a search-results page scoped to Gironde (département 33).
Price, room-count and location filters are enforced downstream by the
LLM + a numeric post-filter, so it's fine if a site doesn't support them
in the URL.

To add a site: append a dict below. To pause a site: comment it out.
"""

SEARCH_URLS: list[dict[str, str]] = [
    {
        "site": "green-acres.fr",
        "url": "https://www.green-acres.fr/property-for-sale/gironde",
    },
    {
        "site": "frenchestateagents.com",
        "url": "https://www.frenchestateagents.com/french-property-for-sale/department/gironde/33",
    },
    {
        "site": "lesiteimmo.com",
        "url": "https://www.lesiteimmo.com/acheter/maison/gironde-33",
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
    {
        "site": "leboncoin.fr",
        "url": "https://www.leboncoin.fr/recherche?category=9&locations=d_33&real_estate_type=1",
    },
    {
        "site": "seloger.com",
        "url": "https://www.seloger.com/immobilier/achat/immo-gironde/",
    },
    {
        "site": "bienici.com",
        "url": "https://www.bienici.com/recherche/achat/gironde-33",
    },
]
