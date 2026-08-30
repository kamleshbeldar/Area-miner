from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

from flask import Flask, Response, jsonify, redirect, render_template, request, url_for
from scrapling.fetchers import DynamicFetcher

import queue_store as store
from area_engine import AreaConfig, categories_for, detail_place, discover_task, grid_points, open_worker_session


BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
RUNNERS: dict[str, dict] = {}
RUNNER_LOCK = threading.RLock()

CSV_FIELDS = [
    "name", "category", "discovery_category", "lead_score", "lead_tier", "rating", "reviews",
    "phone", "phone_confidence", "website", "address", "business_hours", "price_range", "plus_code",
    "status", "latitude", "longitude", "place_id", "maps_url", "grid_latitude", "grid_longitude",
]


class SharedRateGate:
    def __init__(self, per_minute: int):
        self.limit = max(1, per_minute)
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self, canceled) -> None:
        while not canceled():
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0] >= 60:
                    self.events.popleft()
                if len(self.events) < self.limit:
                    self.events.append(now)
                    return
                delay = min(1.0, max(0.05, 60 - (now - self.events[0])))
            time.sleep(delay)


def split_values(value: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in value.replace("\n", ",").split(",") if x.strip()))


def log(job_id: str, message: str) -> None:
    store.add_log(job_id, f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


def config_from_form(geometry: dict) -> AreaConfig:
    return AreaConfig(
        geometry=geometry, coverage=request.form.get("coverage", "deep"),
        custom_categories=split_values(request.form.get("custom_categories", "")),
        max_results=max(1, min(300000, int(request.form.get("max_results", "50000")))),
        results_per_query=max(1, min(40, int(request.form.get("results_per_query", "15")))),
        grid_spacing_km=max(0.15, min(25, float(request.form.get("grid_spacing_km", "1")))),
        max_queries=max(1, min(100000, int(request.form.get("max_queries", "20000")))),
        workers=max(1, min(8, int(request.form.get("workers", "3")))),
        detail_retries=max(0, min(3, int(request.form.get("detail_retries", "1")))),
        page_delay_ms=max(600, min(5000, int(request.form.get("page_delay_ms", "900")))),
        random_delay_min_ms=max(0, min(10000, int(request.form.get("random_delay_min_ms", "150")))),
        random_delay_max_ms=max(0, min(15000, int(request.form.get("random_delay_max_ms", "500")))),
        requests_per_minute=max(1, min(240, int(request.form.get("requests_per_minute", "60")))),
        enrich_websites=request.form.get("enrich_websites") == "on",
        headless=request.form.get("headless") == "on", real_chrome=request.form.get("real_chrome") == "on",
    )


def task_generator(points: list[tuple[float, float]], categories: list[str], maximum: int):
    count = 0
    for point in points:
        for category in categories:
            if count >= maximum:
                return
            yield point[0], point[1], category
            count += 1


def worker_loop(job_id: str, worker_number: int, config: AreaConfig, stop_event: threading.Event,
                gate: SharedRateGate) -> None:
    session = None
    try:
        session = open_worker_session(config)
        log(job_id, f"Worker {worker_number} browser session ready.")
        while not stop_event.is_set() and store.job_status(job_id) == "running":
            with RUNNER_LOCK:
                current_count = RUNNERS.get(job_id, {}).get("lead_count", 0)
            if current_count >= config.max_results:
                store.skip_pending(job_id)
                stop_event.set()
                log(job_id, f"Target of {config.max_results:,} unique businesses reached.")
                break
            stage = store.job_stage(job_id)
            if stage == "discovery":
                task = store.claim_task(job_id)
                if not task:
                    if store.advance_to_details(job_id):
                        summary = store.summary(job_id)
                        log(job_id, f"Discovery complete: {summary['metrics']['places_discovered']:,} globally unique places; starting detail phase.")
                    else:
                        time.sleep(0.35)
                    continue
                gate.wait(lambda: stop_event.is_set() or store.job_status(job_id) != "running")
                if stop_event.is_set() or store.job_status(job_id) != "running":
                    store.requeue_task(task["id"])
                    break
                try:
                    places = discover_task(session, config, (task["latitude"], task["longitude"]), task["category"])
                    unique_added = store.save_places(job_id, places)
                    store.finish_task(task["id"], len(places))
                    log(job_id, f"W{worker_number} discover {task['id']}: {task['category']} | {len(places)} URLs | +{unique_added} unique")
                except Exception as exc:
                    store.finish_task(task["id"], 0, str(exc))
                    log(job_id, f"W{worker_number} discovery {task['id']} failed: {str(exc)[:180]}")
            elif stage == "details":
                place = store.claim_place(job_id)
                if not place:
                    if store.details_open(job_id):
                        time.sleep(0.35)
                        continue
                    break
                gate.wait(lambda: stop_event.is_set() or store.job_status(job_id) != "running")
                if stop_event.is_set() or store.job_status(job_id) != "running":
                    store.requeue_place(place["id"])
                    break
                try:
                    result = detail_place(
                        session, config, place,
                        lambda message: log(job_id, f"W{worker_number} {message}"),
                        lambda: stop_event.is_set() or store.job_status(job_id) != "running",
                    )
                    if result["status"] == "pending":
                        store.requeue_place(place["id"])
                        break
                    added = store.save_leads(job_id, [result["lead"]], config.max_results) if result.get("lead") else 0
                    store.finish_place(place["id"], result["status"], result.get("error", ""))
                    if added:
                        with RUNNER_LOCK:
                            if job_id in RUNNERS:
                                RUNNERS[job_id]["lead_count"] += added
                    log(job_id, f"W{worker_number} detail {place['id']}: {result['status']} | +{added} lead")
                except Exception as exc:
                    store.finish_place(place["id"], "failed", str(exc))
                    log(job_id, f"W{worker_number} detail {place['id']} failed: {str(exc)[:180]}")
            else:
                break
    except Exception as exc:
        log(job_id, f"Worker {worker_number} stopped: {str(exc)[:180]}")
    finally:
        if session:
            try:
                session.close()
            except Exception:
                pass
        runner_finished(job_id)


def runner_finished(job_id: str) -> None:
    with RUNNER_LOCK:
        runner = RUNNERS.get(job_id)
        if not runner:
            return
        runner["remaining"] -= 1
        if runner["remaining"] > 0:
            return
        current = store.job_status(job_id)
        if current == "running":
            has_open_work = store.has_open_work(job_id)
            store.set_job_status(job_id, "interrupted" if has_open_work else "complete")
            log(job_id, "Crawl interrupted with pending work preserved." if has_open_work else "Two-phase crawl complete.")
        RUNNERS.pop(job_id, None)


def launch_job(job_id: str) -> bool:
    with RUNNER_LOCK:
        if job_id in RUNNERS:
            return False
        row = store.get_job(job_id)
        if not row:
            return False
        migration = store.migrate_legacy_job(job_id)
        row = store.get_job(job_id)
        config = AreaConfig(**json.loads(row["config_json"]))
        workers = max(1, min(8, row["worker_count"]))
        stop_event = threading.Event()
        runner = {"stop": stop_event, "remaining": workers, "threads": [], "lead_count": store.lead_count(job_id)}
        RUNNERS[job_id] = runner
        store.set_job_status(job_id, "running")
        if migration["migrated"]:
            log(job_id, f"Two-phase migration seeded {migration['seeded']:,} existing leads as already detailed; no existing detail pages will be re-scraped.")
        gate = SharedRateGate(config.requests_per_minute)
        for number in range(1, workers + 1):
            thread = threading.Thread(target=worker_loop, args=(job_id, number, config, stop_event, gate), daemon=True, name=f"area-{job_id}-{number}")
            runner["threads"].append(thread)
            thread.start()
        return True


@app.get("/")
def index() -> str:
    return render_template("index.html", jobs=store.recent_jobs())


@app.post("/geocode")
def geocode() -> Response:
    place = request.form.get("place", "").strip()
    if not place:
        return jsonify({"error": "Enter a city or PIN code."}), 400
    holder = {"url": ""}
    def page_action(page):
        page.wait_for_timeout(2200); holder["url"] = page.url
    try:
        DynamicFetcher.fetch(f"https://www.google.com/maps/search/{quote_plus(place)}", headless=True, wait=700, timeout=45000, network_idle=False, page_action=page_action)
        match = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", holder["url"])
        return jsonify({"lat": float(match.group(1)), "lng": float(match.group(2)), "label": place}) if match else (jsonify({"error": "Location not found."}), 404)
    except Exception as exc:
        return jsonify({"error": str(exc)[:180]}), 502


@app.post("/estimate")
def estimate() -> Response:
    try:
        geometry = json.loads(request.form.get("geometry", "{}"))
        config = AreaConfig(geometry=geometry, coverage=request.form.get("coverage", "deep"), custom_categories=split_values(request.form.get("custom_categories", "")),
                            grid_spacing_km=float(request.form.get("grid_spacing_km", "1")), max_queries=int(request.form.get("max_queries", "20000")))
        categories = categories_for(config)
        max_points = min(5000, max(1, math.ceil(config.max_queries / max(1, len(categories)))))
        points = grid_points(geometry, config.grid_spacing_km, maximum=max_points)
        return jsonify({"grid_points": len(points), "categories": len(categories), "queries": min(config.max_queries, len(points) * len(categories))})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/start")
def start() -> Response:
    try:
        geometry = json.loads(request.form.get("geometry", "{}"))
        config = config_from_form(geometry)
        categories = categories_for(config)
        max_points = min(5000, max(1, math.ceil(config.max_queries / max(1, len(categories)))))
        points = grid_points(geometry, config.grid_spacing_km, maximum=max_points)
        total_tasks = min(config.max_queries, len(points) * len(categories))
        if total_tasks > 100000:
            raise ValueError("Maximum 100,000 crawl tasks are allowed per run.")
    except Exception as exc:
        return f"Invalid area/settings: {exc}", 400
    job_id = uuid4().hex[:12]
    store.create_job(job_id, config.__dict__, geometry, task_generator(points, categories, total_tasks), total_tasks, config.max_results, config.workers)
    log(job_id, f"Created {total_tasks:,} persistent tasks across {len(points):,} grid points and {len(categories)} categories.")
    launch_job(job_id)
    return redirect(url_for("progress", job_id=job_id))


@app.get("/progress/<job_id>")
def progress(job_id: str):
    job = store.summary(job_id)
    return render_template("progress.html", job=job) if job else ("Job not found", 404)


@app.get("/api/jobs/<job_id>")
def api_job(job_id: str) -> Response:
    data = store.summary(job_id)
    return jsonify(data) if data else (jsonify({"error": "Job not found"}), 404)


@app.post("/cancel/<job_id>")
def cancel(job_id: str) -> Response:
    store.set_job_status(job_id, "canceled")
    with RUNNER_LOCK:
        if job_id in RUNNERS:
            RUNNERS[job_id]["stop"].set()
    log(job_id, "Cancel requested; pending tasks remain resumable.")
    return jsonify({"status": "canceled"})


@app.post("/resume/<job_id>")
def resume(job_id: str) -> Response:
    if not store.get_job(job_id):
        return "Job not found", 404
    if launch_job(job_id):
        log(job_id, "Resumed from persistent task queue.")
    else:
        log(job_id, "Resume ignored because workers are already active.")
    return redirect(url_for("progress", job_id=job_id))


@app.post("/retry-failed/<job_id>")
def retry_failed(job_id: str) -> Response:
    counts = store.retry_failed(job_id)
    log(job_id, f"Retry queued: {counts['tasks']} discovery tasks and {counts['places']} business details.")
    if not launch_job(job_id):
        log(job_id, "Failed tasks queued; active workers will pick them up.")
    return redirect(url_for("progress", job_id=job_id))


def csv_value(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value


@app.get("/download/<job_id>/csv")
def download_csv(job_id: str) -> Response:
    if not store.get_job(job_id):
        return "Job not found", 404
    def generate():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader(); yield "\ufeff" + buffer.getvalue(); buffer.seek(0); buffer.truncate(0)
        for lead in store.iter_leads(job_id):
            writer.writerow({field: csv_value(lead.get(field, "")) for field in CSV_FIELDS})
            yield buffer.getvalue(); buffer.seek(0); buffer.truncate(0)
    return Response(generate(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={job_id}_area_businesses.csv"})


@app.get("/download/<job_id>/jsonl")
def download_jsonl(job_id: str) -> Response:
    if not store.get_job(job_id):
        return "Job not found", 404
    return Response((json.dumps(lead, ensure_ascii=False) + "\n" for lead in store.iter_leads(job_id)), mimetype="application/x-ndjson",
                    headers={"Content-Disposition": f"attachment; filename={job_id}_area_businesses.jsonl"})


store.init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5052, debug=False, threaded=True)
