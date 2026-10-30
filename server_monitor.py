#!/usr/bin/env python3
"""
Server Monitor — lightweight system dashboard with Telegram alerts.
Serves a JSON API at /api/status and the dashboard at /.
"""

import json
import os
import re
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timedelta

import psutil

# ── Config ──────────────────────────────────────────────────────────
PORT = int(os.environ.get("MONITOR_PORT", 9090))
MONITOR_DRIVES = os.environ.get("MONITOR_DRIVES", "/srv/data,/srv/backup").split(",")
BORG_REPO = os.environ.get("BORG_REPO", "/srv/backup/immich-borg")
BACKUP_LOG = os.environ.get("BACKUP_LOG", "/var/log/immich-backup.log")
IMMICH_COMPOSE = os.environ.get("IMMICH_COMPOSE", os.path.expanduser("~/immich"))

# Telegram (optional — set both to enable)
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# Alert thresholds
ALERT_CPU_PCT = int(os.environ.get("ALERT_CPU_PCT", 90))
ALERT_RAM_PCT = int(os.environ.get("ALERT_RAM_PCT", 85))
ALERT_DISK_PCT = int(os.environ.get("ALERT_DISK_PCT", 85))
ALERT_BACKUP_STALE_HOURS = int(os.environ.get("ALERT_BACKUP_STALE_HOURS", 36))

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

# ── Alert state ─────────────────────────────────────────────────────
_alert_cooldowns = {}  # key -> last_sent timestamp
ALERT_COOLDOWN = 1800  # seconds between repeated alerts


def send_telegram(message):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)


def maybe_alert(key, message):
    now = time.time()
    if key in _alert_cooldowns and now - _alert_cooldowns[key] < ALERT_COOLDOWN:
        return
    _alert_cooldowns[key] = now
    send_telegram(f"🖥 <b>Server Alert</b>\n{message}")


# ── Metric collectors ───────────────────────────────────────────────

def get_cpu():
    freq = psutil.cpu_freq()
    per_core = psutil.cpu_percent(interval=0, percpu=True)
    avg = sum(per_core) / len(per_core) if per_core else 0
    temps = {}
    try:
        t = psutil.sensors_temperatures()
        for name, entries in t.items():
            for e in entries:
                if e.current > 0:
                    temps[e.label or name] = round(e.current, 1)
    except Exception:
        pass
    return {
        "cores": psutil.cpu_count(logical=True),
        "physical_cores": psutil.cpu_count(logical=False),
        "usage_per_core": per_core,
        "usage_avg": round(avg, 1),
        "freq_current": round(freq.current) if freq else None,
        "freq_max": round(freq.max) if freq and freq.max else None,
        "temps": temps,
        "load_1": round(os.getloadavg()[0], 2),
        "load_5": round(os.getloadavg()[1], 2),
        "load_15": round(os.getloadavg()[2], 2),
    }


def get_memory():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "total_gb": round(vm.total / (1024**3), 1),
        "used_gb": round(vm.used / (1024**3), 1),
        "available_gb": round(vm.available / (1024**3), 1),
        "percent": vm.percent,
        "swap_total_gb": round(sw.total / (1024**3), 1),
        "swap_used_gb": round(sw.used / (1024**3), 1),
        "swap_percent": sw.percent,
    }


def get_disks():
    disks = []
    for path in MONITOR_DRIVES:
        path = path.strip()
        if not path:
            continue
        mounted = os.path.ismount(path)
        info = {"path": path, "mounted": mounted}
        if mounted:
            usage = psutil.disk_usage(path)
            info["total_gb"] = round(usage.total / (1024**3), 1)
            info["used_gb"] = round(usage.used / (1024**3), 1)
            info["free_gb"] = round(usage.free / (1024**3), 1)
            info["percent"] = usage.percent
        disks.append(info)
    return disks


