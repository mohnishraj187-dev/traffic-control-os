from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "traffic_control.db"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
DEFAULT_ADMIN_EMAILS = "mohnishraj187@gmail.com,garvnijhawan24@gmail.com"
ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.environ.get("ADMIN_EMAILS", DEFAULT_ADMIN_EMAILS).split(",")
    if email.strip()
}
APP_BASE_URL = os.environ.get("APP_BASE_URL", f"http://{HOST}:{PORT}")
IOT_NODE_TOKEN = os.environ.get("IOT_NODE_TOKEN", "dev-traffic-node")

SESSIONS: dict[str, dict] = {}
CAMERA_FEED: dict[str, dict] = {}
AI_SIGNAL_STATE: dict[str, object] = {
    "last_update": 0,
    "signal": "ai",
    "green_seconds": 0,
    "vehicle_counts": {"cars": 0, "buses": 0, "trucks": 0, "motorcycles": 0, "bicycles": 0, "trains": 0, "total": 0},
}
ESP32_WORKER: dict[str, object] = {
    "running": False,
    "stop_event": None,
    "thread": None,
    "stream_url": "",
    "lane": "",
    "status": "Auto count is off.",
    "last_count": 0,
    "last_density": 0,
    "updated_at": 0,
}


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS qr_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                location TEXT,
                user_email TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS accident_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_type TEXT NOT NULL,
                description TEXT,
                location TEXT,
                image_filename TEXT,
                image_mime TEXT,
                user_email TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS signal_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                signal TEXT NOT NULL,
                lane_diversion INTEGER NOT NULL DEFAULT 1,
                priority_pass INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS geocode_cache (
                query TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS route_cache (
                route_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO signal_state (id, signal, lane_diversion, priority_pass, updated_at)
            VALUES (1, 'stop', 1, 0, strftime('%s', 'now'));
            """
        )
        existing_columns = {row[1] for row in db.execute("PRAGMA table_info(signal_state)").fetchall()}
        if "target_label" not in existing_columns:
            db.execute("ALTER TABLE signal_state ADD COLUMN target_label TEXT DEFAULT 'Silk Board Junction'")
        if "target_location" not in existing_columns:
            db.execute("ALTER TABLE signal_state ADD COLUMN target_location TEXT DEFAULT '12.9177, 77.6238'")
        db.commit()


def db_rows(query: str, params: tuple = ()) -> list[dict]:
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, params).fetchall()]


def db_execute(query: str, params: tuple = ()) -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(query, params)
        db.commit()


def db_execute_many(query: str, params: tuple = ()) -> int:
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute(query, params)
        db.commit()
        return cursor.rowcount


def html_page(title: str, body: str, extra_head: str = "") -> bytes:
    return f"""<!doctype html>
<html lang="en" class="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Public+Sans:wght@600;700;800;900&display=swap" rel="stylesheet">
  <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #f8f9ff;
      --ink: #0b1c30;
      --muted: #45464d;
      --panel: #ffffff;
      --panel-soft: #eff4ff;
      --line: #c6c6cd;
      --orange: #fd761a;
      --navy: #131b2e;
    }}
    body {{ min-height: 100dvh; background: var(--bg); color: var(--ink); font-family: Inter, system-ui, sans-serif; }}
    h1,h2,h3,.headline {{ font-family: "Public Sans", Inter, sans-serif; }}
    .material-symbols-outlined {{ font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; }}
    .mesh-bg {{ background-image: radial-gradient(at 0% 0%, #eaf1ff 0, transparent 52%), radial-gradient(at 100% 0%, #d3e4fe 0, transparent 48%); }}
    .signal-active {{ filter: drop-shadow(0 0 10px currentColor); }}
    .hide-scrollbar::-webkit-scrollbar {{ display: none; }}
    .hide-scrollbar {{ -ms-overflow-style: none; scrollbar-width: none; }}
  </style>
  {extra_head}
</head>
<body>{body}</body></html>""".encode("utf-8")


def current_user(handler: BaseHTTPRequestHandler) -> dict | None:
    header = handler.headers.get("Cookie", "")
    jar = cookies.SimpleCookie(header)
    sid = jar.get("tc_session")
    if not sid:
        return None
    return SESSIONS.get(sid.value)


def set_session(handler: BaseHTTPRequestHandler, user: dict) -> None:
    sid = secrets.token_urlsafe(32)
    SESSIONS[sid] = user
    handler.send_header("Set-Cookie", f"tc_session={sid}; HttpOnly; SameSite=Lax; Path=/")


def request_base_url(handler: BaseHTTPRequestHandler) -> str:
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    forwarded_host = handler.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    proto = forwarded_proto or ("https" if handler.headers.get("X-Forwarded-Ssl", "").lower() == "on" else "http")
    host = forwarded_host or handler.headers.get("Host", f"{HOST}:{PORT}")
    return f"{proto}://{host}"


def redirect_uri(handler: BaseHTTPRequestHandler) -> str:
    return f"{request_base_url(handler)}/auth/google/callback"


def is_admin_email(email: str) -> bool:
    return email.strip().lower() in ADMIN_EMAILS


def is_admin_user(user: dict | None) -> bool:
    return bool(user and is_admin_email(user.get("email", "")))


def admin_email_list() -> str:
    return ", ".join(sorted(ADMIN_EMAILS))


def safe_print(message: str) -> None:
    try:
        if sys.stdout:
            print(message)
    except OSError:
        pass


def page_login() -> bytes:
    body = """
<header class="fixed top-0 z-40 flex h-16 w-full items-center justify-between border-b border-slate-200 bg-slate-50 px-6">
  <div class="flex items-center gap-2">
    <span class="material-symbols-outlined text-slate-900">traffic</span>
    <span class="text-lg font-bold tracking-tight text-slate-900">TrafficControl OS</span>
  </div>
  <nav class="hidden items-center gap-6 md:flex">
    <a class="font-medium text-slate-500 hover:text-slate-900" href="/public">Public Portal</a>
    <a class="font-medium text-slate-500 hover:text-slate-900" href="/admin-portal">Admin Portal</a>
  </nav>
</header>
<main class="mesh-bg flex min-h-screen items-center justify-center px-6 pt-20">
  <section class="w-full max-w-6xl py-10">
    <div class="mb-8 text-center">
      <span class="mb-4 inline-block rounded-full bg-[#e5eeff] px-3 py-1 text-xs font-bold uppercase tracking-wider text-[#7c839b]">Secure Gateway Access</span>
      <h1 class="headline mb-2 text-3xl font-bold text-slate-950">Select Your Workspace</h1>
      <p class="mx-auto max-w-2xl text-lg text-slate-600">Access real-time traffic telemetry and infrastructure controls. Authenticate to continue to your dashboard.</p>
    </div>
    <div class="grid grid-cols-1 gap-6 md:grid-cols-2">
      <article class="rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm transition hover:shadow-md">
        <div class="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-full bg-[#dce9ff]"><span class="material-symbols-outlined text-4xl">public</span></div>
        <h2 class="headline mb-2 text-2xl font-semibold">Public User</h2>
        <p class="mb-8 text-slate-600">Access live maps, report incidents, scan signal QR codes, and send accident photos.</p>
        <a class="google-btn flex w-full items-center justify-center gap-3 rounded-lg border border-slate-300 bg-white px-5 py-4 font-semibold text-slate-900 hover:bg-slate-50" href="/auth/google?role=public">
          <span class="material-symbols-outlined">login</span> Sign in with Google
        </a>
        <div class="mt-6 h-1 rounded-full bg-[#dce9ff]"><div class="h-1 w-1/3 rounded-full bg-slate-950"></div></div>
      </article>
      <article class="relative rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm transition hover:shadow-md">
        <span class="absolute right-4 top-4 flex h-2 w-2"><span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-[#fd761a] opacity-75"></span><span class="relative inline-flex h-2 w-2 rounded-full bg-[#fd761a]"></span></span>
        <div class="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-full bg-[#ffdbca]"><span class="material-symbols-outlined text-4xl text-[#5c2400]">admin_panel_settings</span></div>
        <h2 class="headline mb-2 text-2xl font-semibold">Admin Portal</h2>
        <p class="mb-8 text-slate-600">Manage signal timings, emergency overrides, QR scan requests, and accident reports.</p>
        <a class="flex w-full items-center justify-center gap-3 rounded-lg border border-slate-950 bg-slate-950 px-5 py-4 font-semibold text-white hover:bg-slate-800" href="/auth/google?role=admin">
          <span class="material-symbols-outlined">login</span> Sign in with Google
        </a>
        <div class="mt-6 h-1 rounded-full bg-[#dce9ff]"><div class="h-1 w-3/4 rounded-full bg-[#fd761a]"></div></div>
      </article>
    </div>
  </section>
</main>"""
    return html_page("TrafficControl OS | Gateway", body)


def page_access_denied(email: str) -> bytes:
    body = f"""
<main class="grid min-h-screen place-items-center bg-[#f8f9ff] p-6 font-sans text-[#0b1c30]">
  <section class="w-full max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center shadow-sm">
    <span class="material-symbols-outlined mb-4 text-5xl text-red-600">block</span>
    <h1 class="headline mb-2 text-2xl font-bold">Admin Access Denied</h1>
    <p class="mb-4 text-slate-600">The account <b>{email or "unknown"}</b> is not allowed to enter the admin portal.</p>
    <p class="rounded bg-slate-50 p-3 text-sm text-slate-500">Admin access is restricted to {admin_email_list()}.</p>
    <a class="mt-6 inline-block rounded-lg bg-slate-950 px-4 py-3 font-bold text-white" href="/">Back to Login</a>
  </section>
</main>"""
    return html_page("Admin Access Denied", body)


def page_public(user: dict | None) -> bytes:
    email = (user or {}).get("email", "demo.public@traffic.local")
    extra_head = """
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIINfQHLyrcf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <style>
    #osmTrafficMap { position: relative; min-height: 500px; height: 100%; width: 100%; overflow: hidden; }
    .leaflet-container { overflow: hidden; height: 100%; width: 100%; font-family: Inter, system-ui, sans-serif; background: #dbe7f1; outline-offset: 1px; }
    .leaflet-pane,
    .leaflet-tile,
    .leaflet-marker-icon,
    .leaflet-marker-shadow,
    .leaflet-tile-container,
    .leaflet-pane > svg,
    .leaflet-pane > canvas,
    .leaflet-zoom-box,
    .leaflet-image-layer,
    .leaflet-layer { position: absolute; left: 0; top: 0; }
    .leaflet-container img { max-width: none !important; max-height: none !important; }
    .leaflet-tile { width: 256px !important; height: 256px !important; user-select: none; visibility: hidden; }
    .leaflet-tile-loaded { visibility: inherit; }
    .leaflet-map-pane,
    .leaflet-tile-pane,
    .leaflet-overlay-pane,
    .leaflet-shadow-pane,
    .leaflet-marker-pane,
    .leaflet-tooltip-pane,
    .leaflet-popup-pane { position: absolute; left: 0; top: 0; }
    .leaflet-tile-pane { z-index: 200; }
    .leaflet-overlay-pane { z-index: 400; }
    .leaflet-shadow-pane { z-index: 500; }
    .leaflet-marker-pane { z-index: 600; }
    .leaflet-tooltip-pane { z-index: 650; }
    .leaflet-popup-pane { z-index: 700; }
    .leaflet-control { position: relative; z-index: 800; pointer-events: auto; float: left; clear: both; }
    .leaflet-top, .leaflet-bottom { position: absolute; z-index: 1000; pointer-events: none; }
    .leaflet-top { top: 10px; }
    .leaflet-right { right: 10px; }
    .leaflet-bottom { bottom: 10px; }
    .leaflet-left { left: 10px; }
    .leaflet-right .leaflet-control { float: right; }
    .leaflet-bottom .leaflet-control { margin-bottom: 10px; }
    .leaflet-top .leaflet-control { margin-top: 10px; }
    .leaflet-left .leaflet-control { margin-left: 10px; }
    .leaflet-right .leaflet-control { margin-right: 10px; }
    .leaflet-control-zoom a { display: grid; place-items: center; width: 34px; height: 34px; border-bottom: 1px solid #d7dde6; background: #fff; color: #0b1c30; font: bold 22px/1 Inter, sans-serif; text-decoration: none; }
    .leaflet-control-zoom a:first-child { border-radius: 6px 6px 0 0; }
    .leaflet-control-zoom a:last-child { border-bottom: 0; border-radius: 0 0 6px 6px; }
    .leaflet-control-zoom { border: 1px solid #d7dde6; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,.12); overflow: hidden; }
    .leaflet-control-attribution { display: none; }
    .leaflet-interactive { cursor: pointer; }
    .route-dot { width: 18px; height: 18px; border-radius: 999px; border: 3px solid #fff; box-shadow: 0 2px 10px rgba(0,0,0,.35); }
    .route-dot-start { background: #0f766e; }
    .route-dot-end { background: #dc2626; }
    .blocked-lane-marker { display: grid; height: 36px; width: 36px; place-items: center; border: 3px solid #fff; border-radius: 999px; background: #dc2626; color: #fff; box-shadow: 0 12px 26px rgb(127 29 29 / 0.35); font-weight: 900; }
  </style>"""
    body = f"""
<aside class="fixed left-0 top-0 z-50 hidden h-full w-64 flex-col border-r border-slate-200 bg-white py-6 lg:flex">
  <div class="mb-8 px-6"><span class="text-lg font-bold uppercase tracking-tight">I-TRAFFIC PUBLIC</span></div>
  <nav class="flex-1 space-y-1 px-3">
    <a class="flex items-center border-r-4 border-slate-900 bg-slate-100 px-4 py-3 font-semibold text-slate-900" href="#live"><span class="material-symbols-outlined mr-3">traffic</span>Live Traffic</a>
    <a class="flex items-center px-4 py-3 text-slate-500 hover:bg-slate-50" href="#report"><span class="material-symbols-outlined mr-3">report_problem</span>Incident Reports</a>
    <a class="flex items-center px-4 py-3 text-slate-500 hover:bg-slate-50" href="#scan"><span class="material-symbols-outlined mr-3">qr_code_scanner</span>Scan QR</a>
  </nav>
  <div class="border-t border-slate-100 px-6 pt-6 text-xs"><p class="font-bold text-slate-900">{email}</p><p class="text-slate-500">Public access</p></div>
</aside>
<main class="min-h-screen pb-16 lg:pl-64">
  <header class="sticky top-0 z-40 flex h-16 w-full items-center justify-between border-b border-slate-200 bg-slate-50/90 px-6 backdrop-blur">
    <h1 class="headline text-xl font-black uppercase tracking-widest">System Overview</h1>
    <span class="text-xs font-bold uppercase tracking-wider text-slate-500">Public Access</span>
  </header>
  <div class="hide-scrollbar flex snap-x snap-mandatory overflow-x-auto">
    <section id="live" class="w-full flex-none snap-start space-y-6 p-6">
      <div class="flex flex-col justify-between gap-4 md:flex-row md:items-center">
        <div><span class="rounded bg-red-100 px-2 py-0.5 text-xs font-bold uppercase text-red-600">Live</span><h2 class="headline mt-2 text-2xl font-semibold">Local Traffic Map</h2><p class="text-slate-600">Location-based map and destination routing without paid Google Maps APIs.</p></div>
        <div class="flex flex-wrap gap-2"><input id="destinationInput" class="min-w-64 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm" placeholder="Enter destination for route"><button id="optimizeRoutesBtn" class="rounded-lg bg-slate-950 px-4 py-2 font-medium text-white">Show Route</button><button id="layersBtn" class="rounded-lg border bg-white px-4 py-2 font-medium">Map Style</button></div>
      </div>
      <div class="grid grid-cols-12 gap-6">
        <div class="relative col-span-12 h-[500px] overflow-hidden rounded-lg border border-slate-200 bg-white lg:col-span-8">
          <div id="osmTrafficMap" class="h-full w-full"></div>
          <div id="fallbackTrafficMap" class="pointer-events-none absolute inset-x-4 bottom-4 z-[450] rounded-lg border bg-white/95 p-3 text-sm shadow-sm backdrop-blur">
            <p id="fallbackLocation" class="font-mono text-slate-600">Waiting for location permission...</p>
          </div>
          <div class="absolute left-4 top-4 rounded-lg border bg-white/90 p-3 shadow-sm backdrop-blur"><p class="text-xs font-bold uppercase text-slate-500">Avg. Speed</p><p id="avgSpeed" class="font-mono text-lg font-bold">24.5 km/h</p><p id="speedTrend" class="text-xs text-red-500">12% from yesterday</p></div>
          <div id="laneBlockAlert" class="pointer-events-none absolute inset-x-4 top-24 z-[450] hidden rounded-lg border border-red-200 bg-red-600/95 p-3 text-white shadow-sm backdrop-blur">
            <p class="text-xs font-bold uppercase">Lane blocked by traffic control</p>
            <p id="laneBlockText" class="mt-1 text-sm">Follow diversion near the active signal.</p>
          </div>
          <div class="absolute bottom-4 right-4 flex flex-col gap-2">
            <button id="zoomInBtn" class="grid h-10 w-10 place-items-center rounded border bg-white shadow-sm"><span class="material-symbols-outlined">add</span></button>
            <button id="zoomOutBtn" class="grid h-10 w-10 place-items-center rounded border bg-white shadow-sm"><span class="material-symbols-outlined">remove</span></button>
            <button id="locateBtn" class="grid h-10 w-10 place-items-center rounded border bg-white shadow-sm"><span class="material-symbols-outlined">my_location</span></button>
          </div>
        </div>
        <aside class="col-span-12 space-y-4 lg:col-span-4">
          <div class="rounded-lg border bg-white p-5"><div class="mb-4 flex items-center justify-between"><h3 class="text-xs font-bold uppercase text-slate-500">Congestion Hotspots</h3><button id="refreshTrafficBtn" class="rounded border px-2 py-1 text-xs font-bold">Refresh</button></div><div id="hotspots" class="space-y-3"></div></div>
          <div class="rounded-lg border bg-white p-5"><h3 class="mb-2 text-xs font-bold uppercase text-slate-500">Map Source</h3><p id="mapSource" class="text-sm text-slate-600">OpenStreetMap with road routing</p></div>
          <div class="rounded-lg border bg-white p-5"><h3 class="mb-2 text-xs font-bold uppercase text-slate-500">Route</h3><p id="routeSummary" class="text-sm text-slate-600">Enter a destination and press Show Route.</p></div>
        </aside>
      </div>
    </section>
    <section id="report" class="w-full flex-none snap-start p-6">
      <div class="mx-auto max-w-4xl"><h2 class="headline text-2xl font-semibold">Incident Reporting</h2><p class="mb-8 text-slate-600">Submit an accident report to the admin portal.</p>
        <form id="reportForm" class="grid gap-8 rounded-lg border bg-white p-6 shadow-sm md:grid-cols-2">
          <div class="space-y-5">
            <label class="block text-xs font-bold uppercase text-slate-500">Incident Type<select name="incident_type" class="mt-2 w-full rounded-lg border-slate-200 bg-slate-50 text-sm"><option>Vehicle Collision</option><option>Infrastructure Damage</option><option>Medical Emergency</option><option>Stalled Vehicle</option></select></label>
            <label class="block text-xs font-bold uppercase text-slate-500">Location<input id="reportLocation" name="location" class="mt-2 w-full rounded-lg border-slate-200 bg-slate-100 font-mono text-sm" readonly value="Detecting location..."></label>
            <label class="block text-xs font-bold uppercase text-slate-500">Description<textarea name="description" rows="5" class="mt-2 w-full rounded-lg border-slate-200 bg-slate-50 text-sm" placeholder="Describe the scene..."></textarea></label>
          </div>
          <div class="space-y-5">
            <label class="block text-xs font-bold uppercase text-slate-500">Photo Evidence<input required name="photo" type="file" accept="image/*" class="mt-2 w-full rounded-lg border border-dashed border-slate-300 bg-slate-50 p-8 text-center"></label>
            <div class="rounded-lg border border-orange-100 bg-orange-50 p-4"><b>Direct Alert Enabled</b><p class="text-sm text-slate-600">This report will appear in the admin accident section.</p></div>
            <button class="w-full rounded-lg bg-[#fd761a] py-4 font-bold text-white shadow-lg shadow-orange-500/20" type="submit">Submit Alert</button>
            <p id="reportStatus" class="text-sm font-semibold text-slate-600"></p>
          </div>
        </form>
      </div>
    </section>
    <section id="scan" class="flex w-full flex-none snap-start items-center justify-center p-6">
      <div class="w-full max-w-md space-y-6 rounded-lg border bg-white p-6 text-center">
        <h2 class="headline text-2xl font-semibold">Scan Location</h2><p class="text-slate-500">Tap scan, point the camera at any QR code, and the request is sent to admin automatically.</p>
        <video id="qrVideo" class="mx-auto hidden h-72 w-72 rounded-lg border-4 border-slate-950 object-cover" playsinline muted></video>
        <canvas id="qrCanvas" class="hidden"></canvas>
        <div id="scannerPlaceholder" class="mx-auto flex h-72 w-72 items-center justify-center rounded-lg border-4 border-slate-950 bg-slate-100"><span class="material-symbols-outlined text-6xl text-slate-400">qr_code_scanner</span></div>
        <div class="flex gap-2"><button id="scanBtn" class="flex-1 rounded-lg bg-slate-950 px-4 py-3 font-bold text-white">Start Scan</button><button id="stopScanBtn" class="hidden flex-1 rounded-lg border bg-white px-4 py-3 font-bold">Stop</button></div>
        <p id="scanStatus" class="text-sm font-semibold text-slate-600"></p>
      </div>
    </section>
  </div>
  <nav class="fixed bottom-0 left-0 z-50 flex h-16 w-full justify-around border-t bg-white lg:hidden"><a class="grid place-items-center text-xs font-bold" href="#live">Live</a><a class="grid place-items-center text-xs font-bold text-slate-400" href="#report">Reports</a><a class="grid place-items-center text-xs font-bold text-slate-400" href="#scan">Scan</a></nav>
</main>
<script src="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.min.js"></script>
<script>
let trafficMap;
let streetLayer;
let topoLayer;
let routeLine;
let routeShadowLine;
let currentLocationMarker;
let destinationMarker;
let routeStartMarker;
let blockedLaneMarker;
let blockedLaneCircle;
let usingTopoLayer = false;
let routeIndex = 0;
const routeFocus = [
  {{ name: 'Worli Sea Link', center: {{ lat: 19.0270, lng: 72.8150 }}, zoom: 14 }},
  {{ name: 'Western Express Hwy', center: {{ lat: 19.1176, lng: 72.8562 }}, zoom: 13 }},
  {{ name: 'Bandra Kurla Complex', center: {{ lat: 19.0697, lng: 72.8697 }}, zoom: 14 }}
];
function initOpenMap() {{
  trafficMap = L.map('osmTrafficMap', {{ zoomControl: false }}).setView([19.0760, 72.8777], 12);
  streetLayer = L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }}).addTo(trafficMap);
  topoLayer = L.tileLayer('https://{{s}}.tile.opentopomap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 17,
    attribution: '&copy; OpenStreetMap contributors, SRTM | OpenTopoMap'
  }});
  L.control.zoom({{ position: 'bottomright' }}).addTo(trafficMap);
  setTimeout(() => trafficMap.invalidateSize(), 150);
  setTimeout(() => trafficMap.invalidateSize(), 700);
  window.addEventListener('resize', () => trafficMap.invalidateSize());
  document.getElementById('mapSource').textContent = 'OpenStreetMap with road routing';
}}
function parseControlCoordinates(placeLocation) {{
  const match = String(placeLocation || '').match(/(-?[0-9]+(?:\\.[0-9]+)?)\\s*,\\s*(-?[0-9]+(?:\\.[0-9]+)?)/);
  if (!match) return null;
  const lat = Number(match[1]);
  const lng = Number(match[2]);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return null;
  return [lat, lng];
}}
function clearBlockedLane() {{
  document.getElementById('laneBlockAlert').classList.add('hidden');
  if (blockedLaneMarker) {{ blockedLaneMarker.remove(); blockedLaneMarker = null; }}
  if (blockedLaneCircle) {{ blockedLaneCircle.remove(); blockedLaneCircle = null; }}
}}
function renderBlockedLane(signal) {{
  if (!signal || !signal.lane_diversion) {{
    clearBlockedLane();
    return;
  }}
  const label = signal.target_label || 'Traffic control zone';
  const location = signal.target_location || '';
  document.getElementById('laneBlockAlert').classList.remove('hidden');
  document.getElementById('laneBlockText').textContent = `${{label}} - lane diversion active`;
  const coords = parseControlCoordinates(location);
  if (!coords || !trafficMap) return;
  if (!blockedLaneCircle) {{
    blockedLaneCircle = L.circle(coords, {{ radius: 220, color: '#dc2626', weight: 3, fillColor: '#ef4444', fillOpacity: 0.2 }}).addTo(trafficMap);
  }} else {{
    blockedLaneCircle.setLatLng(coords);
  }}
  if (!blockedLaneMarker) {{
    blockedLaneMarker = L.marker(coords, {{
      icon: L.divIcon({{ className: '', html: '<div class="blocked-lane-marker">!</div>', iconSize: [36, 36], iconAnchor: [18, 18] }})
    }}).addTo(trafficMap);
  }} else {{
    blockedLaneMarker.setLatLng(coords);
  }}
  blockedLaneMarker.bindPopup(`${{label}}<br>Lane diversion active`);
}}
async function refreshPublicSignal() {{
  try {{
    const data = await (await fetch('/api/public-signal')).json();
    renderBlockedLane(data.signal);
  }} catch (error) {{}}
}}
function renderHotspots(summary) {{
  document.getElementById('avgSpeed').textContent = summary.avg_speed;
  document.getElementById('speedTrend').textContent = summary.trend;
  if (summary.area) document.getElementById('mapSource').textContent = `${{summary.source_label || 'Location-based local congestion model'}} · ${{summary.area}}`;
  document.getElementById('hotspots').innerHTML = summary.hotspots.map((spot, index) => `
    <button class="hotspot-btn w-full rounded-r-lg border-l-4 ${{index === 0 ? 'border-red-500 bg-red-50' : 'border-orange-500 bg-orange-50'}} p-3 text-left" data-index="${{index}}">
      <b>${{spot.name}}</b>
      <p class="text-xs text-slate-500">Delay: ${{spot.delay}}${{spot.distance_km !== undefined ? ` · ${{spot.distance_km}} km away` : ''}}</p>
    </button>`).join('');
  document.querySelectorAll('.hotspot-btn').forEach(btn => btn.onclick = () => focusRoute(Number(btn.dataset.index || 0)));
}}
function focusRoute(index) {{
  const route = routeFocus[index % routeFocus.length];
  routeIndex = index;
  if (trafficMap) trafficMap.setView([route.center.lat, route.center.lng], route.zoom);
  document.getElementById('mapSource').textContent = `Focused route: ${{route.name}}`;
}}
async function refreshTrafficSummary() {{
  let url = '/api/traffic-summary';
  try {{
    const pos = await getCurrentPositionPromise();
    url += `?lat=${{pos.coords.latitude}}&lng=${{pos.coords.longitude}}`;
    const fallbackLocation = document.getElementById('fallbackLocation');
    if (fallbackLocation) fallbackLocation.textContent = `Your current location: ${{pos.coords.latitude.toFixed(6)}}, ${{pos.coords.longitude.toFixed(6)}}`;
  }} catch (error) {{}}
  const summary = await (await fetch(url)).json();
  renderHotspots(summary);
}}
function getCurrentPositionPromise() {{
  return new Promise((resolve, reject) => {{
    if (!navigator.geolocation) reject(new Error('Location unavailable'));
    navigator.geolocation.getCurrentPosition(resolve, reject, {{ enableHighAccuracy: true, timeout: 10000 }});
  }});
}}
async function centerOnCurrentLocation(fromAutoLoad = false) {{
  try {{
    const pos = await getCurrentPositionPromise();
    const center = {{ lat: pos.coords.latitude, lng: pos.coords.longitude }};
    const fallbackLocation = document.getElementById('fallbackLocation');
    if (fallbackLocation) fallbackLocation.textContent = `Your current location: ${{center.lat.toFixed(6)}}, ${{center.lng.toFixed(6)}}`;
    if (trafficMap) {{
      trafficMap.setView([center.lat, center.lng], 15);
      trafficMap.invalidateSize();
      if (!currentLocationMarker) {{
        currentLocationMarker = L.circleMarker([center.lat, center.lng], {{ radius: 8, color: '#fff', weight: 3, fillColor: '#0f766e', fillOpacity: 1 }}).addTo(trafficMap).bindPopup('Your current location');
      }} else {{
        currentLocationMarker.setLatLng([center.lat, center.lng]);
      }}
    }}
    document.getElementById('mapSource').textContent = `OpenStreetMap centered on your location: ${{center.lat.toFixed(4)}}, ${{center.lng.toFixed(4)}}`;
    return center;
  }} catch (error) {{
    const msg = fromAutoLoad ? 'Allow location permission to show your position on the map.' : 'Location permission not granted.';
    document.getElementById('mapSource').textContent = msg;
    const fallbackLocation = document.getElementById('fallbackLocation');
    if (fallbackLocation) fallbackLocation.textContent = msg;
    throw error;
  }}
}}
async function optimizeBestRoute() {{
  const destination = document.getElementById('destinationInput').value.trim();
  const summary = document.getElementById('routeSummary');
  if (!destination) {{
    summary.textContent = 'Please enter a destination first.';
    document.getElementById('destinationInput').focus();
    return;
  }}
  summary.textContent = 'Finding your best route...';
  try {{
    const origin = await centerOnCurrentLocation(false);
    const params = new URLSearchParams({{ origin_lat: origin.lat, origin_lng: origin.lng, destination }});
    const res = await fetch(`/api/route?${{params.toString()}}`);
    const route = await res.json();
    if (!res.ok || !route.ok) {{
      summary.textContent = route.error || 'Route could not be calculated for that destination.';
      document.getElementById('mapSource').textContent = 'Road route unavailable for this request';
      return;
    }}
    const coords = route.geometry.coordinates.map(([lng, lat]) => [lat, lng]);
    if (!coords.length) {{
      summary.textContent = 'Route could not be drawn on the map.';
      return;
    }}
    if (routeLine) routeLine.remove();
    if (routeShadowLine) routeShadowLine.remove();
    if (destinationMarker) destinationMarker.remove();
    if (routeStartMarker) routeStartMarker.remove();
    routeShadowLine = L.polyline(coords, {{ color: '#0b1c30', weight: 12, opacity: 0.45 }}).addTo(trafficMap);
    routeLine = L.polyline(coords, {{ color: '#fd761a', weight: 7, opacity: 1 }}).addTo(trafficMap);
    routeStartMarker = L.marker(coords[0], {{ icon: L.divIcon({{ className: '', html: '<div class="route-dot route-dot-start"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }}) }}).addTo(trafficMap).bindPopup('Start: your current location');
    destinationMarker = L.marker([route.destination.lat, route.destination.lng], {{ icon: L.divIcon({{ className: '', html: '<div class="route-dot route-dot-end"></div>', iconSize: [18, 18], iconAnchor: [9, 9] }}) }}).addTo(trafficMap).bindPopup(route.destination.name || destination);
    trafficMap.invalidateSize();
    setTimeout(() => {{
      trafficMap.invalidateSize();
      trafficMap.fitBounds(routeLine.getBounds(), {{ padding: [52, 52], maxZoom: 15 }});
    }}, 80);
    setTimeout(() => trafficMap.invalidateSize(), 500);
    summary.textContent = `${{route.destination.name || destination}}: ${{route.distance_text}}, about ${{route.duration_text}} by road.`;
    document.getElementById('mapSource').textContent = route.cached ? 'Road route from cache' : 'Road route from OSRM/OpenStreetMap';
  }} catch (error) {{
    summary.textContent = 'Allow location permission and check the destination spelling so the route can be calculated.';
  }}
}}
document.getElementById('optimizeRoutesBtn').onclick = optimizeBestRoute;
document.getElementById('layersBtn').onclick = () => {{
  usingTopoLayer = !usingTopoLayer;
  if (usingTopoLayer) {{
    trafficMap.removeLayer(streetLayer);
    topoLayer.addTo(trafficMap);
  }} else {{
    trafficMap.removeLayer(topoLayer);
    streetLayer.addTo(trafficMap);
  }}
  document.getElementById('layersBtn').textContent = usingTopoLayer ? 'Street Map' : 'Topo Map';
}};
document.getElementById('zoomInBtn').onclick = () => trafficMap.setZoom(trafficMap.getZoom() + 1);
document.getElementById('zoomOutBtn').onclick = () => trafficMap.setZoom(trafficMap.getZoom() - 1);
document.getElementById('locateBtn').onclick = () => centerOnCurrentLocation(false).catch(() => {{}});
document.getElementById('refreshTrafficBtn').onclick = refreshTrafficSummary;
initOpenMap();
refreshTrafficSummary();
refreshPublicSignal();
setInterval(refreshPublicSignal, 2500);
centerOnCurrentLocation(true).catch(() => {{}});
const readFile = file => new Promise((resolve, reject) => {{ const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(file); }});
const readArrayBuffer = file => new Promise((resolve, reject) => {{ const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsArrayBuffer(file); }});
function formatCoords(lat, lng) {{ return `${{lat.toFixed(6)}}, ${{lng.toFixed(6)}}`; }}
function gpsValue(view, offset, littleEndian) {{
  const nums = [];
  for (let i = 0; i < 3; i++) {{
    const num = view.getUint32(offset + i * 8, littleEndian);
    const den = view.getUint32(offset + i * 8 + 4, littleEndian) || 1;
    nums.push(num / den);
  }}
  return nums[0] + nums[1] / 60 + nums[2] / 3600;
}}
async function extractImageGps(file) {{
  const buffer = await readArrayBuffer(file);
  const view = new DataView(buffer);
  if (view.getUint16(0) !== 0xffd8) return '';
  let offset = 2;
  while (offset < view.byteLength) {{
    const marker = view.getUint16(offset); offset += 2;
    const size = view.getUint16(offset); offset += 2;
    if (marker === 0xffe1 && String.fromCharCode(...new Uint8Array(buffer, offset, 4)) === 'Exif') {{
      const tiff = offset + 6;
      const little = view.getUint16(tiff) === 0x4949;
      const firstIfd = tiff + view.getUint32(tiff + 4, little);
      const entries = view.getUint16(firstIfd, little);
      let gpsIfd = 0;
      for (let i = 0; i < entries; i++) {{
        const entry = firstIfd + 2 + i * 12;
        if (view.getUint16(entry, little) === 0x8825) gpsIfd = tiff + view.getUint32(entry + 8, little);
      }}
      if (!gpsIfd) return '';
      const gpsEntries = view.getUint16(gpsIfd, little);
      let latRef = 'N', lngRef = 'E', latOffset = 0, lngOffset = 0;
      for (let i = 0; i < gpsEntries; i++) {{
        const entry = gpsIfd + 2 + i * 12;
        const tag = view.getUint16(entry, little);
        if (tag === 1) latRef = String.fromCharCode(view.getUint8(entry + 8));
        if (tag === 2) latOffset = tiff + view.getUint32(entry + 8, little);
        if (tag === 3) lngRef = String.fromCharCode(view.getUint8(entry + 8));
        if (tag === 4) lngOffset = tiff + view.getUint32(entry + 8, little);
      }}
      if (!latOffset || !lngOffset) return '';
      let lat = gpsValue(view, latOffset, little);
      let lng = gpsValue(view, lngOffset, little);
      if (latRef === 'S') lat *= -1;
      if (lngRef === 'W') lng *= -1;
      return formatCoords(lat, lng);
    }}
    offset += size - 2;
  }}
  return '';
}}
async function getCurrentLocationText() {{
  const pos = await getCurrentPositionPromise();
  return formatCoords(pos.coords.latitude, pos.coords.longitude);
}}
function setLocation(el) {{ if (!navigator.geolocation) {{ el.value = 'Location unavailable'; return; }} navigator.geolocation.getCurrentPosition(pos => {{ el.value = formatCoords(pos.coords.latitude, pos.coords.longitude); }}, () => {{ el.value = 'Location permission not granted'; }}); }}
setLocation(document.getElementById('reportLocation'));
async function sendQr(code) {{
  let location = document.getElementById('reportLocation').value || 'Unknown';
  try {{ location = await getCurrentLocationText(); }} catch (error) {{}}
  const res = await fetch('/api/qr-scan', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{code, location}}) }});
  await res.json();
  document.getElementById('scanStatus').textContent = `QR scanned. Request sent to admin portal.`;
}}
async function handleScannedQr(rawValue) {{
  if (!rawValue) return;
  await sendQr(rawValue);
}}
let activeQrStream = null;
let qrScanStopped = true;
function stopQrScanner() {{
  qrScanStopped = true;
  if (activeQrStream) activeQrStream.getTracks().forEach(track => track.stop());
  activeQrStream = null;
  document.getElementById('qrVideo').classList.add('hidden');
  document.getElementById('scannerPlaceholder').classList.remove('hidden');
  document.getElementById('scanBtn').classList.remove('hidden');
  document.getElementById('stopScanBtn').classList.add('hidden');
}}
async function detectWithBarcodeDetector(video) {{
  if (!('BarcodeDetector' in window)) return '';
  const detector = new BarcodeDetector({{formats: ['qr_code']}});
  const codes = await detector.detect(video);
  return codes.length ? codes[0].rawValue : '';
}}
function detectWithJsQr(video) {{
  if (!window.jsQR || !video.videoWidth || !video.videoHeight) return '';
  const canvas = document.getElementById('qrCanvas');
  const context = canvas.getContext('2d', {{ willReadFrequently: true }});
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  const result = window.jsQR(image.data, image.width, image.height);
  return result ? result.data : '';
}}
document.getElementById('scanBtn').onclick = async () => {{
  const video = document.getElementById('qrVideo');
  const status = document.getElementById('scanStatus');
  try {{
    document.getElementById('scannerPlaceholder').classList.add('hidden');
    video.classList.remove('hidden');
    document.getElementById('scanBtn').classList.add('hidden');
    document.getElementById('stopScanBtn').classList.remove('hidden');
    status.textContent = 'Opening camera... allow camera access when prompted.';
    const stream = await navigator.mediaDevices.getUserMedia({{video: {{facingMode: {{ ideal: 'environment' }}}}}});
    activeQrStream = stream;
    qrScanStopped = false;
    video.srcObject = stream;
    await video.play();
    status.textContent = 'Camera opened. Point it at a QR code.';
    const scanFrame = async () => {{
      if (qrScanStopped) return;
      let value = '';
      try {{
        value = await detectWithBarcodeDetector(video);
      }} catch (error) {{
        value = '';
      }}
      if (!value) value = detectWithJsQr(video);
      if (value) {{
        stopQrScanner();
        await handleScannedQr(value);
        return;
      }}
      requestAnimationFrame(scanFrame);
    }};
    requestAnimationFrame(scanFrame);
  }} catch (error) {{
    status.textContent = 'Camera could not be opened. Please allow camera permission and try again.';
    stopQrScanner();
  }}
}};
document.getElementById('stopScanBtn').onclick = () => {{
  stopQrScanner();
  document.getElementById('scanStatus').textContent = 'Scanner stopped.';
}};
document.getElementById('reportForm').onsubmit = async event => {{
  event.preventDefault();
  const form = new FormData(event.target);
  const file = form.get('photo');
  let location = '';
  try {{ location = await extractImageGps(file); }} catch (error) {{ location = ''; }}
  if (!location) location = form.get('location');
  const image_data = await readFile(file);
  const payload = {{ incident_type: form.get('incident_type'), description: form.get('description'), location, image_data, image_name: file.name, image_mime: file.type }};
  const res = await fetch('/api/report', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload) }});
  const data = await res.json();
  document.getElementById('reportStatus').textContent = data.ok ? `Report sent to admin accident section from ${{location}}.` : 'Could not send report.';
  event.target.reset(); setLocation(document.getElementById('reportLocation'));
}};
</script>"""
    return html_page("I-TRAFFIC | Public Portal", body, extra_head)


def page_admin(user: dict | None) -> bytes:
    email = (user or {}).get("email", "demo.admin@traffic.local")
    extra_head = """
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIINfQHLyrcf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <style>
    #adminTrafficMap { height: 100%; width: 100%; background: #dbe7f1; }
    .leaflet-container { font-family: Inter, system-ui, sans-serif; }
    .admin-map-marker {
      display: grid;
      height: 34px;
      width: 34px;
      place-items: center;
      border: 3px solid #ffffff;
      border-radius: 999px;
      background: #fd761a;
      color: #0b1c30;
      box-shadow: 0 10px 24px rgb(15 23 42 / 0.28);
      font-weight: 900;
    }
  </style>
"""
    body = f"""
<header class="sticky top-0 z-40 flex w-full items-center justify-between border-b border-slate-200 bg-white/90 px-6 py-3 backdrop-blur">
  <div class="flex items-center gap-3"><span class="material-symbols-outlined">traffic</span><h1 class="headline text-lg font-bold">Traffic Operations Center</h1></div>
  <div class="flex items-center gap-3"><span class="h-2 w-2 rounded-full bg-green-500"></span><span class="text-xs font-bold uppercase text-slate-600">System: Live</span><span class="hidden text-xs text-slate-500 md:block">{email}</span></div>
</header>
<main class="pb-24 md:pl-16">
  <aside class="fixed left-0 top-0 z-50 hidden h-full w-16 flex-col border-r border-slate-200 bg-slate-50 pt-20 md:flex">
    <a class="grid place-items-center p-4 text-slate-500" href="#map"><span class="material-symbols-outlined">map</span></a>
    <a class="grid place-items-center bg-slate-950 p-4 text-white" href="#control"><span class="material-symbols-outlined">traffic</span></a>
    <a class="grid place-items-center p-4 text-slate-500" href="#ai"><span class="material-symbols-outlined">memory</span></a>
    <a class="grid place-items-center p-4 text-slate-500" href="#accidents"><span class="material-symbols-outlined">report_problem</span></a>
  </aside>
  <section id="map" class="relative h-[353px] w-full overflow-hidden bg-slate-200">
    <div id="adminTrafficMap" class="h-full w-full"></div>
    <div class="absolute left-4 top-4 rounded-lg border bg-white/90 p-3 shadow-sm"><p class="text-xs font-bold uppercase text-slate-400">Current Node</p><p id="currentNodeLabel" class="headline text-sm font-semibold">Silk Board Junction</p><p id="currentNodeLocation" class="mt-1 font-mono text-xs text-slate-500">12.9177, 77.6238</p></div>
    <div class="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2"><span id="mapSignalIcon" class="material-symbols-outlined signal-active text-5xl text-red-700" style="font-variation-settings: 'FILL' 1;">traffic</span></div>
  </section>
  <section class="grid grid-cols-2 border-b border-slate-200 bg-white">
    <div class="border-r p-4"><p class="text-xs font-bold uppercase text-slate-400">QR Scan Requests</p><p id="qrCount" class="font-mono text-3xl font-bold">0</p></div>
    <div class="p-4"><p class="text-xs font-bold uppercase text-slate-400">Current Signal</p><p id="signalLabel" class="font-mono text-3xl font-bold text-[#fd761a]">STOP</p></div>
  </section>
  <section class="border-b border-slate-200 bg-white p-6">
    <div class="mb-4 flex items-center justify-between"><h2 class="headline text-lg font-semibold">Incoming QR Requests</h2><span class="text-xs font-bold uppercase text-slate-400">Click a place before override</span></div>
    <div id="scanRequests" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3"></div>
  </section>
  <section id="control" class="space-y-8 p-6">
    <div><h2 class="headline mb-4 text-lg font-semibold">Manual Signal Override</h2><div class="grid grid-cols-3 gap-4">
      <button data-signal="stop" class="signal-btn rounded-lg border-2 border-red-700 bg-red-50 py-6 text-red-700"><span class="material-symbols-outlined mb-2 text-3xl" style="font-variation-settings: 'FILL' 1;">stop_circle</span><span class="block text-xs font-bold uppercase">Stop</span></button>
      <button data-signal="slow" class="signal-btn rounded-lg border-2 border-slate-200 bg-white py-6 text-slate-400"><span class="material-symbols-outlined mb-2 text-3xl" style="font-variation-settings: 'FILL' 1;">warning</span><span class="block text-xs font-bold uppercase">Slow</span></button>
      <button data-signal="go" class="signal-btn rounded-lg border-2 border-slate-200 bg-white py-6 text-slate-400"><span class="material-symbols-outlined mb-2 text-3xl" style="font-variation-settings: 'FILL' 1;">play_circle</span><span class="block text-xs font-bold uppercase">Go</span></button>
    </div></div>
    <div class="grid gap-4 md:grid-cols-2">
      <button id="laneBtn" class="rounded-lg border bg-white p-4 text-left"><span class="material-symbols-outlined">fork_right</span><p class="text-xs font-bold uppercase">Lane Diversion</p><p id="laneStatus" class="text-xs text-slate-500">Active</p></button>
      <button id="priorityBtn" class="rounded-lg border bg-white p-4 text-left"><span class="material-symbols-outlined">emergency</span><p class="text-xs font-bold uppercase">Priority Pass</p><p id="priorityStatus" class="text-xs text-slate-500">Standby</p></button>
    </div>
    <div class="space-y-3"><button id="emergencyBtn" class="w-full rounded-lg bg-slate-950 py-4 font-bold text-white">EMERGENCY CLEAR SEQUENCE</button><button data-signal="ai" class="signal-btn w-full rounded-lg border bg-white py-4 font-bold text-slate-950">RESUME AI CONTROL</button></div>
  </section>
  <section id="ai" class="border-y border-slate-200 bg-slate-50 p-6">
    <div class="mb-4 flex flex-col justify-between gap-3 md:flex-row md:items-center">
      <div><h2 class="headline text-lg font-semibold">AI Traffic Signal Control</h2><p class="text-sm text-slate-500">External AI model updates vehicle counts, density, and signal timing in real time.</p></div>
      <div class="flex gap-2"><button id="refreshAiBtn" class="rounded-lg border bg-white px-4 py-2 text-sm font-bold">Refresh AI</button><button id="applyAiBtn" class="rounded-lg bg-slate-950 px-4 py-2 text-sm font-bold text-white">Apply AI Signal</button></div>
    </div>
    <div class="mb-4 grid gap-4 lg:grid-cols-4">
      <article class="rounded-lg border bg-white p-4"><p class="text-xs font-bold uppercase text-slate-400">Cars</p><p id="aiCars" class="mt-2 font-mono text-3xl font-bold">0</p></article>
      <article class="rounded-lg border bg-white p-4"><p class="text-xs font-bold uppercase text-slate-400">Buses</p><p id="aiBuses" class="mt-2 font-mono text-3xl font-bold">0</p></article>
      <article class="rounded-lg border bg-white p-4"><p class="text-xs font-bold uppercase text-slate-400">Trucks</p><p id="aiTrucks" class="mt-2 font-mono text-3xl font-bold">0</p></article>
      <article class="rounded-lg border bg-white p-4"><p class="text-xs font-bold uppercase text-slate-400">Two Wheelers</p><p id="aiMotorcycles" class="mt-2 font-mono text-3xl font-bold">0</p></article>
    </div>
    <article class="mb-4 rounded-lg border bg-white p-4">
      <div class="mb-3 flex flex-col justify-between gap-3 md:flex-row md:items-center">
        <div><h3 class="font-bold">Traffic Signal Details</h3><p class="text-xs text-slate-500">Live signal timing and density received from the separate AI model.</p></div>
        <span class="rounded bg-slate-100 px-2 py-1 font-mono text-xs">/api/ai-traffic-update</span>
      </div>
      <div class="grid gap-3 md:grid-cols-4">
        <div class="rounded border bg-slate-50 p-3"><p class="text-xs font-bold uppercase text-slate-400">AI Signal</p><p id="signalDetailSignal" class="mt-1 font-mono text-xl font-bold">AI</p></div>
        <div class="rounded border bg-slate-50 p-3"><p class="text-xs font-bold uppercase text-slate-400">Green Time</p><p id="signalDetailGreen" class="mt-1 font-mono text-xl font-bold">0s</p></div>
        <div class="rounded border bg-slate-50 p-3"><p class="text-xs font-bold uppercase text-slate-400">Density</p><p id="signalDetailDensity" class="mt-1 font-mono text-xl font-bold">0%</p></div>
        <div class="rounded border bg-slate-50 p-3"><p class="text-xs font-bold uppercase text-slate-400">Total Vehicles</p><p id="signalDetailTotal" class="mt-1 font-mono text-xl font-bold">0</p></div>
      </div>
    </article>
    <div class="grid gap-4 lg:grid-cols-3">
      <article class="rounded-lg border bg-white p-4">
        <p class="text-xs font-bold uppercase text-slate-400">Recommended Signal</p>
        <p id="aiSignal" class="mt-2 font-mono text-3xl font-bold text-[#fd761a]">AI</p>
        <p id="aiReason" class="mt-2 text-sm text-slate-600">Waiting for camera density feed...</p>
      </article>
      <article class="rounded-lg border bg-white p-4">
        <p class="text-xs font-bold uppercase text-slate-400">Camera Confidence</p>
        <p id="aiConfidence" class="mt-2 font-mono text-3xl font-bold">0%</p>
        <p id="aiCycle" class="mt-2 text-sm text-slate-600">Signal cycle not calculated yet.</p>
      </article>
      <article class="rounded-lg border bg-white p-4">
        <p class="text-xs font-bold uppercase text-slate-400">Priority Lane</p>
        <p id="aiLane" class="mt-2 text-xl font-bold">-</p>
        <p id="aiStatus" class="mt-2 text-sm text-slate-600">Waiting for AI model data.</p>
      </article>
    </div>
    <div id="aiLanes" class="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4"></div>
  </section>
  <section id="accidents" class="p-6">
    <div class="mb-4 flex items-center justify-between"><h2 class="headline text-lg font-semibold">Accident Section</h2><span id="reportCount" class="rounded-full bg-orange-100 px-3 py-1 text-xs font-bold text-orange-700">0 reports</span></div>
    <div id="reports" class="grid gap-4 md:grid-cols-2 xl:grid-cols-3"></div>
  </section>
</main>
<nav class="fixed bottom-0 left-0 right-0 z-50 flex justify-around border-t bg-white px-4 py-3 md:hidden"><a class="text-xs font-bold" href="#map">Map</a><a class="text-xs font-bold" href="#control">Control</a><a class="text-xs font-bold" href="#ai">AI</a><a class="text-xs font-bold" href="#accidents">Accidents</a></nav>
<script>
async function postJson(url, payload) {{ const res = await fetch(url, {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(payload) }}); return res.json(); }}
let selectedControlPlace = {{ label: 'Silk Board Junction', placeLocation: '12.9177, 77.6238' }};
let adminTrafficMap;
let adminNodeMarker;
function parseControlCoordinates(placeLocation) {{
  const match = String(placeLocation || '').match(/(-?[0-9]+(?:\\.[0-9]+)?)\\s*,\\s*(-?[0-9]+(?:\\.[0-9]+)?)/);
  if (!match) return null;
  const lat = Number(match[1]);
  const lng = Number(match[2]);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  if (Math.abs(lat) > 90 || Math.abs(lng) > 180) return null;
  return [lat, lng];
}}
function initAdminMap() {{
  if (!window.L || adminTrafficMap) return;
  adminTrafficMap = L.map('adminTrafficMap', {{ zoomControl: false }}).setView([12.9177, 77.6238], 15);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }}).addTo(adminTrafficMap);
  L.control.zoom({{ position: 'bottomright' }}).addTo(adminTrafficMap);
  adminNodeMarker = L.marker([12.9177, 77.6238], {{
    icon: L.divIcon({{ className: '', html: '<div class="admin-map-marker">T</div>', iconSize: [34, 34], iconAnchor: [17, 17] }})
  }}).addTo(adminTrafficMap).bindPopup('Silk Board Junction');
  setTimeout(() => adminTrafficMap.invalidateSize(), 150);
  setTimeout(() => adminTrafficMap.invalidateSize(), 700);
  window.addEventListener('resize', () => adminTrafficMap.invalidateSize());
}}
function updateAdminMap(label, placeLocation) {{
  const coords = parseControlCoordinates(placeLocation);
  if (!coords || !adminTrafficMap) return;
  adminTrafficMap.setView(coords, 16);
  adminTrafficMap.invalidateSize();
  if (!adminNodeMarker) return;
  adminNodeMarker.setLatLng(coords);
  adminNodeMarker.bindPopup(label || 'Selected traffic node');
}}
function selectControlPlace(label, placeLocation, jumpToControl = true) {{
  selectedControlPlace = {{ label: label || 'Selected traffic node', placeLocation: placeLocation || 'Unknown location' }};
  document.getElementById('currentNodeLabel').textContent = selectedControlPlace.label;
  document.getElementById('currentNodeLocation').textContent = selectedControlPlace.placeLocation;
  updateAdminMap(selectedControlPlace.label, selectedControlPlace.placeLocation);
  if (jumpToControl) window.location.hash = 'control';
}}
function paintSignal(signal) {{
  const label = document.getElementById('signalLabel'); const icon = document.getElementById('mapSignalIcon');
  label.textContent = signal.toUpperCase(); icon.className = 'material-symbols-outlined signal-active text-5xl ' + (signal === 'go' ? 'text-green-700' : signal === 'slow' ? 'text-orange-500' : signal === 'ai' ? 'text-blue-700' : 'text-red-700');
  document.querySelectorAll('.signal-btn').forEach(btn => {{ const active = btn.dataset.signal === signal; btn.classList.toggle('border-slate-950', active); btn.classList.toggle('text-slate-950', active); }});
}}
function renderScanRequests(scans) {{
  document.getElementById('scanRequests').innerHTML = scans.map(scan => `
    <article class="rounded-lg border bg-slate-50 p-4">
      <div class="flex items-start justify-between gap-3">
        <div>
          <h3 class="font-bold text-slate-900">${{scan.code}}</h3>
          <p class="mt-1 flex items-center gap-1 font-mono text-xs text-slate-500"><span class="material-symbols-outlined text-sm">location_on</span>${{scan.location || 'Unknown location'}}</p>
          <p class="mt-1 text-xs text-slate-400">From: ${{scan.user_email || 'public scanner'}}</p>
        </div>
        <button class="control-place-btn rounded bg-slate-950 px-3 py-2 text-xs font-bold text-white" data-label="${{scan.code}}" data-location="${{scan.location || ''}}">Control</button>
      </div>
    </article>`).join('') || '<p class="text-slate-500">No QR scan requests yet.</p>';
  document.querySelectorAll('.control-place-btn').forEach(btn => btn.onclick = () => selectControlPlace(btn.dataset.label, btn.dataset.location));
}}
function renderReports(reports) {{
  document.getElementById('reportCount').textContent = `${{reports.length}} reports`;
  document.getElementById('reports').innerHTML = reports.map(r => `
    <article class="overflow-hidden rounded-lg border bg-white shadow-sm">
      ${{r.image_url ? `<img src="${{r.image_url}}" class="h-48 w-full object-cover" alt="Accident photo">` : ''}}
      <div class="space-y-2 p-4">
        <div class="flex items-center justify-between gap-2"><h3 class="font-bold">${{r.incident_type}}</h3><span class="text-xs text-slate-500">#${{r.id}}</span></div>
        <p class="text-sm text-slate-600">${{r.description || 'No description provided.'}}</p>
        <p class="flex items-center gap-1 text-xs font-mono text-slate-500"><span class="material-symbols-outlined text-sm">location_on</span>${{r.location || 'Unknown location'}}</p>
        <div class="flex flex-wrap gap-2">
          <a class="inline-block text-xs font-bold text-orange-700 underline" href="${{r.image_url}}" target="_blank">Open image</a>
          <button class="control-place-btn rounded bg-slate-950 px-3 py-2 text-xs font-bold text-white" data-label="${{r.incident_type}} #${{r.id}}" data-location="${{r.location || ''}}">Control this place</button>
        </div>
      </div>
    </article>`).join('') || '<p class="text-slate-500">No accident reports yet.</p>';
  document.querySelectorAll('#reports .control-place-btn').forEach(btn => btn.onclick = () => selectControlPlace(btn.dataset.label, btn.dataset.location));
}}
function renderAiTraffic(ai) {{
  const counts = ai.vehicle_counts || {{}};
  document.getElementById('aiSignal').textContent = ai.recommended_signal.toUpperCase();
  document.getElementById('aiReason').textContent = ai.reason;
  document.getElementById('aiConfidence').textContent = `${{ai.confidence}}%`;
  document.getElementById('aiCycle').textContent = `Green window: ${{ai.green_seconds}} seconds`;
  document.getElementById('aiLane').textContent = ai.priority_lane;
  document.getElementById('aiStatus').textContent = `Updated ${{ai.updated_at}}`;
  document.getElementById('aiCars').textContent = counts.cars || 0;
  document.getElementById('aiBuses').textContent = counts.buses || 0;
  document.getElementById('aiTrucks').textContent = counts.trucks || 0;
  document.getElementById('aiMotorcycles').textContent = counts.motorcycles || 0;
  document.getElementById('signalDetailSignal').textContent = ai.recommended_signal.toUpperCase();
  document.getElementById('signalDetailGreen').textContent = `${{ai.green_seconds}}s`;
  document.getElementById('signalDetailDensity').textContent = `${{ai.average_density}}%`;
  document.getElementById('signalDetailTotal').textContent = counts.total || ai.lanes.reduce((sum, lane) => sum + lane.vehicle_count, 0);
  document.getElementById('aiLanes').innerHTML = ai.lanes.map(lane => `
    <article class="rounded-lg border bg-white p-4">
      <div class="mb-2 flex items-center justify-between gap-2">
        <h3 class="font-bold">${{lane.name}}</h3>
        <span class="rounded bg-slate-100 px-2 py-1 font-mono text-xs">${{lane.density}}%</span>
      </div>
      <div class="h-2 overflow-hidden rounded bg-slate-100"><div class="h-full rounded bg-[#fd761a]" style="width: ${{lane.density}}%"></div></div>
      <p class="mt-2 text-xs text-slate-500">${{lane.vehicle_count}} vehicles - ${{lane.source || 'ai model'}}</p>
    </article>`).join('');
}}
async function refresh() {{
  const data = await (await fetch('/api/admin-state')).json();
  document.getElementById('qrCount').textContent = data.total_scans;
  document.getElementById('laneStatus').textContent = data.signal.lane_diversion ? 'Active' : 'Standby';
  document.getElementById('priorityStatus').textContent = data.signal.priority_pass ? 'Active' : 'Standby';
  if (data.signal.target_label && data.signal.target_location) selectControlPlace(data.signal.target_label, data.signal.target_location, false);
  paintSignal(data.signal.signal);
  renderScanRequests(data.latest_scans);
  renderReports(data.reports);
  renderAiTraffic(data.traffic_ai);
}}
document.querySelectorAll('.signal-btn').forEach(btn => btn.onclick = async () => {{ await postJson('/api/signal', {{signal: btn.dataset.signal, target_label: selectedControlPlace.label, target_location: selectedControlPlace.placeLocation}}); refresh(); }});
document.getElementById('laneBtn').onclick = async () => {{ await postJson('/api/control-toggle', {{field: 'lane_diversion', target_label: selectedControlPlace.label, target_location: selectedControlPlace.placeLocation}}); refresh(); }};
document.getElementById('priorityBtn').onclick = async () => {{ await postJson('/api/control-toggle', {{field: 'priority_pass', target_label: selectedControlPlace.label, target_location: selectedControlPlace.placeLocation}}); refresh(); }};
document.getElementById('emergencyBtn').onclick = async () => {{ await postJson('/api/signal', {{signal: 'go', priority_pass: 1, target_label: selectedControlPlace.label, target_location: selectedControlPlace.placeLocation}}); refresh(); }};
document.getElementById('refreshAiBtn').onclick = refresh;
document.getElementById('applyAiBtn').onclick = async () => {{ const res = await postJson('/api/ai-apply', {{target_label: selectedControlPlace.label, target_location: selectedControlPlace.placeLocation}}); document.getElementById('aiStatus').textContent = res.ok ? 'AI recommendation applied to live signal.' : (res.error || 'AI apply failed.'); refresh(); }};
initAdminMap(); updateAdminMap(selectedControlPlace.label, selectedControlPlace.placeLocation);
refresh(); setInterval(refresh, 2500);
</script>"""
    return html_page("Traffic Operations Center - Manual Control", body, extra_head)


class TrafficHandler(BaseHTTPRequestHandler):
    server_version = "TrafficControlOS/1.0"

    def send_bytes(self, data: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, status: int = 302, user: dict | None = None) -> None:
        self.send_response(status)
        if user:
            set_session(self, user)
        self.send_header("Location", location)
        self.end_headers()

    def json_response(self, payload: dict, status: int = 200) -> None:
        self.send_bytes(json.dumps(payload).encode("utf-8"), status, "application/json")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        user = current_user(self)

        if path == "/":
            self.send_bytes(page_login())
        elif path == "/public":
            self.send_bytes(page_public(user))
        elif path == "/admin-portal":
            self.redirect("/admin/dashboard")
        elif path == "/admin/dashboard":
            if not user or not is_admin_email(user.get("email", "")):
                denied_email = urllib.parse.quote((user or {}).get("email", "not signed in"))
                self.redirect(f"/access-denied?email={denied_email}")
                return
            self.send_bytes(page_admin(user))
        elif path == "/auth/google":
            self.start_google_auth(query)
        elif path == "/auth/google/callback":
            self.finish_google_auth(query)
        elif path == "/access-denied":
            self.send_bytes(page_access_denied(query.get("email", [""])[0]))
        elif path == "/qr-direct":
            self.record_direct_qr(query, user)
        elif path == "/api/admin-state":
            if not is_admin_user(user):
                self.json_response({"ok": False, "error": "Admin access required"}, 403)
                return
            self.json_response(admin_state())
        elif path == "/api/public-signal":
            self.json_response(public_signal_state())
        elif path == "/api/route":
            self.json_response(route_summary(query))
        elif path == "/api/traffic-summary":
            self.json_response(traffic_summary(query))
        elif path.startswith("/uploads/"):
            self.serve_upload(path)
        else:
            self.send_bytes(b"Not found", 404, "text/plain")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        user = current_user(self) or {}
        try:
            if parsed.path == "/api/qr-scan":
                payload = self.read_json()
                db_execute(
                    "INSERT INTO qr_scans (code, location, user_email, created_at) VALUES (?, ?, ?, ?)",
                    (payload.get("code", "UNKNOWN"), payload.get("location", ""), user.get("email", ""), int(time.time())),
                )
                total = db_rows("SELECT COUNT(*) AS count FROM qr_scans")[0]["count"]
                self.json_response({"ok": True, "total_scans": total})
            elif parsed.path == "/api/report":
                self.save_report(self.read_json(), user)
            elif parsed.path == "/api/signal":
                if not is_admin_user(user):
                    self.json_response({"ok": False, "error": "Admin access required"}, 403)
                    return
                payload = self.read_json()
                signal = payload.get("signal", "stop")
                priority = int(payload.get("priority_pass", -1))
                target_label = payload.get("target_label", "Silk Board Junction")
                target_location = payload.get("target_location", "12.9177, 77.6238")
                if priority >= 0:
                    db_execute(
                        "UPDATE signal_state SET signal = ?, priority_pass = ?, target_label = ?, target_location = ?, updated_at = ? WHERE id = 1",
                        (signal, priority, target_label, target_location, int(time.time())),
                    )
                else:
                    db_execute(
                        "UPDATE signal_state SET signal = ?, target_label = ?, target_location = ?, updated_at = ? WHERE id = 1",
                        (signal, target_label, target_location, int(time.time())),
                    )
                self.json_response({"ok": True})
            elif parsed.path == "/api/control-toggle":
                if not is_admin_user(user):
                    self.json_response({"ok": False, "error": "Admin access required"}, 403)
                    return
                payload = self.read_json()
                field = payload.get("field")
                if field not in {"lane_diversion", "priority_pass"}:
                    self.json_response({"ok": False, "error": "Invalid field"}, 400)
                    return
                target_label = payload.get("target_label", "Silk Board Junction")
                target_location = payload.get("target_location", "12.9177, 77.6238")
                db_execute(
                    f"UPDATE signal_state SET {field} = CASE {field} WHEN 1 THEN 0 ELSE 1 END, target_label = ?, target_location = ?, updated_at = ? WHERE id = 1",
                    (target_label, target_location, int(time.time())),
                )
                self.json_response({"ok": True})
            elif parsed.path == "/api/ai-apply":
                if not is_admin_user(user):
                    self.json_response({"ok": False, "error": "Admin access required"}, 403)
                    return
                payload = self.read_json()
                ai = traffic_ai_state()
                target_label = payload.get("target_label") or ai["priority_lane"]
                target_location = payload.get("target_location") or "AI camera network"
                db_execute(
                    "UPDATE signal_state SET signal = ?, priority_pass = ?, target_label = ?, target_location = ?, updated_at = ? WHERE id = 1",
                    (ai["recommended_signal"], 1 if ai["emergency_priority"] else 0, target_label, target_location, int(time.time())),
                )
                self.json_response({"ok": True, "traffic_ai": ai})
            elif parsed.path == "/api/camera-density":
                if not is_admin_user(user):
                    self.json_response({"ok": False, "error": "Admin access required"}, 403)
                    return
                update_camera_feed(self.read_json(), source="browser-camera")
                self.json_response({"ok": True, "traffic_ai": traffic_ai_state()})
            elif parsed.path == "/api/iot/camera-density":
                payload = self.read_json()
                token = payload.get("token", "")
                auth_header = self.headers.get("Authorization", "")
                bearer = auth_header.removeprefix("Bearer ").strip()
                if token != IOT_NODE_TOKEN and bearer != IOT_NODE_TOKEN:
                    self.json_response({"ok": False, "error": "Invalid IoT node token"}, 403)
                    return
                update_camera_feed(payload, source="esp32-cam-node")
                self.json_response({"ok": True, "traffic_ai": traffic_ai_state()})
            elif parsed.path == "/api/ai-traffic-update":
                payload = self.read_json()
                token = payload.get("token", "")
                auth_header = self.headers.get("Authorization", "")
                bearer = auth_header.removeprefix("Bearer ").strip()
                if token != IOT_NODE_TOKEN and bearer != IOT_NODE_TOKEN:
                    self.json_response({"ok": False, "error": "Invalid AI model token"}, 403)
                    return
                ai = update_ai_model_feed(payload)
                self.json_response({"ok": True, "traffic_ai": ai})
            elif parsed.path == "/api/esp32-worker/start":
                if not is_admin_user(user):
                    self.json_response({"ok": False, "error": "Admin access required"}, 403)
                    return
                payload = self.read_json()
                result = start_esp32_worker(payload.get("stream_url", ""), payload.get("lane", "Eastbound camera"))
                self.json_response(result, 200 if result.get("ok") else 400)
            elif parsed.path == "/api/esp32-worker/stop":
                if not is_admin_user(user):
                    self.json_response({"ok": False, "error": "Admin access required"}, 403)
                    return
                stop_esp32_worker()
                self.json_response({"ok": True, "esp32_worker": esp32_worker_state()})
            else:
                self.json_response({"ok": False, "error": "Not found"}, 404)
        except Exception as exc:
            self.json_response({"ok": False, "error": str(exc)}, 500)

    def start_google_auth(self, query: dict[str, list[str]]) -> None:
        role = query.get("role", ["public"])[0]
        if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
            state = secrets.token_urlsafe(16) + ":" + role
            callback_url = redirect_uri(self)
            params = urllib.parse.urlencode(
                {
                    "client_id": GOOGLE_CLIENT_ID,
                    "redirect_uri": callback_url,
                    "response_type": "code",
                    "scope": "openid email profile",
                    "state": state,
                    "access_type": "offline",
                    "prompt": "select_account",
                }
            )
            self.redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
            return
        demo_email = sorted(ADMIN_EMAILS)[0] if role == "admin" else f"demo.{role}@traffic.local"
        demo_user = {"email": demo_email, "name": f"Demo {role.title()} User", "role": role, "picture": ""}
        self.redirect("/admin/dashboard" if role == "admin" else "/public", user=demo_user)

    def finish_google_auth(self, query: dict[str, list[str]]) -> None:
        code = query.get("code", [""])[0]
        state = query.get("state", ["token:public"])[0]
        role = state.split(":")[-1] if ":" in state else "public"
        data = urllib.parse.urlencode(
            {
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri(self),
                "grant_type": "authorization_code",
            }
        ).encode()
        token_req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, headers={"Accept": "application/json"})
        with urllib.request.urlopen(token_req, timeout=15) as response:
            token = json.loads(response.read().decode())
        user_req = urllib.request.Request("https://www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": f"Bearer {token['access_token']}"})
        with urllib.request.urlopen(user_req, timeout=15) as response:
            google_user = json.loads(response.read().decode())
        user = {"email": google_user.get("email", ""), "name": google_user.get("name", ""), "picture": google_user.get("picture", ""), "role": role}
        if role == "admin" and not is_admin_email(user["email"]):
            denied_email = urllib.parse.quote(user["email"])
            self.redirect(f"/access-denied?email={denied_email}")
            return
        self.redirect("/admin/dashboard" if role == "admin" else "/public", user=user)

    def save_report(self, payload: dict, user: dict) -> None:
        image_data = payload.get("image_data", "")
        image_mime = payload.get("image_mime", "image/jpeg")
        ext = ".jpg"
        if "png" in image_mime:
            ext = ".png"
        elif "webp" in image_mime:
            ext = ".webp"
        filename = f"accident_{int(time.time())}_{secrets.token_hex(4)}{ext}"
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]
        (UPLOAD_DIR / filename).write_bytes(base64.b64decode(image_data))
        db_execute(
            """
            INSERT INTO accident_reports
            (incident_type, description, location, image_filename, image_mime, user_email, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("incident_type", "Vehicle Collision"),
                payload.get("description", ""),
                payload.get("location", ""),
                filename,
                image_mime,
                user.get("email", ""),
                int(time.time()),
            ),
        )
        self.json_response({"ok": True, "image_url": f"/uploads/{filename}"})

    def record_direct_qr(self, query: dict[str, list[str]], user: dict | None) -> None:
        code = query.get("code", ["DIRECT-QR"])[0]
        location = query.get("location", ["QR opened from generated public portal code"])[0]
        db_execute(
            "INSERT INTO qr_scans (code, location, user_email, created_at) VALUES (?, ?, ?, ?)",
            (code, location, (user or {}).get("email", "direct-qr-scan"), int(time.time())),
        )
        total = db_rows("SELECT COUNT(*) AS count FROM qr_scans")[0]["count"]
        accepts = self.headers.get("Accept", "")
        if "text/html" in accepts and "application/json" not in accepts:
            body = f"""
<main class="grid min-h-screen place-items-center bg-[#f8f9ff] p-6 font-sans text-[#0b1c30]">
  <section class="w-full max-w-md rounded-lg border border-slate-200 bg-white p-8 text-center shadow-sm">
    <span class="material-symbols-outlined mb-4 text-5xl text-green-600">check_circle</span>
    <h1 class="headline mb-2 text-2xl font-bold">Request Sent</h1>
    <p class="mb-4 text-slate-600">QR code <b>{code}</b> has been sent to the admin portal.</p>
    <p class="rounded bg-slate-50 p-3 font-mono text-sm text-slate-500">Total QR requests: {total}</p>
    <a class="mt-6 inline-block rounded-lg bg-slate-950 px-4 py-3 font-bold text-white" href="/public#scan">Back to Public Portal</a>
  </section>
</main>"""
            self.send_bytes(html_page("QR Request Sent", body))
            return
        self.json_response({"ok": True, "code": code, "total_scans": total})

    def serve_upload(self, path: str) -> None:
        filename = Path(urllib.parse.unquote(path)).name
        target = UPLOAD_DIR / filename
        if not target.exists():
            self.send_bytes(b"Not found", 404, "text/plain")
            return
        mime = "image/png" if filename.endswith(".png") else "image/webp" if filename.endswith(".webp") else "image/jpeg"
        self.send_bytes(target.read_bytes(), 200, mime)

    def log_message(self, fmt: str, *args) -> None:
        safe_print(f"{self.address_string()} - {fmt % args}")


