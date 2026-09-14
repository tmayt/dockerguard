import asyncio
import json
import logging
import os
import platform
import socket
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import docker
import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from database import load_containers, load_settings, upsert_container, upsert_settings

curl_start = os.getenv("START_NOTIFICATION_CURL", "")
curl_stop = os.getenv("STOP_NOTIFICATION_CURL", "")

METRICS_INTERVAL = int(os.getenv("METRICS_INTERVAL", "10"))
CONTAINER_STATS_EVERY = int(os.getenv("CONTAINER_STATS_EVERY", "2"))
PRIORITY_MIN = 1
PRIORITY_MAX = 10
DEFAULT_PRIORITY = 5
LOG_TAIL_MAX = 200

# ─── Config ───────────────────────────────────────────────────────────────────
CPU_THRESHOLD = float(os.getenv("CPU_THRESHOLD", "80"))
RAM_THRESHOLD = float(os.getenv("RAM_THRESHOLD", "80"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
CPU_HIGH_STREAK_REQUIRED = int(os.getenv("CPU_HIGH_STREAK", "3"))
LOG_FILE = Path(os.getenv("LOG_FILE", "logs/guardian.log"))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────
logger = logging.getLogger("guardian")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.propagate = False
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_file = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
_file.setFormatter(_fmt)
_stream = logging.StreamHandler()
_stream.setFormatter(_fmt)
logger.addHandler(_file)
logger.addHandler(_stream)

# ─── State ────────────────────────────────────────────────────────────────────
app = FastAPI(title="Docker Guardian")
DASHBOARD_HTML = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")

try:
    docker_client = docker.from_env()
    docker_client.ping()
    DOCKER_AVAILABLE = True
    logger.info("✅ Docker connected successfully")
except Exception as e:
    docker_client = None
    DOCKER_AVAILABLE = False
    logger.warning(f"⚠️  Docker not available: {e} — running in demo mode")

priority_map: dict[str, int] = {}
suspended_containers: set[str] = set()
monitor_running = False
monitor_task: Optional[asyncio.Task] = None
metrics_task: Optional[asyncio.Task] = None
cpu_high_streak = 0
resource_saturated = False
cached_containers: list[dict] = []
_prev_cpu: dict[str, tuple[int, int]] = {}
subscribers: set[asyncio.Queue] = set()
RAM_TOTAL_GB = round(psutil.virtual_memory().total / 1e9, 2)
last_metrics = {
    "cpu": 0.0,
    "ram": 0.0,
    "ram_used_gb": 0.0,
    "ram_total_gb": RAM_TOTAL_GB,
}
_stats_tick = 0
_cached_logs: list[str] = []
_cached_logs_at = 0.0
LOG_CACHE_TTL = 8.0


def _detect_primary_ip() -> str:
    env_ip = (os.getenv("SERVER_IP") or "").strip()
    if env_ip:
        return env_ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "—"


def _detect_os_label() -> str:
    try:
        path = Path("/etc/os-release")
        if path.exists():
            data = {}
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k] = v.strip().strip('"')
            return data.get("PRETTY_NAME") or data.get("NAME") or platform.system()
    except Exception:
        pass
    return f"{platform.system()} {platform.release()}".strip()


SERVER_INFO = {
    "hostname": platform.node() or "—",
    "ip": _detect_primary_ip(),
    "os": _detect_os_label(),
    "kernel": platform.release() or "—",
    "arch": platform.machine() or "—",
    "cpu_cores": os.cpu_count() or 1,
    "ram_total_gb": RAM_TOTAL_GB,
    "boot_time": int(psutil.boot_time()),
}


def load_state():
    global CPU_THRESHOLD, RAM_THRESHOLD, CHECK_INTERVAL
    global priority_map, suspended_containers

    settings = load_settings()
    if settings:
        CPU_THRESHOLD = settings["cpu_threshold"]
        RAM_THRESHOLD = settings["ram_threshold"]
        CHECK_INTERVAL = settings["check_interval"]

    configs = load_containers()
    priority_map = {item["name"]: int(item["priority"]) for item in configs}
    suspended_containers = {item["name"] for item in configs if item["suspended"]}

    for name in tuple(suspended_containers):
        try:
            c = docker_client.containers.get(name) if DOCKER_AVAILABLE else None
            if c is not None and c.status == "running":
                c.stop(timeout=10)
        except Exception as e:
            logger.error(f"Failed restoring suspended state for {name}: {e}")

    logger.info("✅ State loaded from SQLite")


def save_container(name, priority=None, suspended=None):
    upsert_container(name, priority=priority, suspended=suspended)


def save_settings():
    upsert_settings(CPU_THRESHOLD, RAM_THRESHOLD, CHECK_INTERVAL)


def as_priority(value) -> int:
    try:
        priority = int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "Priority must be a number")
    if not PRIORITY_MIN <= priority <= PRIORITY_MAX:
        raise HTTPException(400, f"Priority must be between {PRIORITY_MIN} and {PRIORITY_MAX}")
    return priority


