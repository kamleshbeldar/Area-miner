from __future__ import annotations

import math
import random
import re
import socket
import ssl
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote_plus, urljoin, urlparse

from scrapling.fetchers import DynamicFetcher, Fetcher


LogFn = Callable[[str], None]
LeadFn = Callable[[dict], None]
StateFn = Callable[[], bool]


@dataclass
class SearchConfig:
    categories: list[str]
    locations: list[str]
    keywords: list[str] = field(default_factory=list)
    search_mode: str = "exact"
    max_results: int = 100
    min_rating: float | None = None
    max_rating: float | None = None
    min_reviews: int | None = None
    max_reviews: int | None = None
    website_condition: str = "all"
    contact_requirement: str = "none"
    business_status: str = "operational"
    enrich_websites: bool = True
    scrape_review_details: bool = False
    review_limit: int = 5
    detail_retries: int = 2
    page_delay_ms: int = 1300
    random_delay_min_ms: int = 300
    random_delay_max_ms: int = 900
    requests_per_minute: int = 30
    parallel_workers: int = 1
    location_mode: str = "text"
    latitude: float | None = None
    longitude: float | None = None
    radius_km: float | None = None
    retry_urls: list[str] = field(default_factory=list)
    headless: bool = True
    real_chrome: bool = False


def build_queries(config: SearchConfig) -> list[tuple[str, str]]:
    suffixes = [""]
    if config.search_mode == "balanced":
        suffixes = ["", "near me", "services"]
    elif config.search_mode == "broad":
        suffixes = ["", "near me", "company", "services", "best"]
    keywords = config.keywords or [""]
    queries: list[tuple[str, str]] = []
    for category in config.categories:
        for location in config.locations:
            for keyword in keywords:
                for suffix in suffixes:
                    query = " ".join(x for x in (category, keyword, suffix, location) if x).strip()
                    if query and (query, location) not in queries:
                        queries.append((query, location))
    return queries


def build_search_url(query: str, config: SearchConfig) -> str:
    base = f"https://www.google.com/maps/search/{quote_plus(query)}"
    if config.location_mode == "coordinates" and config.latitude is not None and config.longitude is not None:
        radius = max(0.2, config.radius_km or 5)
        zoom = max(8, min(18, round(14 + math.log2(5 / radius))))
        return f"{base}/@{config.latitude},{config.longitude},{zoom}z"
    return base


class RateLimiter:
    def __init__(self, per_minute: int):
        self.limit = max(1, per_minute)
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0] >= 60:
                    self.events.popleft()
                if len(self.events) < self.limit:
                    self.events.append(now)
                    return
                delay = max(0.05, 60 - (now - self.events[0]))
            time.sleep(min(delay, 1))


def _place_key(lead: dict) -> str:
    if lead.get("place_id"):
        return f"place:{lead['place_id']}"
    phone = re.sub(r"\D", "", lead.get("phone", ""))
    if phone:
        return f"phone:{phone}"
    domain = urlparse(lead.get("website", "")).netloc.lower().removeprefix("www.")
    if domain:
        return f"domain:{domain}"
    return "text:" + "|".join(re.sub(r"\W", "", str(lead.get(x, "")).lower()) for x in ("name", "address"))


def _passes_filters(lead: dict, config: SearchConfig) -> bool:
    rating, reviews = lead.get("rating"), lead.get("reviews")
    if config.min_rating is not None and (rating is None or rating < config.min_rating):
        return False
    if config.max_rating is not None and (rating is None or rating > config.max_rating):
        return False
    if config.min_reviews is not None and (reviews is None or reviews < config.min_reviews):
        return False
    if config.max_reviews is not None and (reviews is None or reviews > config.max_reviews):
        return False
    if config.website_condition == "with" and not lead.get("website"):
        return False
    if config.website_condition == "without" and lead.get("website"):
        return False
    if config.contact_requirement == "phone" and not lead.get("phone"):
        return False
    if config.contact_requirement == "email" and not lead.get("email"):
        return False
    if config.contact_requirement == "any" and not any(lead.get(x) for x in ("phone", "email", "website")):
        return False
    return not (config.business_status == "operational" and lead.get("status") == "closed")