def _smart_parse(output):
    """Pull health + key attributes out of `smartctl -H -A` text (ATA and NVMe)."""
    info = {"health": "UNKNOWN"}
    for line in output.splitlines():
        s = line.strip()
        if "overall-health" in s or "SMART Health Status" in s:
            if "PASSED" in s or s.endswith("OK"):
                info["health"] = "PASSED"
            elif "FAILED" in s:
                info["health"] = "FAILED"
            continue
        parts = s.split()
        # ATA rows: ID NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW...
        if len(parts) >= 10 and parts[0].isdigit():
            m = re.match(r"\d+", parts[9])
            if not m:
                continue
            raw = int(m.group())
            name = parts[1]
            if name == "Reallocated_Sector_Ct":
                info["reallocated"] = raw
            elif name == "Current_Pending_Sector":
                info["pending"] = raw
            elif name == "Power_On_Hours":
                info["power_hours"] = raw
            elif name in ("Temperature_Celsius", "Airflow_Temperature_Cel"):
                info["temp_c"] = raw
            continue
        # NVMe rows: "Label:   value"
        m = re.match(r"Power On Hours:\s+([\d,]+)", s)
        if m:
            info["power_hours"] = int(m.group(1).replace(",", ""))
        m = re.match(r"Percentage Used:\s+(\d+)%", s)
        if m:
            info["wear_pct"] = int(m.group(1))
        m = re.match(r"Temperature:\s+(\d+) Celsius", s)
        if m:
            info["temp_c"] = int(m.group(1))
    return info


def get_smart():
    try:
        out = subprocess.run(
            ["lsblk", "-dn", "-J", "-o", "NAME,SIZE,MODEL,TYPE"],
            capture_output=True, text=True, timeout=10
        ).stdout
        devices = json.loads(out).get("blockdevices", [])
    except Exception as e:
        return [{"error": f"lsblk failed: {e}"}]

    results = []
    for d in devices:
        if d.get("type") != "disk":
            continue
        dev = "/dev/" + d["name"]
        entry = {"device": dev, "size": d.get("size", ""),
                 "model": (d.get("model") or "").strip(), "health": "UNKNOWN"}
        try:
            for extra in ([], ["-d", "sat"]):  # second try for USB bridges
                proc = subprocess.run(
                    ["sudo", "-n", "/usr/sbin/smartctl", "-H", "-A", *extra, dev],
                    capture_output=True, text=True, timeout=30
                )
                if "password is required" in proc.stderr:
                    entry["note"] = "sudo blocked smartctl - check /etc/sudoers.d/server-monitor"
                    break
                entry.update(_smart_parse(proc.stdout))
                if entry["health"] != "UNKNOWN":
                    break
            if entry["health"] == "UNKNOWN" and "note" not in entry:
                entry["note"] = "no SMART data returned"
        except Exception as e:
            entry["note"] = f"smartctl error: {e}"
        results.append(entry)
    return results


_smart_cache = {"data": None, "time": 0}
SMART_TTL = 600  # drives don't change health every 3s; don't wake/poll them that often


def get_smart_cached():
    now = time.time()
    if _smart_cache["data"] is None or now - _smart_cache["time"] > SMART_TTL:
        _smart_cache["data"] = get_smart()
        _smart_cache["time"] = now
    return _smart_cache["data"]


def get_docker():
    containers = []
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"],
            capture_output=True, text=True, timeout=15
        )
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) >= 3:
                name, status, image = parts
                healthy = "healthy" in status.lower()
                running = status.lower().startswith("up")
                containers.append({
                    "name": name,
                    "status": status,
                    "image": image.split("/")[-1],
                    "running": running,
                    "healthy": healthy,
                })
    except Exception as e:
        containers.append({"error": str(e)})
    return containers


def get_network():
    counters = psutil.net_io_counters()
    interfaces = {}
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    for iface, addr_list in addrs.items():
        if iface.startswith("lo") or iface.startswith("veth") or iface.startswith("br-") or iface == "docker0":
            continue
        ipv4 = ""
        for a in addr_list:
            if a.family.name == "AF_INET":
                ipv4 = a.address
        s = stats.get(iface)
        interfaces[iface] = {
            "ip": ipv4,
            "up": s.isup if s else False,
            "speed_mbps": s.speed if s else 0,
        }
    return {
        "bytes_sent": counters.bytes_sent,
        "bytes_recv": counters.bytes_recv,
        "interfaces": interfaces,
    }