def notify(template: str, name: str):
    if not template:
        return
    try:
        subprocess.Popen(
            template.replace("#####", name),
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        logger.error(f"Notification failed: {e}")


# ─── Models ───────────────────────────────────────────────────────────────────
class PriorityUpdate(BaseModel):
    container_name: str
    priority: int = Field(..., ge=PRIORITY_MIN, le=PRIORITY_MAX)


class ThresholdUpdate(BaseModel):
    cpu: Optional[float] = None
    ram: Optional[float] = None
    interval: Optional[int] = None

# ─── Docker helpers ───────────────────────────────────────────────────────────
def collect_container_info() -> list[dict]:
    if not DOCKER_AVAILABLE:
        return []
    try:
        raw = docker_client.api.containers(all=True)
    except Exception as e:
        logger.error(f"Failed to list containers: {e}")
        return []
    prev_by_name = {c["name"]: c for c in cached_containers}
    out = []
    for item in raw:
        names = item.get("Names") or []
        if names:
            name = names[0][1:] if names[0].startswith("/") else names[0]
        else:
            name = (item.get("Id") or "")[:12]
        state = item.get("State") or ""
        if isinstance(state, dict):
            state = state.get("Status") or ""
        prev = prev_by_name.get(name) or {}
        out.append({
            "id": item.get("Id") or "",
            "name": name,
            "status": state,
            "image": item.get("Image") or "",
            "priority": int(priority_map.get(name, DEFAULT_PRIORITY)),
            "suspended_by_guardian": name in suspended_containers,
            "cpu": None if state != "running" else prev.get("cpu"),
            "ram_mb": None if state != "running" else prev.get("ram_mb"),
        })
    return out


def _cpu_percent(cpu_stats: dict, name: str) -> float:
    usage = cpu_stats.get("cpu_usage") or {}
    total = int(usage.get("total_usage") or 0)
    system = int(cpu_stats.get("system_cpu_usage") or 0)
    online = cpu_stats.get("online_cpus")
    if not online:
        online = len(usage.get("percpu_usage") or []) or (os.cpu_count() or 1)
    prev = _prev_cpu.get(name)
    _prev_cpu[name] = (total, system)
    if not prev:
        return None
    cpu_delta = total - prev[0]
    sys_delta = system - prev[1]
    if cpu_delta > 0 and sys_delta > 0:
        return round((cpu_delta / sys_delta) * online * 100.0, 1)
    return 0.0


def _mem_usage_bytes(memory_stats: dict) -> int:
    usage = int(memory_stats.get("usage") or 0)
    stats = memory_stats.get("stats") or {}
    cache = stats.get("total_inactive_file")
    if cache is None:
        cache = stats.get("inactive_file")
    if cache is None:
        cache = stats.get("cache")
    if cache:
        usage = max(0, usage - int(cache))
    return usage


def sample_container_stats(containers: list[dict]) -> dict:
    if not DOCKER_AVAILABLE:
        return {c["name"]: {"cpu": None, "ram_mb": None} for c in containers if c.get("name")}
    usage = {}
    live = set()
    for c in containers:
        name = c.get("name")
        if not name:
            continue
        if c.get("status") != "running" or not c.get("id"):
            usage[name] = {"cpu": None, "ram_mb": None}
            continue
        live.add(name)
        try:
            st = docker_client.api.stats(c["id"], stream=False, one_shot=True)
        except Exception:
            usage[name] = {"cpu": c.get("cpu"), "ram_mb": c.get("ram_mb")}
            continue
        ram_mb = round(_mem_usage_bytes(st.get("memory_stats") or {}) / (1024 * 1024), 1)
        usage[name] = {
            "cpu": _cpu_percent(st.get("cpu_stats") or {}, name),
            "ram_mb": ram_mb,
        }
    for stale in list(_prev_cpu):
        if stale not in live:
            _prev_cpu.pop(stale, None)
    return usage


def apply_usage(usage: dict):
    for c in cached_containers:
        u = usage.get(c["name"])
        if not u:
            if c.get("status") != "running":
                c["cpu"] = None
                c["ram_mb"] = None
            continue
        c["cpu"] = u.get("cpu")
        c["ram_mb"] = u.get("ram_mb")


def usage_map() -> dict:
    return {
        c["name"]: {"cpu": c.get("cpu"), "ram_mb": c.get("ram_mb")}
        for c in cached_containers
    }


def stop_container(name: str) -> bool:
    if not DOCKER_AVAILABLE:
        logger.info(f"[DEMO] Would stop container: {name}")
        suspended_containers.add(name)
        save_container(name, suspended=True)
        return True
    try:
        c = docker_client.containers.get(name)
        c.stop(timeout=10)
        suspended_containers.add(name)
        save_container(name, suspended=True)
        logger.warning(f"🛑 Stopped low-priority container: {name}")
        notify(curl_stop, name)
        return True
    except Exception as e:
        logger.error(f"Failed to stop {name}: {e}")
        return False


def start_container(name: str) -> bool:
    if not DOCKER_AVAILABLE:
        logger.info(f"[DEMO] Would start container: {name}")
        suspended_containers.discard(name)
        save_container(name, suspended=False)
        return True
    try:
        c = docker_client.containers.get(name)
        c.start()
        suspended_containers.discard(name)
        save_container(name, suspended=False)
        logger.info(f"▶️  Restarted container: {name}")
        notify(curl_start, name)
        return True
    except Exception as e:
        logger.error(f"Failed to start {name}: {e}")
        return False


def sample_metrics():
    mem = psutil.virtual_memory()
    last_metrics["cpu"] = psutil.cpu_percent(None)
    last_metrics["ram"] = mem.percent
    last_metrics["ram_used_gb"] = round(mem.used / 1e9, 2)
    last_metrics["ram_total_gb"] = RAM_TOTAL_GB


def metrics_payload() -> dict:
    return {
        "cpu": last_metrics["cpu"],
        "ram": last_metrics["ram"],
        "ram_used_gb": last_metrics["ram_used_gb"],
        "ram_total_gb": last_metrics["ram_total_gb"],
        "overloaded": resource_saturated,
        "cpu_high_streak": cpu_high_streak,
        "cpu_high_streak_required": CPU_HIGH_STREAK_REQUIRED,
        "monitor_running": monitor_running,
        "usage": usage_map(),
        "server": {
            **SERVER_INFO,
            "uptime_sec": max(0, int(time.time() - SERVER_INFO["boot_time"])),
        },
    }


def cached_tail_logs(n: int = 40) -> list[str]:
    global _cached_logs, _cached_logs_at
    now = time.monotonic()
    if now - _cached_logs_at < LOG_CACHE_TTL and _cached_logs:
        return _cached_logs[-n:]
    _cached_logs = tail_lines(LOG_FILE, n)
    _cached_logs_at = now
    return _cached_logs


def full_payload() -> dict:
    payload = metrics_payload()
    payload.update({
        "thresholds": {"cpu": CPU_THRESHOLD, "ram": RAM_THRESHOLD, "interval": CHECK_INTERVAL},
        "docker_available": DOCKER_AVAILABLE,
        "containers": [{k: v for k, v in c.items() if k != "id"} for c in cached_containers],
        "suspended_count": len(suspended_containers),
        "logs": cached_tail_logs(40),
    })
    return payload


def publish(event: str, payload: dict):
    if not subscribers:
        return
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    for q in tuple(subscribers):
        try:
            if q.full():
                q.get_nowait()
            q.put_nowait((event, blob))
        except Exception:
            pass


def publish_snapshot():
    if subscribers:
        publish("snapshot", full_payload())


async def refresh_containers_and_publish():
    global cached_containers
    cached_containers = await asyncio.to_thread(collect_container_info)
    publish_snapshot()


def tail_lines(path: Path, n: int) -> list[str]:
    n = max(1, min(int(n), LOG_TAIL_MAX))
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return []
            data = b""
            block = 4096
            needed = n
            while f.tell() > 0:
                step = min(block, f.tell())
                f.seek(-step, os.SEEK_CUR)
                data = f.read(step) + data
                f.seek(-step, os.SEEK_CUR)
                if data.count(b"\n") >= needed or f.tell() == 0:
                    break
            return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except FileNotFoundError:
        return []


# ─── Monitor loops ────────────────────────────────────────────────────────────
async def metrics_loop():
    global _stats_tick
    psutil.cpu_percent(None)
    await asyncio.sleep(1)
    while True:
        has_viewers = bool(subscribers)
        # Host CPU/RAM via psutil is cheap and keeps the monitor accurate.
        sample_metrics()
        if has_viewers:
            # Docker stats() is expensive — throttle and skip when nobody is watching.
            if _stats_tick % max(1, CONTAINER_STATS_EVERY) == 0:
                usage = await asyncio.to_thread(sample_container_stats, list(cached_containers))
                apply_usage(usage)
            _stats_tick += 1
            publish("metrics", metrics_payload())
        else:
            _stats_tick = 0
        await asyncio.sleep(max(5, METRICS_INTERVAL))


async def monitor_loop():
    global cpu_high_streak, resource_saturated, cached_containers
    cpu_high_streak = 0
    resource_saturated = False
    logger.info(
        f"🚀 Monitor started — CPU>{CPU_THRESHOLD}% for {CPU_HIGH_STREAK_REQUIRED} intervals "
        f"| RAM>{RAM_THRESHOLD}% | every {CHECK_INTERVAL}s"
    )
    await asyncio.sleep(METRICS_INTERVAL)
    while monitor_running:
        cpu = last_metrics["cpu"]
        ram = last_metrics["ram"]

        if cpu > CPU_THRESHOLD:
            cpu_high_streak += 1
        else:
            cpu_high_streak = 0

        cpu_saturated = cpu_high_streak >= CPU_HIGH_STREAK_REQUIRED
        ram_saturated = ram > RAM_THRESHOLD
        overloaded = cpu_saturated or ram_saturated
        resource_saturated = overloaded

        if cpu > CPU_THRESHOLD and not cpu_saturated:
            logger.info(
                f"📊 CPU={cpu:.1f}% RAM={ram:.1f}% "
                f"⏳ CPU high {cpu_high_streak}/{CPU_HIGH_STREAK_REQUIRED} consecutive intervals"
            )
        elif overloaded:
            logger.info(f"📊 CPU={cpu:.1f}% RAM={ram:.1f}% ⚠️ OVERLOADED")

        containers = await asyncio.to_thread(collect_container_info)
        mutated = False

        if overloaded:
            candidates = sorted(
                [
                    c for c in containers
                    if c["status"] == "running"
                    and c["name"] not in suspended_containers
                    and c["priority"] < 5
                ],
                key=lambda c: c["priority"],
            )
            if candidates:
                target = candidates[0]
                logger.warning(
                    f"🔴 Overload detected — stopping '{target['name']}' "
                    f"(priority={target['priority']}, CPU={cpu:.1f}%, RAM={ram:.1f}%)"
                )
                await asyncio.to_thread(stop_container, target["name"])
                mutated = True
            else:
                logger.warning("⚠️  Overloaded but no low-priority containers to stop")
        elif suspended_containers and cpu < CPU_THRESHOLD - 10 and ram < RAM_THRESHOLD - 10:
            name = max(
                suspended_containers,
                key=lambda n: int(priority_map.get(n, DEFAULT_PRIORITY)),
            )
            logger.info(f"🟢 Resources freed — restoring '{name}'")
            await asyncio.to_thread(start_container, name)
            mutated = True

        if mutated:
            containers = await asyncio.to_thread(collect_container_info)
        cached_containers = containers
        publish_snapshot()
        await asyncio.sleep(max(5, CHECK_INTERVAL))

# ─── API routes ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    global monitor_running, monitor_task, metrics_task
    global cached_containers
    load_state()
    logger.info(
        f"🖥️  Server {SERVER_INFO['hostname']} ip={SERVER_INFO['ip']} "
        f"cpu={SERVER_INFO['cpu_cores']} ram={SERVER_INFO['ram_total_gb']}GB"
    )
    cached_containers = await asyncio.to_thread(collect_container_info)
    metrics_task = asyncio.create_task(metrics_loop())
    monitor_running = True
    monitor_task = asyncio.create_task(monitor_loop())


@app.on_event("shutdown")
async def shutdown():
    global monitor_running
    monitor_running = False
    for task in (monitor_task, metrics_task):
        if task:
            task.cancel()


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


@app.get("/api/status")
async def get_status():
    return full_payload()


@app.get("/api/events")
async def status_events():
    async def stream():
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        subscribers.add(q)
        try:
            yield f"event: snapshot\ndata: {json.dumps(full_payload(), separators=(',', ':'), ensure_ascii=False)}\n\n"
            while True:
                try:
                    event, blob = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"event: {event}\ndata: {blob}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/priority")
