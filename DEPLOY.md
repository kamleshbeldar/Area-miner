# Deployment Guide — Area Business Miner

## ⚠️ Why Wasmer / Vercel / Cloudflare / Netlify will NEVER work

This app scrapes Google Maps using **Playwright + Chromium (a real browser)**.

- Wasmer runs Python inside **WebAssembly (WASM)** — Playwright has no WASM build
  (`ERROR: No matching distribution found for playwright`), and even if it did,
  WASM cannot spawn a Chromium process. This is a hard platform limitation, not a config issue.
- Vercel / Netlify / Cloudflare are serverless — no long-running background workers,
  no persistent SQLite file, no browser binaries.

**You need a real Linux machine (or Docker host).**

## ✅ Option 1: Your own PC (recommended for scraping — free, your own IP)

```bash
pip install "flask>=3.1.3" "scrapling[fetchers]>=0.4.11"
python -m playwright install chromium
cd maps_area_scraper
python app.py
# open http://localhost:5000
```

## ✅ Option 2: Docker (works on any VPS / Railway / Koyeb / Fly.io)

```bash
docker build -t area-miner .
docker run -d -p 5000:5000 -v am_data:/app/maps_area_scraper --name area-miner area-miner
# open http://<server-ip>:5000
```

The volume keeps `area_scale.db` (all jobs + leads) across restarts.

## ✅ Option 3: Free VPS — Oracle Cloud Free Tier (free forever)

1. Create an "Always Free" ARM VM (up to 4 CPU / 24 GB RAM) at cloud.oracle.com
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Clone repo and run the Docker commands above
4. Open port 5000 in the VCN security list

## ✅ Option 4: Railway / Koyeb (easy git-push deploy)

Both detect the `Dockerfile` automatically:
- **Railway**: New Project → Deploy from GitHub → select repo. Add a volume mounted
  at `/app/maps_area_scraper` for persistence.
- **Koyeb**: Create Service → GitHub → Dockerfile builder. Free tier: 1 small instance.

Notes:
- Free tiers have limited RAM (512 MB – 1 GB). Use **1 worker** and headless mode.
- For serious scraping (multiple workers), a cheap VPS (Hetzner CX22 ~€4/mo) is much better.

## 🔐 Recommended settings on a public server

- Keep worker count low (1–2) on small instances — each worker = one Chromium (~300–400 MB RAM)
- Set a proxy in the start form if the server's datacenter IP gets blocked by Google
- The app has no built-in auth — put it behind a firewall, VPN, or reverse-proxy
  basic auth (e.g. Caddy: `basic_auth { user hash }`) if exposed to the internet