def update_camera_feed(payload: dict, source: str) -> None:
    lane = payload.get("lane", "Eastbound camera")
    density = max(0, min(100, int(payload.get("density", 0))))
    vehicle_count = max(0, int(payload.get("vehicle_count", 0)))
    confidence = max(0, min(100, int(payload.get("confidence", 70))))
    vehicle_counts = normalize_vehicle_counts(payload.get("vehicle_counts", {}), vehicle_count)
    CAMERA_FEED[lane] = {
        "name": lane,
        "density": density,
        "vehicle_count": vehicle_count,
        "vehicle_counts": vehicle_counts,
        "confidence": confidence,
        "source": payload.get("source", source),
        "camera_url": payload.get("camera_url", ""),
        "updated_at": int(time.time()),
    }


def normalize_vehicle_counts(raw_counts: object, fallback_total: int = 0) -> dict:
    counts = raw_counts if isinstance(raw_counts, dict) else {}
    cars = max(0, int(counts.get("cars", counts.get("car", 0)) or 0))
    buses = max(0, int(counts.get("buses", counts.get("bus", 0)) or 0))
    trucks = max(0, int(counts.get("trucks", counts.get("truck", 0)) or 0))
    motorcycles = max(0, int(counts.get("motorcycles", counts.get("motorcycle", counts.get("bikes", 0))) or 0))
    bicycles = max(0, int(counts.get("bicycles", counts.get("bicycle", 0)) or 0))
    trains = max(0, int(counts.get("trains", counts.get("train", 0)) or 0))
    total = max(fallback_total, cars + buses + trucks + motorcycles + bicycles + trains)
    return {
        "cars": cars,
        "buses": buses,
        "trucks": trucks,
        "motorcycles": motorcycles,
        "bicycles": bicycles,
        "trains": trains,
        "total": total,
    }


