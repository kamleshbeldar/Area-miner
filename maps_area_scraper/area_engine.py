from __future__ import annotations

import math
import sys
import re
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


CORE_DIR = Path(__file__).resolve().parents[1] / "maps_lead_studio"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from scrapling.fetchers import DynamicSession  # noqa: E402
from scraper import (  # noqa: E402
    COLLECT_SCRIPT,
    DETAIL_SCRIPT,
    SearchConfig,
    _contact_confidence,
    _enrich_website,
    _score,
    build_search_url,
    scrape_google_maps,
)


QUICK_CATEGORIES = [
    "restaurants", "shops", "hotels", "hospitals", "schools", "banks", "offices",
    "salons", "gyms", "pharmacies", "petrol pumps", "professional services",
]

DEEP_CATEGORIES = QUICK_CATEGORIES + [
    "cafes", "bakeries", "grocery stores", "supermarkets", "clothing stores", "shoe stores",
    "jewellery stores", "electronics stores", "mobile phone stores", "furniture stores",
    "hardware stores", "automobile dealers", "car repair", "bike repair", "travel agencies",
    "real estate agents", "event planners", "wedding services", "photographers", "printing services",
    "digital marketing agencies", "software companies", "accountants", "lawyers", "insurance agencies",
    "clinics", "dentists", "diagnostic centres", "physiotherapists", "veterinary clinics",
    "coaching centres", "colleges", "day care centres", "courier services", "warehouses",
    "manufacturers", "wholesalers", "construction companies", "interior designers", "architects",
    "electricians", "plumbers", "cleaning services", "security services", "beauty parlours",
    "spas", "fitness centres", "sports clubs", "religious places", "community centres",
    "fast food restaurants", "sweet shops", "ice cream shops", "juice shops", "bars", "pubs",
    "banquet halls", "marriage gardens", "guest houses", "hostels", "resorts", "tour operators",
    "taxi services", "transport companies", "logistics companies", "packers and movers", "car rentals",
    "driving schools", "tyre shops", "battery dealers", "car wash", "auto parts stores",
    "medical stores", "ayurvedic clinics", "homeopathy clinics", "eye hospitals", "skin clinics",
    "pathology labs", "nursing homes", "ambulance services", "hearing aid stores", "opticians",
    "stationery shops", "book stores", "toy stores", "gift shops", "cosmetics stores",
    "department stores", "kitchenware stores", "home decor stores", "paint stores", "tile stores",
    "sanitary ware stores", "electrical shops", "solar companies", "computer stores", "CCTV dealers",
    "internet service providers", "repair services", "laundry services", "dry cleaners", "tailors",
    "boutiques", "tattoo studios", "dance schools", "music schools", "yoga centres",
    "chartered accountants", "tax consultants", "financial advisors", "stock brokers", "loan agencies",
    "recruitment agencies", "consulting firms", "advertising agencies", "PR agencies", "BPO companies",
    "web designers", "IT services", "computer training institutes", "language institutes", "libraries",
    "play schools", "CBSE schools", "universities", "vocational institutes", "NGOs",
    "property developers", "builders", "civil contractors", "surveyors", "property management companies",
    "pest control services", "water suppliers", "RO service", "AC repair", "appliance repair",
    "fabricators", "machine shops", "chemical manufacturers", "food manufacturers", "garment manufacturers",
    "packaging companies", "exporters", "importers", "agricultural suppliers", "dairy suppliers",
]


@dataclass
class AreaConfig:
    geometry: dict
    coverage: str = "deep"
    custom_categories: list[str] = field(default_factory=list)
    max_results: int = 500
    results_per_query: int = 12
    grid_spacing_km: float = 2.0
    max_queries: int = 300
    workers: int = 2
    detail_retries: int = 1
    page_delay_ms: int = 1000
    random_delay_min_ms: int = 250
    random_delay_max_ms: int = 700
    requests_per_minute: int = 45
    enrich_websites: bool = False
    headless: bool = True
    real_chrome: bool = False
    proxy: str = ""  # optional: http://user:pass@host:port or socks5://host:port


class BlockedError(RuntimeError):
    """Google served a CAPTCHA / unusual-traffic block page."""


BLOCK_CHECK_SCRIPT = r"""
() => {
  const t = (document.title || '').toLowerCase();
  const b = (document.body ? document.body.innerText.slice(0, 3000) : '').toLowerCase();
  const u = location.href.toLowerCase();
  if (u.includes('/sorry/') || u.includes('recaptcha')) return 'block-url';
  if (t.includes('unusual traffic') || b.includes('unusual traffic from your computer')) return 'unusual-traffic';
  if (b.includes("i'm not a robot") || document.querySelector('form#captcha-form, iframe[src*="recaptcha"]')) return 'captcha';
  return '';
}
"""


