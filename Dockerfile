# Area Business Miner — Playwright needs a real OS + Chromium, so this must run
# on a normal server/VPS/Docker host (Railway, Koyeb, Fly.io, Hetzner, Oracle Cloud).
# It CANNOT run on WASM/serverless platforms (Wasmer, Vercel, Cloudflare, Netlify).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Python deps first (better layer caching)
RUN pip install "flask>=3.1.3" "scrapling[fetchers]>=0.4.11"

# Chromium + its OS libraries (the heavy part; cached unless deps change)
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# App code
COPY maps_lead_studio/ maps_lead_studio/
COPY maps_area_scraper/ maps_area_scraper/

WORKDIR /app/maps_area_scraper

# SQLite queue lives here — mount a volume to keep data across restarts:
#   docker run -v am_data:/app/maps_area_scraper ...
EXPOSE 5000
ENV PORT=5000

CMD ["python", "app.py"]