def merge_vehicle_counts(lanes: list[dict]) -> dict:
    total = {"cars": 0, "buses": 0, "trucks": 0, "motorcycles": 0, "bicycles": 0, "trains": 0, "total": 0}
    for lane in lanes:
        counts = normalize_vehicle_counts(lane.get("vehicle_counts", {}), int(lane.get("vehicle_count", 0)))
        for key in total:
            total[key] += counts[key]
    return total


def signal_for_density(density: int, average_density: int) -> str:
    if density >= 65:
        return "go"
    if average_density >= 35:
        return "slow"
    return "ai"


def update_ai_model_feed(payload: dict) -> dict:
    lane = payload.get("lane", "Eastbound camera")
    vehicle_counts = normalize_vehicle_counts(payload.get("vehicle_counts", {}), int(payload.get("vehicle_count", 0) or 0))
    vehicle_count = int(payload.get("vehicle_count", vehicle_counts["total"]) or vehicle_counts["total"])
    density = max(0, min(100, int(payload.get("density", min(100, vehicle_count * 8)) or 0)))
    confidence = max(0, min(100, int(payload.get("confidence", 80) or 80)))
    recommended_signal = payload.get("recommended_signal") or signal_for_density(density, density)
    green_seconds = max(15, min(120, int(payload.get("green_seconds", 20 + density) or 20 + density)))

    update_camera_feed(
        {
            "lane": lane,
            "vehicle_count": vehicle_count,
            "vehicle_counts": vehicle_counts,
            "density": density,
            "confidence": confidence,
            "source": payload.get("source", "separate-ai-model"),
        },
        source="separate-ai-model",
    )
    AI_SIGNAL_STATE.update(
        {
            "last_update": int(time.time()),
            "signal": recommended_signal,
            "green_seconds": green_seconds,
            "vehicle_counts": vehicle_counts,
        }
    )
    db_execute(
        "UPDATE signal_state SET signal = ?, priority_pass = ?, target_label = ?, target_location = ?, updated_at = ? WHERE id = 1",
        (
            recommended_signal,
            1 if density >= 75 else 0,
            payload.get("target_label", lane),
            payload.get("target_location", "AI model traffic node"),
            int(time.time()),
        ),
    )
    return traffic_ai_state()