def check_blocked(page) -> None:
    """Raise BlockedError if the current page is a Google block/CAPTCHA page."""
    try:
        marker = page.evaluate(BLOCK_CHECK_SCRIPT)
    except Exception:
        return
    if marker:
        raise BlockedError(f"Google block detected ({marker}) — lower rate limit / change IP, then Resume")


def open_worker_session(config: AreaConfig) -> DynamicSession:
    options = dict(
        headless=config.headless,
        real_chrome=config.real_chrome,
        timeout=25000,
        network_idle=False,
        max_pages=1,
        disable_resources=True,
        extra_flags=["--disable-blink-features=AutomationControlled"],
    )
    if config.proxy:
        options["proxy"] = config.proxy
    try:
        session = DynamicSession(**options)
    except TypeError:
        # older scrapling versions without proxy kwarg
        options.pop("proxy", None)
        session = DynamicSession(**options)
    session.start()
    return session


def use_fast_navigation(page) -> None:
    """Do not wait on Google Maps' long-lived load event; page actions wait for useful UI."""
    goto = page.goto

    def goto_committed(url, **kwargs):
        kwargs.setdefault("wait_until", "commit")
        return goto(url, **kwargs)

    page.goto = goto_committed


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def polygon_points(geometry: dict) -> list[tuple[float, float]]:
    coordinates = geometry.get("coordinates", [])
    if geometry.get("type") == "Feature":
        return polygon_points(geometry.get("geometry", {}))
    if geometry.get("type") != "Polygon" or not coordinates:
        return []
    return [(float(lat), float(lon)) for lon, lat in coordinates[0]]


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    if len(polygon) < 3:
        return False
    y, x = point
    inside = False
    j = len(polygon) - 1
    for i, (yi, xi) in enumerate(polygon):
        yj, xj = polygon[j]
        if (yi > y) != (yj > y):
            crossing = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < crossing:
                inside = not inside
        j = i
    return inside


def point_in_area(point: tuple[float, float], geometry: dict) -> bool:
    if geometry.get("type") == "circle":
        center = tuple(map(float, geometry.get("center", [0, 0])))
        return haversine_km(point, center) <= float(geometry.get("radius_m", 0)) / 1000
    return point_in_polygon(point, polygon_points(geometry))


def area_bounds(geometry: dict) -> tuple[float, float, float, float]:
    if geometry.get("type") == "circle":
        lat, lon = map(float, geometry["center"])
        radius = float(geometry["radius_m"]) / 1000
        lat_delta = radius / 110.574
        lon_delta = radius / max(1, 111.320 * math.cos(math.radians(lat)))
        return lat - lat_delta, lon - lon_delta, lat + lat_delta, lon + lon_delta
    points = polygon_points(geometry)
    if not points:
        raise ValueError("A circle, rectangle, or polygon must be selected on the map.")
    lats, lons = zip(*points)
    return min(lats), min(lons), max(lats), max(lons)


def grid_points(geometry: dict, spacing_km: float, maximum: int = 120) -> list[tuple[float, float]]:
    south, west, north, east = area_bounds(geometry)
    center_lat, center_lon = (south + north) / 2, (west + east) / 2
    lat_step = max(0.001, spacing_km / 110.574)
    lon_step = max(0.001, spacing_km / max(1, 111.320 * math.cos(math.radians(center_lat))))
    estimated = max(1, math.ceil((north - south) / lat_step) + 1) * max(1, math.ceil((east - west) / lon_step) + 1)
    if estimated > maximum:
        scale = math.sqrt(estimated / maximum) * 1.03
        lat_step *= scale
        lon_step *= scale
    points: list[tuple[float, float]] = []
    lat = south
    while lat <= north + lat_step / 2:
        lon = west
        while lon <= east + lon_step / 2:
            point = (round(lat, 6), round(lon, 6))
            if point_in_area(point, geometry):
                points.append(point)
            lon += lon_step
        lat += lat_step
    center = (round(center_lat, 6), round(center_lon, 6))
    if point_in_area(center, geometry) and center not in points:
        points.insert(0, center)
    if not points:
        points = [center]
    if len(points) > maximum:
        stride = math.ceil(len(points) / maximum)
        points = points[::stride][:maximum]
    return points


def categories_for(config: AreaConfig) -> list[str]:
    if config.custom_categories:
        return list(dict.fromkeys(config.custom_categories))
    return QUICK_CATEGORIES if config.coverage == "quick" else DEEP_CATEGORIES


def lead_key(lead: dict) -> str:
    return str(lead.get("place_id") or lead.get("phone") or f"{lead.get('name')}|{lead.get('address')}").lower()


def recover_coordinates(lead: dict) -> None:
    if lead.get("latitude") is not None and lead.get("longitude") is not None:
        return
    url = str(lead.get("maps_url", ""))
    match = re.search(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)", url)
    if not match:
        match = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", url)
    if match:
        lead["latitude"], lead["longitude"] = float(match.group(1)), float(match.group(2))


