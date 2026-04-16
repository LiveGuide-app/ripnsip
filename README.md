# Rip & Sip

Planning repository for **Rip & Sip** — a boutique surf & wine retreat planned for the Médoc, France. 12 rooms + 4 glamping tents. Rooms open spring 2028, glamping spring 2029.

Published as a static GitHub Pages site; the property monitor runs weekly in GitHub Actions.

## Structure

```
.
├── index.html                      # Landing page / vision
├── properties/
│   ├── index.html                  # Renders listings.json
│   └── data/listings.json          # Seeded now; overwritten weekly by the Action
├── marketing/                      # Placeholder
├── financials/                     # Placeholder
├── assets/css/style.css            # Shared styles
├── scripts/
│   ├── property_monitor.py         # Tavily + Claude Haiku pipeline
│   ├── sources.py                  # List of search URLs to monitor
│   └── requirements.txt            # Python deps
├── .github/workflows/
│   └── property-monitor.yml        # Weekly cron + manual trigger
└── .nojekyll
```

## Enabling GitHub Pages

1. Push to GitHub.
2. Settings → Pages → Source: **Deploy from a branch** → `main` / `/ (root)`.
3. Site: `https://<user>.github.io/<repo>/`.

## Property monitor setup

The monitor uses **Tavily** (web scrape) + **Claude Haiku** (structured extraction) — the combo survives JS-rendered pages and anti-bot measures without maintaining brittle CSS selectors.

### One-time setup

1. **Get API keys:**
   - Tavily: https://tavily.com — free tier is 1,000 credits/month (this project uses ~25).
   - Anthropic: https://console.anthropic.com — Haiku usage for this project costs ~$0.01/month.
2. **Add them as GitHub Secrets:**
   Repository → Settings → Secrets and variables → Actions → *New repository secret*
   - `TAVILY_API_KEY`
   - `ANTHROPIC_API_KEY`
3. **Enable bot writes:**
   Settings → Actions → General → Workflow permissions → **Read and write**.
4. **First run:** Actions tab → *Property Monitor* → *Run workflow*.

### Schedule

- **Cron:** Monday at 07:00 UTC (`0 7 * * 1`) — edit in `.github/workflows/property-monitor.yml`.
- **Manual:** any time via *Run workflow*.

### How it works

1. `scripts/sources.py` lists search URLs (one per real-estate site).
2. Tavily `/extract` fetches clean markdown for all URLs in a single batch.
3. Claude Haiku extracts structured listings from each page's markdown using a JSON schema, filters to Médoc communes, and enforces absolute listing URLs.
4. Results are deduped by URL and written as a fresh snapshot to `properties/data/listings.json`. The bot commits the file if anything changed.

### Adding or fixing a source

Edit `scripts/sources.py` — append `{"site": "...", "url": "..."}` to `SEARCH_URLS` and run the workflow manually to verify.

### Current sources

| Site | URL shape |
|------|-----------|
| green-acres.fr | Gironde search, price-filtered |
| frenchestateagents.com (Leggett) | Gironde search, price-filtered |
| lesiteimmo.com | Gironde maisons |
| proprietes.lefigaro.fr | Gironde annonces |
| bellesdemeures.com | Gironde listings |
| paruvendu.fr | Médoc communes by INSEE code |

First real run will expose any sources where Tavily can't reach the content or the URL format has drifted — check the `errors` array in `listings.json`.

## Local testing

```bash
pip install -r scripts/requirements.txt
TAVILY_API_KEY=tvly-... ANTHROPIC_API_KEY=sk-ant-... python scripts/property_monitor.py
```
