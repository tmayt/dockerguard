import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import docker
import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import (
    SessionLocal,
    ContainerConfig,
    Settings,
)
import os

curl_start = os.getenv("START_NOTIFICATION_CURL", "")
curl_stop = os.getenv("STOP_NOTIFICATION_CURL", "")


def load_state():
    global CPU_THRESHOLD, RAM_THRESHOLD, CHECK_INTERVAL
    global priority_map, suspended_containers

    db = SessionLocal()

    try:
        settings = db.query(Settings).first()

        if settings:
            CPU_THRESHOLD = settings.cpu_threshold
            RAM_THRESHOLD = settings.ram_threshold
            CHECK_INTERVAL = settings.check_interval

        configs = db.query(ContainerConfig).all()

        priority_map = {
            item.name: item.priority
            for item in configs
        }

        suspended_containers = {
            item.name
            for item in configs
            if item.suspended
        }

        for name in suspended_containers.copy():
            try:
                c = docker_client.containers.get(name)

                if c.status == "running":
                    c.stop(timeout=10)

            except Exception as e:
                logger.error(f"Failed restoring suspended state for {name}: {e}")

        logger.info("✅ State loaded from SQLite")

    finally:
        db.close()


def save_container(name, priority=None, suspended=None):
    db = SessionLocal()

    try:
        obj = db.query(ContainerConfig).filter_by(name=name).first()

        if not obj:
            obj = ContainerConfig(name=name)
            db.add(obj)

        if priority is not None:
            obj.priority = priority

        if suspended is not None:
            obj.suspended = suspended

        db.commit()

    finally:
        db.close()


def save_settings():
    db = SessionLocal()

    try:
        settings = db.query(Settings).first()

        if not settings:
            settings = Settings(id=1)
            db.add(settings)

        settings.cpu_threshold = CPU_THRESHOLD
        settings.ram_threshold = RAM_THRESHOLD
        settings.check_interval = CHECK_INTERVAL

        db.commit()

    finally:
        db.close()

# ─── Config ───────────────────────────────────────────────────────────────────
CPU_THRESHOLD = float(os.getenv("CPU_THRESHOLD", "80"))      # %
RAM_THRESHOLD = float(os.getenv("RAM_THRESHOLD", "80"))      # %
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))      # seconds
LOG_FILE = Path(os.getenv("LOG_FILE", "logs/guardian.log"))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("guardian")

# ─── State ────────────────────────────────────────────────────────────────────
app = FastAPI(title="Docker Guardian")

try:
    docker_client = docker.from_env()
    docker_client.ping()
    DOCKER_AVAILABLE = True
    logger.info("✅ Docker connected successfully")
except Exception as e:
    docker_client = None
    DOCKER_AVAILABLE = False
    logger.warning(f"⚠️  Docker not available: {e} — running in demo mode")

# priority map: container_name -> priority (1=lowest, 10=highest)
priority_map: dict[str, int] = {}
suspended_containers: set[str] = set()
monitor_running = False
monitor_task: Optional[asyncio.Task] = None

# ─── Models ───────────────────────────────────────────────────────────────────
class PriorityUpdate(BaseModel):
    container_name: str
    priority: int  # 1–10

class ThresholdUpdate(BaseModel):
    cpu: Optional[float] = None
    ram: Optional[float] = None
    interval: Optional[int] = None

# ─── Docker helpers ───────────────────────────────────────────────────────────
def get_containers():
    if not DOCKER_AVAILABLE:
        return []
    try:
        return docker_client.containers.list(all=True)
    except Exception as e:
        logger.error(f"Failed to list containers: {e}")
        return []

def stop_container(name: str) -> bool:
    if not DOCKER_AVAILABLE:
        logger.info(f"[DEMO] Would stop container: {name}")
        suspended_containers.add(name)
        save_container(
            name,
            suspended=True,
        )
        return True
    try:
        c = docker_client.containers.get(name)
        c.stop(timeout=10)
        suspended_containers.add(name)
        logger.warning(f"🛑 Stopped low-priority container: {name}")
        os.system(curl_stop.replace('#####', name))
        return True
    except Exception as e:
        logger.error(f"Failed to stop {name}: {e}")
        return False

def start_container(name: str) -> bool:
    if not DOCKER_AVAILABLE:
        logger.info(f"[DEMO] Would start container: {name}")
        suspended_containers.discard(name)
        save_container(
            name,
            suspended=False,
        )
        return True
    try:
        c = docker_client.containers.get(name)
        c.start()
        suspended_containers.discard(name)
        save_container(
            name,
            suspended=False,
        )
        logger.info(f"▶️  Restarted container: {name}")
        os.system(curl_start.replace('#####', name))
        return True
    except Exception as e:
        logger.error(f"Failed to start {name}: {e}")
        return False