def place_key_from_url(url: str) -> str:
    decoded = str(url or "")
    match = re.search(r"(0x[0-9a-f]+:0x[0-9a-f]+)", decoded, re.I)
    if match:
        return match.group(1).lower()
    match = re.search(r"!1s([^!/?]+)", decoded)
    if match:
        return match.group(1).lower()
    return decoded.split("?", 1)[0].rstrip("/").lower()


def discover_task(session: DynamicSession, config: AreaConfig, point: tuple[float, float], category: str) -> list[dict]:
    lat, lon = point
    search = SearchConfig(
        categories=[category], locations=[""], max_results=config.results_per_query,
        location_mode="coordinates", latitude=lat, longitude=lon,
        radius_km=max(0.4, config.grid_spacing_km * 0.8), headless=config.headless,
        real_chrome=config.real_chrome,
    )
    holder: dict[str, list[dict]] = {"places": []}

    def page_action(page):
        try:
            page.locator('[role="feed"], a[href*="/maps/place/"]').first.wait_for(state="attached", timeout=10000)
        except Exception:
            page.wait_for_timeout(900)
        check_blocked(page)
        raw_places = page.evaluate(COLLECT_SCRIPT, {"maxResults": config.results_per_query})
        holder["places"] = [
            {
                "place_key": place_key_from_url(place.get("href", "")),
                "maps_url": place.get("href", ""),
                "fallback_name": place.get("fallbackName", ""),
                "fallback_text": place.get("fallbackText", ""),
                "discovery_category": category,
                "grid_latitude": lat,
                "grid_longitude": lon,
            }
            for place in raw_places if place.get("href")
        ]

    session.fetch(
        build_search_url(category, search), wait=300,
        page_setup=use_fast_navigation, page_action=page_action,
    )
    return holder["places"]


def detail_place(session: DynamicSession, config: AreaConfig, place: dict,
                 logger: Callable[[str], None] = print,
                 should_cancel: Callable[[], bool] = lambda: False) -> dict:
    candidate = None
    last_error = "missing detail fields"
    for attempt in range(config.detail_retries + 1):
        if should_cancel():
            return {"status": "pending", "lead": None, "error": "canceled"}
        holder: dict[str, dict | None] = {"lead": None}

        def page_action(page):
            try:
                page.locator('h1, button[data-item-id="address"]').first.wait_for(
                    state="attached", timeout=10000 + attempt * 1500,
                )
            except Exception:
                pass
            check_blocked(page)
            page.wait_for_timeout(config.page_delay_ms + attempt * 250)
            holder["lead"] = page.evaluate(DETAIL_SCRIPT, {
                "fallbackName": place.get("fallback_name", ""),
                "fallbackText": place.get("fallback_text", ""),
                "mapsUrl": place.get("maps_url", ""),
            })

        try:
            if config.random_delay_max_ms > 0:
                max_backoff = config.random_delay_min_ms * (2 ** attempt)
                delay_ms = min(max_backoff, config.random_delay_max_ms)
                delay_ms = random.uniform(0, delay_ms)
                time.sleep(delay_ms / 1000.0)
            session.fetch(
                place["maps_url"], wait=250,
                page_setup=use_fast_navigation, page_action=page_action,
            )
            if holder["lead"] and holder["lead"].get("name"):
                candidate = holder["lead"]
                break
        except BlockedError:
            raise
        except Exception as exc:
            last_error = str(exc)[:300]
    if not candidate:
        return {"status": "failed", "lead": None, "error": last_error}
    recover_coordinates(candidate)
    if candidate.get("latitude") is None or candidate.get("longitude") is None:
        return {"status": "skipped", "lead": None, "error": "coordinates unavailable"}
    lat_c, lon_c = float(candidate["latitude"]), float(candidate["longitude"])
    if not point_in_area((lat_c, lon_c), config.geometry):
        if config.geometry.get("type") == "circle":
            center = tuple(map(float, config.geometry.get("center", [0, 0])))
            dist_km = haversine_km((lat_c, lon_c), center)
            radius_km = float(config.geometry.get("radius_m", 0)) / 1000
            error = (
                f"outside boundary: {dist_km:.2f} km from center, "
                f"boundary radius is only {radius_km*1000:.0f} m — "
                "cancel this job and draw a larger circle"
            )
        else:
            error = "outside selected polygon boundary"
        return {"status": "skipped", "lead": None, "error": error}
    candidate["category"] = candidate.get("category") or str(place.get("discovery_category", "")).title()
    candidate["discovery_category"] = place.get("discovery_category", "")
    candidate["grid_latitude"], candidate["grid_longitude"] = place.get("grid_latitude"), place.get("grid_longitude")
    if config.enrich_websites and candidate.get("website"):
        _enrich_website(candidate, logger)
    else:
        _contact_confidence(candidate)
    score, tier = _score(candidate)
    candidate.update({"lead_score": score, "lead_tier": tier})
    return {"status": "complete", "lead": candidate, "error": ""}