async def set_priority(update: PriorityUpdate):
    priority = as_priority(update.priority)
    priority_map[update.container_name] = priority
    save_container(update.container_name, priority=priority)
    logger.info(f"⚙️ Priority updated: {update.container_name} → {priority}")
    for item in cached_containers:
        if item["name"] == update.container_name:
            item["priority"] = priority
            break
    publish_snapshot()
    return {"ok": True, "container": update.container_name, "priority": priority}


@app.post("/api/thresholds")
async def update_thresholds(update: ThresholdUpdate):
    global CPU_THRESHOLD, RAM_THRESHOLD, CHECK_INTERVAL
    if update.cpu is not None:
        CPU_THRESHOLD = update.cpu
    if update.ram is not None:
        RAM_THRESHOLD = update.ram
    if update.interval is not None:
        CHECK_INTERVAL = max(5, int(update.interval))
    logger.info(f"⚙️  Thresholds updated: CPU={CPU_THRESHOLD}% RAM={RAM_THRESHOLD}% interval={CHECK_INTERVAL}s")
    save_settings()
    publish_snapshot()
    return {"ok": True, "cpu": CPU_THRESHOLD, "ram": RAM_THRESHOLD, "interval": CHECK_INTERVAL}


@app.post("/api/container/{name}/start")
async def manual_start(name: str):
    ok = await asyncio.to_thread(start_container, name)
    await refresh_containers_and_publish()
    return {"ok": ok}


@app.post("/api/container/{name}/stop")
async def manual_stop(name: str):
    ok = await asyncio.to_thread(stop_container, name)
    await refresh_containers_and_publish()
    return {"ok": ok}


@app.get("/api/logs")
async def get_logs(lines: int = 80):
    return {"lines": await asyncio.to_thread(tail_lines, LOG_FILE, lines)}


@app.get("/api/monitor/toggle")
async def toggle_monitor():
    global monitor_running, monitor_task
    if monitor_running:
        monitor_running = False
        if monitor_task:
            monitor_task.cancel()
        logger.info("⏸️  Monitor paused by user")
        publish_snapshot()
        return {"running": False}

    monitor_running = True
    monitor_task = asyncio.create_task(monitor_loop())
    logger.info("▶️  Monitor resumed by user")
    publish_snapshot()
    return {"running": True}