def esp32_worker_state() -> dict:
    return {
        "running": bool(ESP32_WORKER.get("running")),
        "stream_url": ESP32_WORKER.get("stream_url", ""),
        "lane": ESP32_WORKER.get("lane", ""),
        "status": ESP32_WORKER.get("status", "Auto count is off."),
        "last_count": int(ESP32_WORKER.get("last_count", 0) or 0),
        "last_density": int(ESP32_WORKER.get("last_density", 0) or 0),
        "updated_at": ESP32_WORKER.get("updated_at", 0),
    }


def stop_esp32_worker() -> None:
    stop_event = ESP32_WORKER.get("stop_event")
    if stop_event:
        stop_event.set()
    ESP32_WORKER["running"] = False
    ESP32_WORKER["status"] = "Auto count is off."
    ESP32_WORKER["updated_at"] = int(time.time())


def start_esp32_worker(stream_url: str, lane: str) -> dict:
    stream_url = stream_url.strip()
    lane = lane.strip() or "Eastbound camera"
    if not stream_url:
        return {"ok": False, "error": "ESP32-CAM stream URL is required."}
    try:
        import cv2  # type: ignore
    except ImportError:
        return {"ok": False, "error": "OpenCV is missing. Run: python -m pip install opencv-python"}

    stop_esp32_worker()
    stop_event = threading.Event()
    ESP32_WORKER.update(
        {
            "running": True,
            "stop_event": stop_event,
            "stream_url": stream_url,
            "lane": lane,
            "status": "Auto count starting...",
            "last_count": 0,
            "last_density": 0,
            "updated_at": int(time.time()),
        }
    )
    thread = threading.Thread(target=esp32_worker_loop, args=(cv2, stream_url, lane, stop_event), daemon=True)
    ESP32_WORKER["thread"] = thread
    thread.start()
    return {"ok": True, "esp32_worker": esp32_worker_state()}


