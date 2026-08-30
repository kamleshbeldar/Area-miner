from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import unquote, urlsplit


DB_PATH = Path(__file__).resolve().parent / "area_scale.db"


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=60, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=60000")
    return connection


def _ensure_column(connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db(recover: bool = True) -> None:
    with connect() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS area_jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'discovery',
                pipeline_version INTEGER NOT NULL DEFAULT 2, config_json TEXT NOT NULL,
                geometry_json TEXT NOT NULL, total_tasks INTEGER NOT NULL DEFAULT 0,
                max_results INTEGER NOT NULL, worker_count INTEGER NOT NULL,
                error TEXT DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS area_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                latitude REAL NOT NULL, longitude REAL NOT NULL, category TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                found_count INTEGER NOT NULL DEFAULT 0, last_error TEXT DEFAULT '',
                created_at REAL NOT NULL, started_at REAL, completed_at REAL,
                UNIQUE(job_id, latitude, longitude, category)
            );
            CREATE INDEX IF NOT EXISTS idx_area_tasks_claim ON area_tasks(job_id,status,id);
            CREATE TABLE IF NOT EXISTS area_places (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                unique_key TEXT NOT NULL, maps_url TEXT NOT NULL, fallback_name TEXT DEFAULT '',
                fallback_text TEXT DEFAULT '', discovery_category TEXT DEFAULT '',
                grid_latitude REAL, grid_longitude REAL, status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT DEFAULT '',
                created_at REAL NOT NULL, started_at REAL, completed_at REAL,
                UNIQUE(job_id, unique_key)
            );
            CREATE INDEX IF NOT EXISTS idx_area_places_claim ON area_places(job_id,status,id);
            CREATE TABLE IF NOT EXISTS area_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                unique_key TEXT NOT NULL, category TEXT NOT NULL, data_json TEXT NOT NULL,
                created_at REAL NOT NULL, UNIQUE(job_id, unique_key)
            );
            CREATE INDEX IF NOT EXISTS idx_area_leads_job ON area_leads(job_id,id);
            CREATE TABLE IF NOT EXISTS area_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                message TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_area_logs_job ON area_logs(job_id,id);
        """)
        _ensure_column(connection, "area_jobs", "stage", "TEXT NOT NULL DEFAULT 'discovery'")
        _ensure_column(connection, "area_jobs", "pipeline_version", "INTEGER NOT NULL DEFAULT 1")
        # Add performance indexes
        connection.execute("CREATE INDEX IF NOT EXISTS idx_area_tasks_job_status ON area_tasks(job_id, status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_area_places_job_status ON area_places(job_id, status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_area_leads_job ON area_leads(job_id)")
        if recover:
            connection.execute("UPDATE area_tasks SET status='pending',started_at=NULL WHERE status='running'")
            connection.execute("UPDATE area_places SET status='pending',started_at=NULL WHERE status='running'")
            connection.execute("UPDATE area_jobs SET status='interrupted',updated_at=? WHERE status IN ('running','queued')", (time.time(),))


def canonical_place_key(url: str, place_id: str = "") -> str:
    if place_id:
        return place_id.strip().lower()
    decoded = unquote(str(url or ""))
    hex_match = re.search(r"(0x[0-9a-f]+:0x[0-9a-f]+)", decoded, re.I)
    if hex_match:
        return hex_match.group(1).lower()
    one_s = re.search(r"!1s([^!/?]+)", decoded)
    if one_s:
        return one_s.group(1).lower()
    split = urlsplit(decoded)
    return (split.path.rstrip("/") or decoded).lower()


def create_job(job_id: str, config: dict, geometry: dict, tasks: Iterable[tuple[float, float, str]],
               total_tasks: int, max_results: int, workers: int) -> None:
    now = time.time()
    with connect() as connection:
        connection.execute(
            "INSERT INTO area_jobs(id,status,stage,pipeline_version,config_json,geometry_json,total_tasks,max_results,worker_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, "queued", "discovery", 2, json.dumps(config), json.dumps(geometry), total_tasks, max_results, workers, now, now),
        )
        batch: list[tuple] = []
        for latitude, longitude, category in tasks:
            batch.append((job_id, latitude, longitude, category, "pending", now))
            if len(batch) >= 2000:
                connection.executemany("INSERT OR IGNORE INTO area_tasks(job_id,latitude,longitude,category,status,created_at) VALUES(?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            connection.executemany("INSERT OR IGNORE INTO area_tasks(job_id,latitude,longitude,category,status,created_at) VALUES(?,?,?,?,?,?)", batch)


def migrate_legacy_job(job_id: str) -> dict:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute("SELECT pipeline_version FROM area_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            connection.rollback()
            raise ValueError("Job not found")
        if job["pipeline_version"] >= 2:
            connection.commit()
            seeded = connection.execute("SELECT COUNT(*) FROM area_places WHERE job_id=? AND status='complete'", (job_id,)).fetchone()[0]
            return {"migrated": False, "seeded": seeded}
        rows = connection.execute("SELECT data_json FROM area_leads WHERE job_id=?", (job_id,)).fetchall()
        seeded = 0
        now = time.time()
        for row in rows:
            lead = json.loads(row["data_json"])
            url = str(lead.get("maps_url", ""))
            key = canonical_place_key(url, str(lead.get("place_id", "")))
            cursor = connection.execute(
                "INSERT OR IGNORE INTO area_places(job_id,unique_key,maps_url,fallback_name,discovery_category,grid_latitude,grid_longitude,status,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, key, url, lead.get("name", ""), lead.get("discovery_category", ""),
                 lead.get("grid_latitude"), lead.get("grid_longitude"), "complete", now, now),
            )
            seeded += cursor.rowcount
        connection.execute("UPDATE area_tasks SET status='pending',attempts=0,found_count=0,last_error='',started_at=NULL,completed_at=NULL WHERE job_id=?", (job_id,))
        connection.execute("UPDATE area_jobs SET pipeline_version=2,stage='discovery',status='interrupted',updated_at=? WHERE id=?", (now, job_id))
        connection.commit()
        return {"migrated": True, "seeded": seeded}
    finally:
        connection.close()


def get_job(job_id: str) -> dict | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM area_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def recent_jobs(limit: int = 12) -> list[dict]:
    with connect() as connection:
        rows = connection.execute("SELECT * FROM area_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def set_job_status(job_id: str, status: str, error: str = "") -> None:
    with connect() as connection:
        connection.execute("UPDATE area_jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, time.time(), job_id))


def set_stage(job_id: str, stage: str) -> None:
    with connect() as connection:
        connection.execute("UPDATE area_jobs SET stage=?,updated_at=? WHERE id=?", (stage, time.time(), job_id))


def job_status(job_id: str) -> str:
    with connect() as connection:
        row = connection.execute("SELECT status FROM area_jobs WHERE id=?", (job_id,)).fetchone()
    return row["status"] if row else "missing"


def job_stage(job_id: str) -> str:
    with connect() as connection:
        row = connection.execute("SELECT stage FROM area_jobs WHERE id=?", (job_id,)).fetchone()
    return row["stage"] if row else "missing"


def claim_task(job_id: str) -> dict | None:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM area_tasks WHERE job_id=? AND status='pending' ORDER BY id LIMIT 1", (job_id,)).fetchone()
        if not row:
            connection.commit()
            return None
        connection.execute("UPDATE area_tasks SET status='running',attempts=attempts+1,started_at=? WHERE id=?", (time.time(), row["id"]))
        connection.commit()
        return dict(row)
    finally:
        connection.close()


def finish_task(task_id: int, found_count: int, error: str = "") -> None:
    status = "failed" if error else "complete"
    with connect() as connection:
        connection.execute("UPDATE area_tasks SET status=?,found_count=?,last_error=?,completed_at=? WHERE id=?", (status, found_count, error[:500], time.time(), task_id))


def requeue_task(task_id: int) -> None:
    with connect() as connection:
        connection.execute("UPDATE area_tasks SET status='pending',started_at=NULL WHERE id=?", (task_id,))


def discovery_open(job_id: str) -> bool:
    with connect() as connection:
        return bool(connection.execute("SELECT 1 FROM area_tasks WHERE job_id=? AND status IN ('pending','running') LIMIT 1", (job_id,)).fetchone())


def advance_to_details(job_id: str) -> bool:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        open_task = connection.execute("SELECT 1 FROM area_tasks WHERE job_id=? AND status IN ('pending','running') LIMIT 1", (job_id,)).fetchone()
        if open_task:
            connection.commit()
            return False
        connection.execute("UPDATE area_jobs SET stage='details',updated_at=? WHERE id=? AND stage='discovery'", (time.time(), job_id))
        changed = connection.total_changes > 0
        connection.commit()
        return changed
    finally:
        connection.close()


def save_places(job_id: str, places: list[dict]) -> int:
    if not places:
        return 0
    now = time.time()
    rows = []
    for place in places:
        url = str(place.get("maps_url") or place.get("href") or "")
        key = str(place.get("place_key") or canonical_place_key(url))
        rows.append((job_id, key, url, place.get("fallback_name", ""), place.get("fallback_text", ""),
                     place.get("discovery_category", ""), place.get("grid_latitude"), place.get("grid_longitude"), "pending", now))
    with connect() as connection:
        before = connection.total_changes
        connection.executemany(
            "INSERT OR IGNORE INTO area_places(job_id,unique_key,maps_url,fallback_name,fallback_text,discovery_category,grid_latitude,grid_longitude,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        return connection.total_changes - before


def claim_place(job_id: str) -> dict | None:
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM area_places WHERE job_id=? AND status='pending' ORDER BY id LIMIT 1", (job_id,)).fetchone()
        if not row:
            connection.commit()
            return None
        connection.execute("UPDATE area_places SET status='running',attempts=attempts+1,started_at=? WHERE id=?", (time.time(), row["id"]))
        connection.commit()
        return dict(row)
    finally:
        connection.close()


def finish_place(place_id: int, status: str = "complete", error: str = "") -> None:
    with connect() as connection:
        connection.execute("UPDATE area_places SET status=?,last_error=?,completed_at=? WHERE id=?", (status, error[:500], time.time(), place_id))


def requeue_place(place_id: int) -> None:
    with connect() as connection:
        connection.execute("UPDATE area_places SET status='pending',started_at=NULL WHERE id=?", (place_id,))


def details_open(job_id: str) -> bool:
    with connect() as connection:
        return bool(connection.execute("SELECT 1 FROM area_places WHERE job_id=? AND status IN ('pending','running') LIMIT 1", (job_id,)).fetchone())


def retry_failed(job_id: str) -> dict:
    with connect() as connection:
        tasks = connection.execute("UPDATE area_tasks SET status='pending',last_error='',started_at=NULL,completed_at=NULL WHERE job_id=? AND status='failed'", (job_id,)).rowcount
        places = connection.execute("UPDATE area_places SET status='pending',last_error='',started_at=NULL,completed_at=NULL WHERE job_id=? AND status='failed'", (job_id,)).rowcount
        return {"tasks": tasks, "places": places}


def skip_pending(job_id: str) -> int:
    with connect() as connection:
        tasks = connection.execute("UPDATE area_tasks SET status='skipped',completed_at=? WHERE job_id=? AND status='pending'", (time.time(), job_id)).rowcount
        places = connection.execute("UPDATE area_places SET status='skipped',completed_at=? WHERE job_id=? AND status='pending'", (time.time(), job_id)).rowcount
        return tasks + places


def lead_key(lead: dict) -> str:
    return str(lead.get("place_id") or lead.get("phone") or lead.get("website") or f"{lead.get('name')}|{lead.get('address')}").strip().lower()


def save_lead(job_id: str, lead: dict) -> bool:
    return save_leads(job_id, [lead]) == 1


def save_leads(job_id: str, leads: list[dict], maximum: int | None = None) -> int:
    if not leads:
        return 0
    rows = []
    now = time.time()
    for lead in leads:
        category = str(lead.get("category") or lead.get("discovery_category") or "Uncategorized")
        lead["category"] = category
        rows.append((job_id, lead_key(lead), category, json.dumps(lead, ensure_ascii=False), now))
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if maximum is not None:
            existing = connection.execute("SELECT COUNT(*) FROM area_leads WHERE job_id=?", (job_id,)).fetchone()[0]
            rows = rows[:max(0, maximum - existing)]
        if not rows:
            return 0
        before = connection.total_changes
        connection.executemany("INSERT OR IGNORE INTO area_leads(job_id,unique_key,category,data_json,created_at) VALUES(?,?,?,?,?)", rows)
        return connection.total_changes - before


def lead_count(job_id: str) -> int:
    with connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM area_leads WHERE job_id=?", (job_id,)).fetchone()[0]


def add_log(job_id: str, message: str) -> None:
    with connect() as connection:
        cursor = connection.execute("INSERT INTO area_logs(job_id,message,created_at) VALUES(?,?,?)", (job_id, message, time.time()))
        if cursor.lastrowid % 100 == 0:
            connection.execute("DELETE FROM area_logs WHERE job_id=? AND id NOT IN (SELECT id FROM area_logs WHERE job_id=? ORDER BY id DESC LIMIT 600)", (job_id, job_id))


def _counts(connection: sqlite3.Connection, table: str, job_id: str) -> dict[str, int]:
    return {row["status"]: row["count"] for row in connection.execute(f"SELECT status,COUNT(*) AS count FROM {table} WHERE job_id=? GROUP BY status", (job_id,))}


def summary(job_id: str) -> dict | None:
    with connect() as connection:
        job = connection.execute("SELECT * FROM area_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return None
        task_counts = _counts(connection, "area_tasks", job_id)
        place_counts = _counts(connection, "area_places", job_id)
        businesses = connection.execute("SELECT COUNT(*) FROM area_leads WHERE job_id=?", (job_id,)).fetchone()[0]
        logs = [row["message"] for row in connection.execute("SELECT message FROM area_logs WHERE job_id=? ORDER BY id DESC LIMIT 180", (job_id,)).fetchall()][::-1]
        latest_rows = connection.execute("SELECT id,data_json FROM area_leads WHERE job_id=? ORDER BY id DESC LIMIT 50", (job_id,)).fetchall()
    leads = []
    for row in latest_rows:
        item = json.loads(row["data_json"])
        item["_id"] = row["id"]
        leads.append(item)
    discovery_done = task_counts.get("complete", 0) + task_counts.get("failed", 0) + task_counts.get("skipped", 0)
    details_done = place_counts.get("complete", 0) + place_counts.get("failed", 0) + place_counts.get("skipped", 0)
    return {
        "id": job["id"], "status": job["status"], "stage": job["stage"], "error": job["error"],
        "metrics": {
            "businesses": businesses,
            "discovery_total": job["total_tasks"], "discovery_completed": discovery_done,
            "discovery_pending": task_counts.get("pending", 0), "discovery_running": task_counts.get("running", 0),
            "discovery_failures": task_counts.get("failed", 0),
            "places_discovered": sum(place_counts.values()), "details_completed": details_done,
            "details_pending": place_counts.get("pending", 0), "details_running": place_counts.get("running", 0),
            "detail_failures": place_counts.get("failed", 0),
            "total_tasks": job["total_tasks"], "completed_tasks": discovery_done,
            "pending_tasks": task_counts.get("pending", 0), "running_tasks": task_counts.get("running", 0),
            "failures": task_counts.get("failed", 0) + place_counts.get("failed", 0),
        },
        "logs": logs, "leads": leads, "created_at": job["created_at"],
    }


def iter_leads(job_id: str, batch_size: int = 1000) -> Iterator[dict]:
    connection = connect()
    try:
        cursor = connection.execute("SELECT data_json FROM area_leads WHERE job_id=? ORDER BY id", (job_id,))
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            for row in rows:
                yield json.loads(row["data_json"])
    finally:
        connection.close()


def has_open_work(job_id: str) -> bool:
    return discovery_open(job_id) or details_open(job_id)