def get_backup_status():
    status = {
        "log_exists": False,
        "last_line": "",
        "last_success": None,
        "stale": True,
        "hours_ago": None,
    }
    log_path = Path(BACKUP_LOG)
    if not log_path.exists():
        return status
    status["log_exists"] = True
    try:
        lines = log_path.read_text().strip().split("\n")
        # find last "finished ok" line
        for line in reversed(lines):
            if "finished ok" in line:
                status["last_line"] = line.strip()
                # parse timestamp: === 2026-10-11 21:35:06 finished ok ===
                parts = line.split()
                if len(parts) >= 4:
                    try:
                        ts = datetime.strptime(f"{parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S")
                        hours = (datetime.now() - ts).total_seconds() / 3600
                        status["last_success"] = ts.isoformat()
                        status["hours_ago"] = round(hours, 1)
                        status["stale"] = hours > ALERT_BACKUP_STALE_HOURS
                    except ValueError:
                        pass
                break
        if not status["last_line"]:
            status["last_line"] = lines[-1].strip() if lines else ""
    except Exception as e:
        status["last_line"] = f"Error reading log: {e}"
    return status


def get_uptime():
    boot = datetime.fromtimestamp(psutil.boot_time())
    delta = datetime.now() - boot
    days = delta.days
    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60
    return {
        "boot_time": boot.isoformat(),
        "uptime_str": f"{days}d {hours}h {minutes}m",
        "days": days,
    }


def collect_all():
    cpu = get_cpu()
    mem = get_memory()
    disks = get_disks()
    backup = get_backup_status()
    smart = get_smart_cached()

    # ── Alerting ──
    if cpu["usage_avg"] > ALERT_CPU_PCT:
        maybe_alert("cpu", f"CPU at {cpu['usage_avg']}% (threshold {ALERT_CPU_PCT}%)")
    if mem["percent"] > ALERT_RAM_PCT:
        maybe_alert("ram", f"RAM at {mem['percent']}% (threshold {ALERT_RAM_PCT}%)")
    for d in disks:
        if d.get("percent", 0) > ALERT_DISK_PCT:
            maybe_alert(f"disk_{d['path']}", f"Disk {d['path']} at {d['percent']}% (threshold {ALERT_DISK_PCT}%)")
        if not d["mounted"]:
            maybe_alert(f"unmount_{d['path']}", f"⚠️ {d['path']} is NOT MOUNTED!")
    for s in smart:
        label = s.get("model") or s.get("device", "drive")
        if s.get("health") == "FAILED":
            maybe_alert(f"smart_{s.get('device')}", f"💀 {label} SMART health FAILED")
        elif s.get("pending", 0) > 0 or s.get("reallocated", 0) > 0:
            maybe_alert(f"smartwarn_{s.get('device')}",
                        f"{label}: {s.get('reallocated', 0)} reallocated, {s.get('pending', 0)} pending sectors")
    if backup.get("stale"):
        maybe_alert("backup_stale", f"Last backup is {backup.get('hours_ago', '?')} hours old (threshold {ALERT_BACKUP_STALE_HOURS}h)")

    return {
        "timestamp": datetime.now().isoformat(),
        "hostname": os.uname().nodename,
        "uptime": get_uptime(),
        "cpu": cpu,
        "memory": mem,
        "disks": disks,
        "smart": smart,
        "docker": get_docker(),
        "network": get_network(),
        "backup": backup,
    }


# ── HTTP Server ─────────────────────────────────────────────────────

# Pre-collect so first request doesn't block
_cached = {"data": None, "time": 0}
_cache_lock = threading.Lock()
CACHE_TTL = 3  # seconds


def get_cached_status():
    now = time.time()
    with _cache_lock:
        if _cached["data"] is None or now - _cached["time"] > CACHE_TTL:
            _cached["data"] = collect_all()
            _cached["time"] = now
        return _cached["data"]


class MonitorHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            data = get_cached_status()
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.write(payload)
        elif self.path == "/" or self.path == "/index.html":
            try:
                html = DASHBOARD_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.write(html)
            except FileNotFoundError:
                self.send_error(404, "dashboard.html not found")
        else:
            self.send_error(404)

    def write(self, data):
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

    def log_message(self, format, *args):
        # suppress per-request logs
        pass


def main():
    # warm the psutil CPU counter
    psutil.cpu_percent(interval=0, percpu=True)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), MonitorHandler)
    print(f"Server Monitor running on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
