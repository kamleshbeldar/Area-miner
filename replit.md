# Area Business Miner

Draw a geographic area on a map and automatically collect business data from Google Maps — no Google Maps API key required. Uses Scrapling for browser-based scraping with a two-phase pipeline (discovery then detail).

## Run & Operate

- **Area Business Miner** workflow runs the app at port 5000
- `cd maps_area_scraper && python3 app.py` — run manually

## Stack

- Python 3.13, Flask 3
- Scrapling + Playwright (browser automation)
- SQLite WAL (`area_scale.db`) — persistent queue
- Leaflet + OpenStreetMap — map UI (no Google Maps API key needed)

## Where things live

- `maps_area_scraper/app.py` — Flask routes and worker thread management
- `maps_area_scraper/area_engine.py` — grid geometry, discovery, detail extraction
- `maps_area_scraper/queue_store.py` — SQLite queue and job management
- `maps_area_scraper/templates/` — Jinja2 HTML templates
- `maps_area_scraper/static/` — JS and CSS
- `maps_lead_studio/scraper.py` — **stub only**; replace with the real scraper module to enable actual crawling

## Architecture decisions

- Two-phase pipeline: discovery (collect place URLs) then detail (scrape each place once globally unique)
- SQLite WAL for concurrent worker access and crash-safe task recovery
- Shared rate gate across all workers (requests_per_minute)
- Per-run deduplication by place_id, phone, website, or name/address

## Product

Draw a circle, rectangle, or polygon on the map. Configure coverage depth, grid spacing, workers, and rate limits. The app creates a persistent SQLite task queue and runs browser workers that crawl Google Maps. Export results as CSV or JSONL at any time.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- `maps_lead_studio/scraper.py` is a stub. The real module (from the sibling `maps_lead_studio` project) must be present for crawls to actually collect data.
- Playwright browsers must be installed: `python3 -m playwright install chromium`
- Port 5000 — the workflow routes here via the webview

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