def _contact_confidence(lead: dict) -> None:
    phone = re.sub(r"\D", "", lead.get("phone", ""))
    lead["phone_valid"] = 10 <= len(phone) <= 15
    lead["phone_confidence"] = 95 if lead["phone_valid"] and lead.get("phone", "").startswith("+") else 82 if lead["phone_valid"] else 20 if phone else 0
    email = lead.get("email", "").lower()
    email_valid = bool(re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", email))
    site_domain = urlparse(lead.get("website", "")).netloc.lower().removeprefix("www.")
    email_domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    lead["email_valid"] = email_valid
    lead["email_confidence"] = 95 if email_valid and site_domain and (email_domain == site_domain or email_domain.endswith("." + site_domain)) else 75 if email_valid else 0


def _score(lead: dict) -> tuple[int, str]:
    score = (22 if lead.get("phone_valid") else 0) + (22 if lead.get("email_valid") else 0)
    score += 12 if lead.get("website") else 18
    score += 8 if lead.get("address") else 0
    score += 8 if lead.get("social_profiles") else 0
    score += 5 if lead.get("whatsapp") else 0
    score += min(10, (lead.get("reviews") or 0) // 25)
    score += 5 if (lead.get("rating") or 0) >= 4 else 0
    score = min(100, score)
    return score, "hot" if score >= 70 else "warm" if score >= 45 else "cold"


def _ssl_status(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return "not_https"
    try:
        context = ssl.create_default_context()
        with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname=parsed.hostname) as secured:
                secured.getpeercert()
        return "valid"
    except Exception:
        return "invalid"


def _detect_technologies(html: str) -> list[str]:
    signatures = {
        "WordPress": ("wp-content", "wp-includes"), "Shopify": ("cdn.shopify.com", "shopify.theme"),
        "Wix": ("wixstatic.com",), "Squarespace": ("static.squarespace.com",),
        "Webflow": ("webflow.js", "data-wf-page"), "React": ("react-dom", "__next_data__"),
        "Bootstrap": ("bootstrap.min.css", "bootstrap.bundle"), "Google Analytics": ("gtag(", "google-analytics.com"),
    }
    lowered = html.lower()
    return [name for name, needles in signatures.items() if any(x in lowered for x in needles)]


def _enrich_website(lead: dict, logger: LogFn) -> None:
    website = lead.get("website", "")
    if not website.startswith(("http://", "https://")):
        _contact_confidence(lead)
        return
    lead["ssl_status"] = _ssl_status(website)
    try:
        page = Fetcher.get(website, timeout=15, stealthy_headers=True)
        raw = page.html_content
        html = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
        emails = sorted(set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", html, re.I)))
        emails = [x for x in emails if not x.lower().endswith((".png", ".jpg", ".webp", ".svg"))]
        links = page.css("a::attr(href)").getall()
        absolute = [urljoin(website, link) for link in links if link]
        socials = [x for x in absolute if any(host in x.lower() for host in ("facebook.com", "instagram.com", "linkedin.com", "youtube.com", "x.com/"))]
        lead.update({
            "email": emails[0] if emails else "", "emails": ", ".join(emails[:5]),
            "social_profiles": ", ".join(dict.fromkeys(socials[:5])),
            "whatsapp": next((x for x in absolute if "wa.me/" in x.lower() or "api.whatsapp.com" in x.lower()), ""),
            "contact_page": next((x for x in absolute if re.search(r"/(contact|contact-us|about)(/|$)", x, re.I)), ""),
            "website_status": "reachable", "technologies": ", ".join(_detect_technologies(html)),
        })
    except Exception as exc:
        lead["website_status"] = "failed"
        logger(f"ENRICH - Website failed for {lead.get('name')}: {str(exc)[:100]}")
    _contact_confidence(lead)


COLLECT_SCRIPT = r"""
async ({ maxResults }) => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const feed = document.querySelector('[role="feed"]');
  let stagnant = 0, previous = 0;
  for (let i = 0; feed && i < 180 && stagnant < 9; i++) {
    feed.scrollTo(0, feed.scrollHeight); feed.dispatchEvent(new Event('scroll', {bubbles:true})); await sleep(700);
    const count = document.querySelectorAll('a[href*="/maps/place/"]').length;
    stagnant = count === previous ? stagnant + 1 : 0; previous = count;
    if (count >= maxResults) break;
  }
  const found = [], seen = new Set();
  for (const anchor of document.querySelectorAll('a[href*="/maps/place/"]')) {
    if (!anchor.href || seen.has(anchor.href)) continue; seen.add(anchor.href);
    const card = anchor.closest('[role="article"], .Nv2PK') || anchor.parentElement || anchor;
    found.push({href:anchor.href, fallbackName:anchor.getAttribute('aria-label') || '', fallbackText:card.innerText || ''});
    if (found.length >= maxResults) break;
  }
  return found;
}
"""


DETAIL_SCRIPT = r"""
({ fallbackName, fallbackText, mapsUrl }) => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const attr = (selectors, name) => { for (const s of selectors) { const e=document.querySelector(s),v=clean(e?.getAttribute(name)); if(v)return v; } return ''; };
  const text = selectors => { for (const s of selectors) { const e=document.querySelector(s),v=clean(e?.innerText||e?.textContent); if(v)return v; } return ''; };
  const body=clean(document.body.innerText), ratingRaw=text(['div.F7nice span[aria-hidden="true"]','span.MW4etd']);
  const reviewRaw=attr(['span[aria-label*="reviews"]','button[aria-label*="reviews"]'],'aria-label')||body.match(/[0-9,]+\s+reviews?/i)?.[0]||'';
  const phoneRaw=attr(['button[data-item-id^="phone:tel"]','button[aria-label^="Phone"]'],'aria-label');
  const addressRaw=attr(['button[data-item-id="address"]','button[aria-label^="Address"]'],'aria-label');
  const website=attr(['a[data-item-id="authority"]','a[aria-label^="Website"]'],'href');
  const hoursRaw=attr(['button[data-item-id="oh"]','button[aria-label*="hours"]'],'aria-label')||text(['div[aria-label*="hours"]']);
  const plusRaw=attr(['button[data-item-id="oloc"]','button[aria-label^="Plus code"]'],'aria-label');
  const category=text(['button[jsaction*="category"]','.DkEaL','button.DkEaL']);
  const nameRaw=text(['h1.DUwDvf','h1']), rating=String(ratingRaw).match(/[0-5](?:\.[0-9])?/), reviews=String(reviewRaw).match(/[0-9][0-9,]*/);
  const coords=(location.href||mapsUrl).match(/@(-?\d+\.\d+),(-?\d+\.\d+)/), placeId=(location.href||mapsUrl).match(/!1s([^!/?]+)/)?.[1]||mapsUrl.match(/place\/([^/@]+)/)?.[1]||'';
  const price=body.match(/(?:₹|\$|£|€){1,4}/)?.[0]||'';
  return {name:nameRaw&&nameRaw.toLowerCase()!=='results'?nameRaw:fallbackName,category,rating:rating?Number(rating[0]):null,reviews:reviews?Number(reviews[0].replace(/,/g,'')):null,
    phone:clean(phoneRaw.replace(/^Phone:?\s*/i,'')),website,address:clean(addressRaw.replace(/^Address:?\s*/i,'')),business_hours:clean(hoursRaw.replace(/^Hours:?\s*/i,'')),price_range:price,plus_code:clean(plusRaw.replace(/^Plus code:?\s*/i,'')),
    status:/permanently closed/i.test(body)?'closed':/temporarily closed/i.test(body)?'temporarily_closed':'operational',latitude:coords?Number(coords[1]):null,longitude:coords?Number(coords[2]):null,
    place_id:decodeURIComponent(placeId),maps_url:location.href||mapsUrl,raw_card:clean(fallbackText)};
}
"""


REVIEW_SCRIPT = r"""
async ({ limit }) => {
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const button=document.querySelector('button[jsaction*="pane.reviewChart.moreReviews"],button[aria-label*="reviews"]');
  if(button){button.click();await sleep(1400);} const feed=document.querySelector('div[role="main"] div[tabindex="-1"],div[role="feed"]');
  for(let i=0;feed&&i<Math.ceil(limit/3)+2;i++){feed.scrollTo(0,feed.scrollHeight);await sleep(500);}
  const rows=[];
  for(const card of document.querySelectorAll('div.jftiEf,div[data-review-id]')){
    const clean=v=>(v||'').replace(/\s+/g,' ').trim();
    rows.push({reviewer:clean(card.querySelector('.d4r55')?.textContent),rating:card.querySelector('[aria-label*="star"]')?.getAttribute('aria-label')||'',date:clean(card.querySelector('.rsqaWe')?.textContent),text:clean(card.querySelector('.wiI7pd')?.textContent)});
    if(rows.length>=limit)break;
  } return rows;
}
"""


def scrape_google_maps(config: SearchConfig, logger: LogFn = print, on_lead: LeadFn | None = None,
                       on_duplicate: LeadFn | None = None, on_failure: LeadFn | None = None,
                       should_cancel: StateFn = lambda: False, should_pause: StateFn = lambda: False) -> dict:
    queries = build_queries(config)
    if not queries and not config.retry_urls:
        raise ValueError("At least one category and location are required.")
    leads: list[dict] = []
    duplicates: list[dict] = []
    failures: list[dict] = []
    seen: dict[str, dict] = {}
    state_lock = threading.RLock()
    limiter = RateLimiter(config.requests_per_minute)

    def wait_turn() -> None:
        while should_pause() and not should_cancel():
            time.sleep(0.4)
        limiter.wait()
        low, high = sorted((config.random_delay_min_ms, config.random_delay_max_ms))
        time.sleep(random.randint(max(0, low), max(0, high)) / 1000)

    def accept(lead: dict) -> None:
        with state_lock:
            key = _place_key(lead)
            if key in seen:
                lead.update({"duplicate_of": seen[key].get("name", ""), "duplicate_key": key})
                duplicates.append(lead)
                if on_duplicate: on_duplicate(lead)
                return
            seen[key] = lead
            if not _passes_filters(lead, config) or len(leads) >= config.max_results:
                return
            leads.append(lead)
            if on_lead: on_lead(lead)
            logger(f"DETAIL - {len(leads)}/{config.max_results}: {lead['name']}")

    def fail(place: dict, query: str, location: str, error: str) -> None:
        item = {"maps_url": place.get("href", ""), "name": place.get("fallbackName", ""), "query": query, "location": location, "error": error, "attempts": config.detail_retries + 1}
        with state_lock: failures.append(item)
        if on_failure: on_failure(item)

    def process_place(page, place: dict, query: str, location: str) -> None:
        lead = None
        last_error = "Missing detail fields"
        for attempt in range(config.detail_retries + 1):
            try:
                wait_turn()
                page.goto(place["href"], wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(config.page_delay_ms + attempt * 500)
                candidate = page.evaluate(DETAIL_SCRIPT, {"fallbackName": place.get("fallbackName", ""), "fallbackText": place.get("fallbackText", ""), "mapsUrl": place["href"]})
                if candidate.get("name") and (candidate.get("address") or candidate.get("phone") or attempt == config.detail_retries):
                    lead = candidate
                    break
            except Exception as exc:
                last_error = str(exc)[:300]
                logger(f"RETRY - {place.get('fallbackName') or 'place'}, attempt {attempt + 1}: {last_error[:90]}")
        if not lead:
            fail(place, query, location, last_error)
            return
        lead.update({"query": query, "location": location})
        if config.scrape_review_details:
            try:
                lead["review_details"] = page.evaluate(REVIEW_SCRIPT, {"limit": config.review_limit})
                lead["review_details_count"] = len(lead["review_details"])
            except Exception as exc:
                lead["review_details"] = []
                logger(f"REVIEWS - Optional reviews failed for {lead.get('name')}: {str(exc)[:90]}")
        if config.enrich_websites and lead.get("website"):
            _enrich_website(lead, logger)
        else:
            _contact_confidence(lead)
        score, tier = _score(lead)
        lead.update({"lead_score": score, "lead_tier": tier})
        accept(lead)

    def run_query(query_index: int, query: str, location: str, direct_urls: list[str] | None = None) -> None:
        if should_cancel(): return
        logger(f"DISCOVERY - Query {query_index}/{max(1, len(queries))}: {query}")
        def page_action(page):
            page.wait_for_timeout(2500)
            remaining = max(1, config.max_results - len(leads))
            places = [{"href": url, "fallbackName": "Retry", "fallbackText": ""} for url in direct_urls] if direct_urls else page.evaluate(COLLECT_SCRIPT, {"maxResults": remaining})
            logger(f"DISCOVERY - {len(places)} listing URLs found")
            for place in places:
                if should_cancel() or len(leads) >= config.max_results: break
                process_place(page, place, query, location)
        url = direct_urls[0] if direct_urls else build_search_url(query, config)
        try:
            DynamicFetcher.fetch(url, headless=config.headless, real_chrome=config.real_chrome, wait=1000, timeout=60000,
                network_idle=False, page_action=page_action, extra_flags=["--disable-blink-features=AutomationControlled"])
        except Exception as exc:
            logger(f"FAILED - Query failed: {str(exc)[:180]}")
            if direct_urls:
                for retry_url in direct_urls: fail({"href": retry_url}, query, location, str(exc))

    work = [(i, q, loc, None) for i, (q, loc) in enumerate(queries, 1)]
    if config.retry_urls:
        work = [(1, "Failed URL retry", "", config.retry_urls)]
    workers = max(1, min(config.parallel_workers, len(work) or 1, 4))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="maps-worker") as pool:
        futures = [pool.submit(run_query, *item) for item in work]
        for future in as_completed(futures):
            future.result()
    logger(f"COMPLETE - {len(leads)} unique leads, {len(duplicates)} duplicates, {len(failures)} failed URLs")
    return {"leads": leads, "duplicates": duplicates, "failures": len(failures), "failed_urls": failures, "queries": len(work)}
