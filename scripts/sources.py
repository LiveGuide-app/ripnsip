"""Search URLs to monitor, one per real-estate site.

Each URL is a search-results page filtered (as much as the site supports)
to Gironde + our price band. The LLM extraction step further filters to
Médoc communes, so false positives here are harmless — they just cost a
few extra tokens.

To add a site: append a dict below. To pause a site: comment it out.
After the first real run, inspect `errors` in listings.json and adjust
URLs for any site that returned zero listings.
"""

SEARCH_URLS: list[dict[str, str]] = [
    {
        "site": "green-acres.fr",
        "url": "https://www.green-acres.fr/en/properties-for-sale/france/aquitaine/gironde?price-min=400000&price-max=900000",
    },
    {
        "site": "frenchestateagents.com",
        "url": "https://www.frenchestateagents.com/french-property-for-sale?location=gironde-aquitaine-france&min_price=400000&max_price=900000",
    },
    {
        "site": "lesiteimmo.com",
        "url": "https://www.lesiteimmo.com/acheter/maison/33-gironde",
    },
    {
        "site": "proprietes.lefigaro.fr",
        "url": "https://proprietes.lefigaro.fr/annonces/maison-gironde-aquitaine-france/",
    },
    {
        "site": "bellesdemeures.com",
        "url": "https://www.bellesdemeures.com/en/sale/france/aquitaine/gironde/",
    },
    {
        "site": "paruvendu.fr",
        "url": "https://www.paruvendu.fr/immobilier/annonceimmofo/liste/listeAnnonces?tt=1&tbApp=0&tbMai=1&tbChb=0&tbDup=0&tbAtl=0&tbPav=0&tbVil=1&tbCha=1&tbPro=1&tbHot=1&tbMou=1&tbFer=1&tbMan=1&tbImm=0&tbLof=0&at=1&codeINSEE=33264~33213~33177~33559~33117~33022~33540~33058~33118",
    },
]