# ─── Monitor loop ─────────────────────────────────────────────────────────────
async def monitor_loop():
    global monitor_running
    logger.info(f"🚀 Monitor started — CPU>{CPU_THRESHOLD}% | RAM>{RAM_THRESHOLD}% | every {CHECK_INTERVAL}s")
    while monitor_running:
        cpu = psutil.cpu_percent(interval=5)
        ram = psutil.virtual_memory().percent
        overloaded = cpu > CPU_THRESHOLD or ram > RAM_THRESHOLD

        logger.info(f"📊 CPU={cpu:.1f}% RAM={ram:.1f}% {'⚠️ OVERLOADED' if overloaded else '✅ OK'}")

        containers = get_containers()

        if overloaded:
            # Sort running containers by priority (lowest first), skip already suspended
            candidates = sorted(
                [
                    c for c in containers
                    if c.status == "running"
                    and c.name not in suspended_containers
                    and priority_map.get(c.name, 5) < 5
                ],
                key=lambda c: priority_map.get(c.name, 5),
            )
            if candidates:
                target = candidates[0]
                logger.warning(
                    f"🔴 Overload detected — stopping '{target.name}' "
                    f"(priority={priority_map.get(target.name, 5)}, CPU={cpu:.1f}%, RAM={ram:.1f}%)"
                )
                stop_container(target.name)
            else:
                logger.warning(f"⚠️  Overloaded but no low-priority containers to stop")
        else:
            # Restore suspended containers (lowest priority last = start first)
            for name in list(suspended_containers):
                new_cpu = psutil.cpu_percent(interval=0.5)
                new_ram = psutil.virtual_memory().percent
                if new_cpu < CPU_THRESHOLD - 10 and new_ram < RAM_THRESHOLD - 10:
                    logger.info(f"🟢 Resources freed — restoring '{name}'")
                    start_container(name)
                else:
                    break

        await asyncio.sleep(CHECK_INTERVAL)

# ─── API routes ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global monitor_running, monitor_task
    
    load_state()

    monitor_running = True
    monitor_task = asyncio.create_task(monitor_loop())

@app.on_event("shutdown")
async def shutdown():
    global monitor_running
    monitor_running = False
    if monitor_task:
        monitor_task.cancel()

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    with open(Path(__file__).parent / "templates" / "index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/api/status")
async def get_status():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    containers = get_containers()

    container_list = []
    for c in containers:
        container_list.append({
            "name": c.name,
            "status": c.status,
            "image": c.image.tags[0] if c.image.tags else c.image.short_id,
            "priority": priority_map.get(c.name, 5),
            "suspended_by_guardian": c.name in suspended_containers,
        })

    return {
        "cpu": cpu,
        "ram": mem.percent,
        "ram_used_gb": round(mem.used / 1e9, 2),
        "ram_total_gb": round(mem.total / 1e9, 2),
        "overloaded": cpu > CPU_THRESHOLD or mem.percent > RAM_THRESHOLD,
        "thresholds": {"cpu": CPU_THRESHOLD, "ram": RAM_THRESHOLD, "interval": CHECK_INTERVAL},
        "monitor_running": monitor_running,
        "docker_available": DOCKER_AVAILABLE,
        "containers": container_list,
        "suspended_count": len(suspended_containers),
    }

@app.post("/api/priority")
async def set_priority(update: PriorityUpdate):
    if not 1 <= update.priority <= 10:
        raise HTTPException(400, "Priority must be between 1 and 10")

    priority_map[update.container_name] = update.priority

    save_container(
        update.container_name,
        priority=update.priority,
    )

    logger.info(
        f"⚙️ Priority updated: {update.container_name} → {update.priority}"
    )

    return {
        "ok": True,
        "container": update.container_name,
        "priority": update.priority,
    }
    
@app.post("/api/thresholds")
async def update_thresholds(update: ThresholdUpdate):
    global CPU_THRESHOLD, RAM_THRESHOLD, CHECK_INTERVAL, monitor_running, monitor_task
    if update.cpu is not None:
        CPU_THRESHOLD = update.cpu
    if update.ram is not None:
        RAM_THRESHOLD = update.ram
    if update.interval is not None:
        CHECK_INTERVAL = update.interval
    # Restart monitor with new settings
    monitor_running = False
    if monitor_task:
        monitor_task.cancel()
    await asyncio.sleep(0.1)
    monitor_running = True
    monitor_task = asyncio.create_task(monitor_loop())
    logger.info(f"⚙️  Thresholds updated: CPU={CPU_THRESHOLD}% RAM={RAM_THRESHOLD}% interval={CHECK_INTERVAL}s")
    save_settings()
    return {"ok": True, "cpu": CPU_THRESHOLD, "ram": RAM_THRESHOLD, "interval": CHECK_INTERVAL}

@app.post("/api/container/{name}/start")
async def manual_start(name: str):
    ok = start_container(name)
    return {"ok": ok}

@app.post("/api/container/{name}/stop")
async def manual_stop(name: str):
    ok = stop_container(name)
    return {"ok": ok}

@app.get("/api/logs")
async def get_logs(lines: int = 100):
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            all_lines = f.readlines()
        return {"lines": all_lines[-lines:]}
    except FileNotFoundError:
        return {"lines": []}

@app.get("/api/monitor/toggle")
async def toggle_monitor():
    global monitor_running, monitor_task
    if monitor_running:
        monitor_running = False
        if monitor_task:
            monitor_task.cancel()
        logger.info("⏸️  Monitor paused by user")
        return {"running": False}
    else:
        monitor_running = True
        monitor_task = asyncio.create_task(monitor_loop())
        logger.info("▶️  Monitor resumed by user")
        return {"running": True}