def set_esp32_worker_status(status: str, *, running: bool | None = None) -> None:
    ESP32_WORKER["status"] = status
    ESP32_WORKER["updated_at"] = int(time.time())
    if running is not None:
        ESP32_WORKER["running"] = running


def open_esp32_capture(cv2, stream_url: str):
    backend = getattr(cv2, "CAP_FFMPEG", 0)
    timeout_props = []
    open_timeout = getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None)
    read_timeout = getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None)
    if open_timeout is not None:
        timeout_props.extend([open_timeout, 5000])
    if read_timeout is not None:
        timeout_props.extend([read_timeout, 5000])
    try:
        if timeout_props:
            return cv2.VideoCapture(stream_url, backend, timeout_props)
    except Exception:
        pass
    return cv2.VideoCapture(stream_url, backend)


def esp32_worker_loop(cv2, stream_url: str, lane: str, stop_event: threading.Event) -> None:
    capture = None
    background = None
    calibration_motion: list[float] = []
    motion_floor = 0.0
    try:
        set_esp32_worker_status(f"Connecting to ESP32-CAM stream for {lane}...")
        capture = open_esp32_capture(cv2, stream_url)
        if not capture.isOpened():
            set_esp32_worker_status("Auto count could not open the ESP32-CAM stream. Check that this PC can reach the URL.", running=False)
            return

        set_esp32_worker_status(f"Auto count connected. Calibrating motion for {lane}...")
        last_update = 0.0
        while not stop_event.is_set():
            ok, frame = capture.read()
            if not ok:
                set_esp32_worker_status("Auto count lost stream. Reconnecting...")
                capture.release()
                time.sleep(1)
                capture = open_esp32_capture(cv2, stream_url)
                background = None
                calibration_motion = []
                motion_floor = 0.0
                continue

            frame = cv2.resize(frame, (640, 360))
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            if background is None:
                background = gray
                update_camera_feed(
                    {
                        "lane": lane,
                        "vehicle_count": 0,
                        "density": 0,
                        "confidence": 60,
                        "camera_url": stream_url,
                    },
                    source="esp32-auto-count",
                )
                ESP32_WORKER["last_count"] = 0
                ESP32_WORKER["last_density"] = 0
                set_esp32_worker_status(f"Auto count is receiving frames for {lane}. Waiting for motion...")
                continue

            delta = cv2.absdiff(background, gray)
            threshold = cv2.threshold(delta, 32, 255, cv2.THRESH_BINARY)[1]
            threshold = cv2.dilate(threshold, None, iterations=2)
            contours, _ = cv2.findContours(threshold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            moving_regions = []
            for contour in contours:
                area = cv2.contourArea(contour)
                if area < 1200:
                    continue
                x, y, width, height = cv2.boundingRect(contour)
                if width < 35 or height < 20:
                    continue
                moving_regions.append(contour)

            total_motion_area = sum(cv2.contourArea(contour) for contour in moving_regions)
            if len(calibration_motion) < 12:
                calibration_motion.append(total_motion_area)
                motion_floor = sorted(calibration_motion)[len(calibration_motion) // 2]
                if time.time() - last_update >= 1.5:
                    update_camera_feed(
                        {
                            "lane": lane,
                            "vehicle_count": 0,
                            "density": 0,
                            "confidence": 60,
                            "camera_url": stream_url,
                        },
                        source="esp32-auto-count",
                    )
                    ESP32_WORKER["last_count"] = 0
                    ESP32_WORKER["last_density"] = 0
                    set_esp32_worker_status(f"Auto count calibrating empty-frame noise for {lane}...")
                    last_update = time.time()
                background = cv2.addWeighted(background, 0.95, gray, 0.05, 0)
                continue

            active_motion_area = max(0.0, total_motion_area - motion_floor)
            if active_motion_area < 7000 or len(moving_regions) == 0:
                vehicle_count = 0
            else:
                area_estimate = round(active_motion_area / 12000)
                vehicle_count = min(12, max(len(moving_regions), area_estimate))
            density = min(100, round(vehicle_count / 12 * 100))
            confidence = 65 if vehicle_count == 0 else min(92, 70 + vehicle_count * 2)

            if time.time() - last_update >= 1.5:
                update_camera_feed(
                    {
                        "lane": lane,
                        "vehicle_count": vehicle_count,
                        "density": density,
                        "confidence": confidence,
                        "camera_url": stream_url,
                    },
                    source="esp32-auto-count",
                )
                ESP32_WORKER["last_count"] = vehicle_count
                ESP32_WORKER["last_density"] = density
                set_esp32_worker_status(f"Auto count live: {vehicle_count} vehicles, {density}% density.")
                last_update = time.time()

            background = cv2.addWeighted(background, 0.92, gray, 0.08, 0)
    except Exception as exc:
        set_esp32_worker_status(f"Auto count error: {exc}", running=False)
    finally:
        if capture:
            capture.release()
        if not stop_event.is_set() and ESP32_WORKER.get("running"):
            ESP32_WORKER["running"] = False


def admin_state() -> dict:
    signal = db_rows("SELECT signal, lane_diversion, priority_pass, target_label, target_location, updated_at FROM signal_state WHERE id = 1")[0]
    total_scans = db_rows("SELECT COUNT(*) AS count FROM qr_scans")[0]["count"]
    latest_scans = db_rows("SELECT * FROM qr_scans ORDER BY id DESC LIMIT 10")
    reports = db_rows("SELECT * FROM accident_reports ORDER BY id DESC LIMIT 30")
    for report in reports:
        report["image_url"] = f"/uploads/{report['image_filename']}" if report.get("image_filename") else ""
    return {
        "signal": signal,
        "total_scans": total_scans,
        "latest_scans": latest_scans,
        "reports": reports,
        "traffic_ai": traffic_ai_state(),
        "esp32_worker": esp32_worker_state(),
    }


def public_signal_state() -> dict:
    signal = db_rows("SELECT signal, lane_diversion, priority_pass, target_label, target_location, updated_at FROM signal_state WHERE id = 1")[0]
    return {"ok": True, "signal": signal}


def traffic_ai_state() -> dict:
    now = int(time.time())
    live_lanes = [feed for feed in CAMERA_FEED.values() if now - int(feed.get("updated_at", 0)) <= 20]
    if live_lanes:
        lane_names = ["Northbound camera", "Southbound camera", "Eastbound camera", "Westbound camera"]
        camera_lanes = []
        for lane_name in lane_names:
            feed = CAMERA_FEED.get(lane_name)
            if feed and now - int(feed.get("updated_at", 0)) <= 20:
                camera_lanes.append(
                    {
                        "name": lane_name,
                        "density": int(feed.get("density", 0)),
                        "vehicle_count": int(feed.get("vehicle_count", 0)),
                        "vehicle_counts": normalize_vehicle_counts(feed.get("vehicle_counts", {}), int(feed.get("vehicle_count", 0))),
                        "source": feed.get("source", "camera-node"),
                    }
                )
            else:
                camera_lanes.append({"name": lane_name, "density": 0, "vehicle_count": 0, "vehicle_counts": normalize_vehicle_counts({}, 0), "source": "idle"})
        priority = max(camera_lanes, key=lambda lane: lane["density"])
        avg_density = round(sum(lane["density"] for lane in camera_lanes) / len(camera_lanes))
        confidence = max(int(feed.get("confidence", 70)) for feed in live_lanes)
        recommended_signal = str(AI_SIGNAL_STATE.get("signal") or signal_for_density(priority["density"], avg_density))
        green_seconds = int(AI_SIGNAL_STATE.get("green_seconds") or min(95, max(20, 20 + priority["density"])))
        vehicle_counts = merge_vehicle_counts(camera_lanes)
        return {
            "mode": "separate_ai_model",
            "lanes": camera_lanes,
            "vehicle_counts": vehicle_counts,
            "priority_lane": priority["name"],
            "average_density": avg_density,
            "recommended_signal": recommended_signal,
            "green_seconds": green_seconds,
            "confidence": confidence,
            "emergency_priority": priority["density"] >= 75,
            "reason": f"AI model counted {vehicle_counts['total']} vehicles. {priority['name']} is highest at {priority['density']}% density.",
            "updated_at": time.strftime("%H:%M:%S", time.localtime(max(feed["updated_at"] for feed in live_lanes))),
        }

    minute_bucket = now // 60
    scan_count = db_rows("SELECT COUNT(*) AS count FROM qr_scans WHERE created_at > ?", (now - 900,))[0]["count"]
    report_count = db_rows("SELECT COUNT(*) AS count FROM accident_reports WHERE created_at > ?", (now - 1800,))[0]["count"]
    lanes = [
        {"name": "Northbound camera", "base": 42},
        {"name": "Southbound camera", "base": 36},
        {"name": "Eastbound camera", "base": 48},
        {"name": "Westbound camera", "base": 32},
    ]
    camera_lanes = []
    for index, lane in enumerate(lanes):
        wave = ((minute_bucket + index * 3) % 11) * 4
        density = min(96, lane["base"] + wave + scan_count * 3 + report_count * 5)
        vehicle_count = max(3, round(density * 0.7 + index * 2))
        camera_lanes.append({"name": lane["name"], "density": density, "vehicle_count": vehicle_count, "vehicle_counts": normalize_vehicle_counts({"cars": vehicle_count}, vehicle_count), "source": "simulation"})

    priority = max(camera_lanes, key=lambda lane: lane["density"])
    avg_density = round(sum(lane["density"] for lane in camera_lanes) / len(camera_lanes))
    emergency_priority = report_count > 0 or priority["density"] >= 85
    recommended_signal = "go" if priority["density"] >= 70 else "slow" if avg_density >= 45 else "ai"
    green_seconds = min(95, max(25, 20 + priority["density"]))
    confidence = min(98, 72 + abs(priority["density"] - avg_density) // 2 + scan_count + report_count * 3)
    reason = f"{priority['name']} has the highest density at {priority['density']}%."
    if emergency_priority:
        reason += " Emergency or high-density priority is active."
    return {
        "mode": "simulation",
        "lanes": camera_lanes,
        "vehicle_counts": merge_vehicle_counts(camera_lanes),
        "priority_lane": priority["name"],
        "average_density": avg_density,
        "recommended_signal": recommended_signal,
        "green_seconds": green_seconds,
        "confidence": confidence,
        "emergency_priority": emergency_priority,
        "reason": reason,
        "updated_at": time.strftime("%H:%M:%S", time.localtime(now)),
    }


def fetch_json(url: str) -> dict | list:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "TrafficControlOS/1.0 contact=mohnishraj187@gmail.com",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


LOCAL_DESTINATIONS = [
    ("vandalur zoo", "Arignar Anna Zoological Park, Vandalur", 12.8793, 80.0817),
    ("arignar anna zoological park", "Arignar Anna Zoological Park, Vandalur", 12.8793, 80.0817),
    ("mambakkam", "Mambakkam, Chennai", 12.8406, 80.1534),
    ("tambaram", "Tambaram, Chennai", 12.9249, 80.1000),
    ("kelambakkam", "Kelambakkam, Chennai", 12.7867, 80.2206),
    ("medavakkam", "Medavakkam, Chennai", 12.9171, 80.1923),
    ("sholinganallur", "Sholinganallur, Chennai", 12.9010, 80.2279),
    ("velachery", "Velachery, Chennai", 12.9756, 80.2207),
    ("t nagar", "T Nagar, Chennai", 13.0418, 80.2341),
    ("anna salai", "Anna Salai, Chennai", 13.0619, 80.2619),
    ("sowcarpet", "Sowcarpet, Chennai", 13.0940, 80.2791),
    ("mint street", "Mint Street, Sowcarpet, Chennai", 13.0969, 80.2795),
    ("parrys corner", "Parry's Corner, Chennai", 13.0878, 80.2893),
    ("george town", "George Town, Chennai", 13.0965, 80.2865),
    ("chennai central", "Chennai Central Railway Station", 13.0827, 80.2757),
    ("kathipara", "Kathipara Junction, Chennai", 13.0076, 80.2012),
    ("silk board", "Silk Board Junction, Bengaluru", 12.9177, 77.6238),
    ("marathahalli", "Marathahalli Bridge, Bengaluru", 12.9569, 77.7011),
    ("kr puram", "KR Puram Tin Factory, Bengaluru", 13.0005, 77.6757),
    ("worli sea link", "Worli Sea Link, Mumbai", 19.0270, 72.8150),
    ("bandra kurla", "Bandra Kurla Complex, Mumbai", 19.0697, 72.8697),
    ("mumbai", "Mumbai, Maharashtra", 19.0760, 72.8777),
    ("delhi", "Delhi", 28.6139, 77.2090),
    ("new delhi", "New Delhi", 28.6139, 77.2090),
    ("kolkata", "Kolkata, West Bengal", 22.5726, 88.3639),
    ("hyderabad", "Hyderabad, Telangana", 17.3850, 78.4867),
    ("pune", "Pune, Maharashtra", 18.5204, 73.8567),
    ("ahmedabad", "Ahmedabad, Gujarat", 23.0225, 72.5714),
    ("jaipur", "Jaipur, Rajasthan", 26.9124, 75.7873),
    ("lucknow", "Lucknow, Uttar Pradesh", 26.8467, 80.9462),
    ("kanpur", "Kanpur, Uttar Pradesh", 26.4499, 80.3319),
    ("nagpur", "Nagpur, Maharashtra", 21.1458, 79.0882),
    ("indore", "Indore, Madhya Pradesh", 22.7196, 75.8577),
    ("thane", "Thane, Maharashtra", 19.2183, 72.9781),
    ("bhopal", "Bhopal, Madhya Pradesh", 23.2599, 77.4126),
    ("visakhapatnam", "Visakhapatnam, Andhra Pradesh", 17.6868, 83.2185),
    ("patna", "Patna, Bihar", 25.5941, 85.1376),
    ("vadodara", "Vadodara, Gujarat", 22.3072, 73.1812),
    ("ghaziabad", "Ghaziabad, Uttar Pradesh", 28.6692, 77.4538),
    ("ludhiana", "Ludhiana, Punjab", 30.9010, 75.8573),
    ("agra", "Agra, Uttar Pradesh", 27.1767, 78.0081),
    ("nashik", "Nashik, Maharashtra", 19.9975, 73.7898),
    ("faridabad", "Faridabad, Haryana", 28.4089, 77.3178),
    ("meerut", "Meerut, Uttar Pradesh", 28.9845, 77.7064),
    ("rajkot", "Rajkot, Gujarat", 22.3039, 70.8022),
    ("varanasi", "Varanasi, Uttar Pradesh", 25.3176, 82.9739),
    ("srinagar", "Srinagar, Jammu and Kashmir", 34.0837, 74.7973),
    ("aurangabad", "Aurangabad, Maharashtra", 19.8762, 75.3433),
    ("dhanbad", "Dhanbad, Jharkhand", 23.7957, 86.4304),
    ("amritsar", "Amritsar, Punjab", 31.6340, 74.8723),
    ("prayagraj", "Prayagraj, Uttar Pradesh", 25.4358, 81.8463),
    ("allahabad", "Prayagraj, Uttar Pradesh", 25.4358, 81.8463),
    ("ranchi", "Ranchi, Jharkhand", 23.3441, 85.3096),
    ("howrah", "Howrah, West Bengal", 22.5958, 88.2636),
    ("coimbatore", "Coimbatore, Tamil Nadu", 11.0168, 76.9558),
    ("jabalpur", "Jabalpur, Madhya Pradesh", 23.1815, 79.9864),
    ("gwalior", "Gwalior, Madhya Pradesh", 26.2183, 78.1828),
    ("vijayawada", "Vijayawada, Andhra Pradesh", 16.5062, 80.6480),
    ("jodhpur", "Jodhpur, Rajasthan", 26.2389, 73.0243),
    ("madurai", "Madurai, Tamil Nadu", 9.9252, 78.1198),
    ("raipur", "Raipur, Chhattisgarh", 21.2514, 81.6296),
    ("kota", "Kota, Rajasthan", 25.2138, 75.8648),
    ("guwahati", "Guwahati, Assam", 26.1445, 91.7362),
    ("chandigarh", "Chandigarh", 30.7333, 76.7794),
    ("solapur", "Solapur, Maharashtra", 17.6599, 75.9064),
    ("hubli", "Hubballi, Karnataka", 15.3647, 75.1240),
    ("hubballi", "Hubballi, Karnataka", 15.3647, 75.1240),
    ("mysuru", "Mysuru, Karnataka", 12.2958, 76.6394),
    ("mysore", "Mysuru, Karnataka", 12.2958, 76.6394),
    ("tiruchirappalli", "Tiruchirappalli, Tamil Nadu", 10.7905, 78.7047),
    ("trichy", "Tiruchirappalli, Tamil Nadu", 10.7905, 78.7047),
    ("salem", "Salem, Tamil Nadu", 11.6643, 78.1460),
    ("tirunelveli", "Tirunelveli, Tamil Nadu", 8.7139, 77.7567),
    ("erode", "Erode, Tamil Nadu", 11.3410, 77.7172),
    ("vellore", "Vellore, Tamil Nadu", 12.9165, 79.1325),
    ("thoothukudi", "Thoothukudi, Tamil Nadu", 8.7642, 78.1348),
    ("tuticorin", "Thoothukudi, Tamil Nadu", 8.7642, 78.1348),
    ("dindigul", "Dindigul, Tamil Nadu", 10.3673, 77.9803),
    ("thanjavur", "Thanjavur, Tamil Nadu", 10.7870, 79.1378),
    ("pondicherry", "Puducherry", 11.9416, 79.8083),
    ("puducherry", "Puducherry", 11.9416, 79.8083),
]


def local_destination_match(destination: str) -> dict | None:
    cleaned = destination.strip().lower()
    for key, name, lat, lng in LOCAL_DESTINATIONS:
        if key in cleaned or cleaned in key:
            return {"lat": lat, "lng": lng, "name": name}
    return None


def cached_destination(query: str) -> dict | None:
    cache_key = query.strip().lower()
    rows = db_rows("SELECT name, lat, lng FROM geocode_cache WHERE query = ?", (cache_key,))
    if not rows:
        return None
    row = rows[0]
    return {"lat": float(row["lat"]), "lng": float(row["lng"]), "name": row["name"]}


def cache_destination(query: str, destination: dict) -> None:
    cache_key = query.strip().lower()
    db_execute(
        """
        INSERT OR REPLACE INTO geocode_cache (query, name, lat, lng, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (cache_key, destination["name"], destination["lat"], destination["lng"], int(time.time())),
    )


def geocode_query_variants(destination: str) -> list[str]:
    cleaned = " ".join(destination.strip().split())
    lowered = cleaned.lower()
    variants = [cleaned]
    has_city_or_country = any(part in lowered for part in ["india", "chennai", "bengaluru", "bangalore", "mumbai", "delhi"])
    if not has_city_or_country:
        variants.extend(
            [
                f"{cleaned}, Chennai, Tamil Nadu, India",
                f"{cleaned}, Tamil Nadu, India",
                f"{cleaned}, Bengaluru, Karnataka, India",
                f"{cleaned}, Mumbai, Maharashtra, India",
                f"{cleaned}, India",
            ]
        )
    elif "india" not in lowered:
        variants.append(f"{cleaned}, India")
    return list(dict.fromkeys(variants))


def nominatim_destination(destination: str) -> dict | None:
    for query in geocode_query_variants(destination):
        params = urllib.parse.urlencode(
            {
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "in",
                "q": query,
            }
        )
        try:
            results = fetch_json(f"https://nominatim.openstreetmap.org/search?{params}")
        except Exception:
            continue
        if isinstance(results, list) and results:
            match = results[0]
            found = {
                "lat": float(match["lat"]),
                "lng": float(match["lon"]),
                "name": match.get("display_name", query),
            }
            cache_destination(destination, found)
            return found
    return None


def photon_destination(destination: str) -> dict | None:
    for query in geocode_query_variants(destination):
        params = urllib.parse.urlencode(
            {
                "q": query,
                "limit": 1,
                "lang": "en",
                "bbox": "68.0,6.0,98.0,37.5",
            }
        )
        try:
            results = fetch_json(f"https://photon.komoot.io/api/?{params}")
        except Exception:
            continue
        features = results.get("features", []) if isinstance(results, dict) else []
        if not features:
            continue
        feature = features[0]
        props = feature.get("properties", {})
        country = str(props.get("country", "")).lower()
        if country and country != "india":
            continue
        coords = feature.get("geometry", {}).get("coordinates", [])
        if len(coords) < 2:
            continue
        name_parts = [props.get("name"), props.get("city"), props.get("state"), props.get("country")]
        found = {
            "lat": float(coords[1]),
            "lng": float(coords[0]),
            "name": ", ".join(str(part) for part in name_parts if part),
        }
        cache_destination(destination, found)
        return found
    return None


def parse_destination(destination: str) -> dict | None:
    cleaned = destination.strip()
    parts = [part.strip() for part in cleaned.split(",")]
    if len(parts) == 2:
        try:
            lat = float(parts[0])
            lng = float(parts[1])
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return {"lat": lat, "lng": lng, "name": cleaned}
        except ValueError:
            pass

    local = local_destination_match(cleaned)
    if local:
        return local

    cached = cached_destination(cleaned)
    if cached:
        return cached

    return nominatim_destination(cleaned) or photon_destination(cleaned)


def route_cache_key(origin_lat: float, origin_lng: float, destination: dict) -> str:
    return f"{origin_lat:.4f},{origin_lng:.4f}:{destination['lat']:.4f},{destination['lng']:.4f}"


def cached_route(route_key: str) -> dict | None:
    rows = db_rows("SELECT payload FROM route_cache WHERE route_key = ?", (route_key,))
    if not rows:
        return None
    payload = json.loads(rows[0]["payload"])
    payload["cached"] = True
    return payload


def cache_route(route_key: str, payload: dict) -> None:
    db_execute(
        "INSERT OR REPLACE INTO route_cache (route_key, payload, created_at) VALUES (?, ?, ?)",
        (route_key, json.dumps(payload), int(time.time())),
    )


def osrm_route_response(origin_lat: float, origin_lng: float, destination: dict) -> dict:
    route_key = route_cache_key(origin_lat, origin_lng, destination)
    cached = cached_route(route_key)
    if cached:
        return cached

    coords = f"{origin_lng},{origin_lat};{destination['lng']},{destination['lat']}"
    params = urllib.parse.urlencode({"overview": "full", "geometries": "geojson", "steps": "false"})
    route_data = fetch_json(f"https://router.project-osrm.org/route/v1/driving/{coords}?{params}")
    routes = route_data.get("routes", []) if isinstance(route_data, dict) else []
    if not routes:
        return {"ok": False, "error": "No road route was found for that destination."}

    route = routes[0]
    geometry = route.get("geometry", {"type": "LineString", "coordinates": []})
    if not geometry.get("coordinates"):
        return {"ok": False, "error": "Road route geometry was empty."}

    distance_km_value = route.get("distance", 0) / 1000
    duration_min_value = route.get("duration", 0) / 60
    duration_text = f"{round(duration_min_value)} mins" if duration_min_value < 90 else f"{round(duration_min_value / 60, 1)} hrs"
    payload = {
        "ok": True,
        "cached": False,
        "destination": destination,
        "distance_km": round(distance_km_value, 1),
        "distance_text": f"{round(distance_km_value, 1)} km",
        "duration_min": round(duration_min_value),
        "duration_text": duration_text,
        "geometry": geometry,
        "source": "osrm_road_route",
    }
    cache_route(route_key, payload)
    return payload


def route_summary(query: dict[str, list[str]]) -> dict:
    try:
        origin_lat = float(query.get("origin_lat", [""])[0])
        origin_lng = float(query.get("origin_lng", [""])[0])
    except ValueError:
        return {"ok": False, "error": "Current location is required."}

    destination_text = query.get("destination", [""])[0].strip()
    if not destination_text:
        return {"ok": False, "error": "Destination is required."}

    try:
        destination = parse_destination(destination_text)
        if not destination:
            return {"ok": False, "error": "Destination was not found."}

        route_key = route_cache_key(origin_lat, origin_lng, destination)
        cached = cached_route(route_key)
        if cached:
            return cached
        return osrm_route_response(origin_lat, origin_lng, destination)
    except Exception as exc:
        if "destination" in locals():
            cached = cached_route(route_cache_key(origin_lat, origin_lng, destination))
            if cached:
                return cached
            return {"ok": False, "destination": destination, "error": f"Destination found as {destination['name']}, but road routing is temporarily unavailable. Try again in a minute."}
        return {"ok": False, "error": "Destination search is temporarily unavailable. Try a more specific place name with city/state."}


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    radius = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return round(2 * radius * asin(sqrt(a)), 1)


def traffic_summary(query: dict[str, list[str]] | None = None) -> dict:
    query = query or {}
    lat = None
    lng = None
    try:
        lat = float(query.get("lat", [""])[0])
        lng = float(query.get("lng", [""])[0])
    except ValueError:
        pass

    hotspots = [
        {"name": "Worli Sea Link", "lat": 19.0270, "lng": 72.8150, "base_delay": 18},
        {"name": "Western Express Hwy", "lat": 19.1176, "lng": 72.8562, "base_delay": 11},
        {"name": "Bandra Kurla Complex", "lat": 19.0697, "lng": 72.8697, "base_delay": 13},
        {"name": "Sion Circle", "lat": 19.0423, "lng": 72.8611, "base_delay": 8},
        {"name": "Eastern Express Hwy", "lat": 19.0790, "lng": 72.9135, "base_delay": 9},
        {"name": "Dadar TT", "lat": 19.0188, "lng": 72.8478, "base_delay": 10},
        {"name": "Silk Board Junction", "lat": 12.9177, "lng": 77.6238, "base_delay": 16},
        {"name": "Marathahalli Bridge", "lat": 12.9569, "lng": 77.7011, "base_delay": 14},
        {"name": "KR Puram Tin Factory", "lat": 13.0005, "lng": 77.6757, "base_delay": 12},
        {"name": "T Nagar", "lat": 13.0418, "lng": 80.2341, "base_delay": 13},
        {"name": "Anna Salai", "lat": 13.0619, "lng": 80.2619, "base_delay": 11},
        {"name": "Kathipara Junction", "lat": 13.0076, "lng": 80.2012, "base_delay": 15},
        {"name": "Connaught Place", "lat": 28.6315, "lng": 77.2167, "base_delay": 12},
        {"name": "AIIMS Ring Road", "lat": 28.5672, "lng": 77.2100, "base_delay": 14},
        {"name": "ITO Junction", "lat": 28.6289, "lng": 77.2425, "base_delay": 17},
    ]

    minute_bucket = int(time.time() // 60)
    pulse = [0, 2, 4, 1, 3][minute_bucket % 5]

    if lat is not None and lng is not None:
        for spot in hotspots:
            spot["distance_km"] = distance_km(lat, lng, spot["lat"], spot["lng"])
        selected = sorted(hotspots, key=lambda spot: spot["distance_km"])[:3]
        area = f"near {selected[0]['name']}"
    else:
        selected = hotspots[:3]
        area = "default Mumbai view"

    formatted = []
    total_delay = 0
    for spot in selected:
        delay = spot["base_delay"] + pulse
        total_delay += delay
        formatted.append(
            {
                "name": spot["name"],
                "delay": f"+{delay} mins",
                "distance_km": spot.get("distance_km"),
            }
        )

    avg_speed = max(12, 42 - total_delay // max(1, len(formatted)))
    summary = {
        "avg_speed": f"{avg_speed}.0 km/h",
        "trend": "Estimated from nearby congestion hotspots",
        "hotspots": formatted,
        "area": area,
        "source": "location_model",
        "source_label": "Location-based local congestion model",
    }
    return summary


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), TrafficHandler)
    safe_print(f"TrafficControl OS running at http://{HOST}:{PORT}")
    safe_print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET for real Google sign-in.")
    server.serve_forever()


if __name__ == "__main__":
    main()
