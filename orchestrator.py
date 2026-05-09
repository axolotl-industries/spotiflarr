#!/usr/bin/env python3
"""
Spotiflarr — bridge SpotiFLAC into Lidarr.

Runs in a Docker container alongside Soularr. Each cycle:
  1. Pull Lidarr's wanted-missing list
  2. Filter out already-denied / no-spotify-url / in-flight entries
  3. For up to ALBUMS_PER_RUN albums:
     a. Resolve MB Release Group → Spotify album URL via MusicBrainz url-rels
     b. If no URL: mark no_spotify_url (Soularr can still try)
     c. Otherwise: shell out to `spotiflac -o /output/<album-folder>/ <url>`
     d. POST DownloadedAlbumsScan to Lidarr pointed at the album folder
     e. On success: clear retry counter
     f. On failure: increment, deny-list at MAX_RETRIES
  4. Sleep INTERVAL_SECONDS, repeat

Tiny Flask UI on :8181 for status + editable config. Config persisted to
/data/config.json (env vars are bootstrap defaults; UI saves override).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from flask import Flask, redirect, render_template, request, url_for

# ─── Paths ──────────────────────────────────────────────────────────────────

DATA_DIR = Path("/data")
OUTPUT_DIR = Path("/output")
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Config ─────────────────────────────────────────────────────────────────

# Defaults match the answers Geoff gave during planning.
DEFAULTS = {
    "lidarr_url": os.environ.get("LIDARR_URL", "http://192.168.1.34:8686"),
    "lidarr_api_key": os.environ.get("LIDARR_API_KEY", ""),
    "lidarr_output_path": os.environ.get(
        "LIDARR_OUTPUT_PATH", "/mnt/bigboi/media/downloads/music/spotiflac"
    ),
    "interval_seconds": 10800,   # 3 hours
    "albums_per_run": 5,
    "max_retries": 5,
    "spotiflac_concurrency": 3,
    # Source for SpotiFLAC's audio fetch. Upstream defaults to tidal but
    # the Tidal APIs are routinely 403/timing out — qobuz is the most
    # reliable mirror at the moment. Valid: "qobuz", "amazon", "tidal".
    "spotiflac_service": "qobuz",
    "ntfy_url": os.environ.get("NTFY_URL", ""),
    "ntfy_token": os.environ.get("NTFY_TOKEN", ""),
    "ntfy_topic": os.environ.get("NTFY_TOPIC", "docker-alerts"),
}

config_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception as e:
            log.warning(f"couldn't read config.json, using defaults: {e}")
    return cfg


def save_config(cfg: dict) -> None:
    with config_lock:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# ─── State ──────────────────────────────────────────────────────────────────

state_lock = threading.Lock()


@dataclass
class State:
    # Per-album-MBID retry counts and verdicts.
    attempts: dict[str, int] = field(default_factory=dict)
    denied: list[str] = field(default_factory=list)
    no_spotify_url: list[str] = field(default_factory=list)
    # Recent activity, capped to 100 entries.
    history: list[dict] = field(default_factory=list)
    # Stats
    total_attempts: int = 0
    total_successes: int = 0
    last_cycle_at: Optional[str] = None
    last_cycle_result: Optional[str] = None

    def trim_history(self, n: int = 100) -> None:
        if len(self.history) > n:
            self.history = self.history[-n:]


def load_state() -> State:
    if not STATE_PATH.exists():
        return State()
    try:
        raw = json.loads(STATE_PATH.read_text())
        return State(**raw)
    except Exception as e:
        log.warning(f"couldn't read state.json, starting fresh: {e}")
        return State()


def save_state(s: State) -> None:
    with state_lock:
        s.trim_history()
        STATE_PATH.write_text(json.dumps(asdict(s), indent=2))


# ─── Logging ────────────────────────────────────────────────────────────────

log_buffer: deque[str] = deque(maxlen=500)


class BufferHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append(self.format(record))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("spotiflarr")
buf_handler = BufferHandler()
buf_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                           "%Y-%m-%d %H:%M:%S"))
log.addHandler(buf_handler)


# ─── Lidarr ─────────────────────────────────────────────────────────────────

def lidarr(method: str, path: str, cfg: dict, **kwargs) -> requests.Response:
    headers = kwargs.pop("headers", {})
    headers["X-Api-Key"] = cfg["lidarr_api_key"]
    url = f"{cfg['lidarr_url'].rstrip('/')}/api/v1{path}"
    r = requests.request(method, url, headers=headers, timeout=60, **kwargs)
    r.raise_for_status()
    return r


def lidarr_wanted_missing(cfg: dict) -> list[dict]:
    """Returns the full wanted/missing list, paginated until exhausted."""
    out: list[dict] = []
    page = 1
    while True:
        data = lidarr("GET", "/wanted/missing", cfg, params={
            "page": page, "pageSize": 100,
            "includeArtist": "true",
            "sortKey": "albums.title", "sortDirection": "ascending",
        }).json()
        out.extend(data.get("records", []))
        if len(out) >= data.get("totalRecords", 0):
            break
        page += 1
        if page > 50:   # safety cap
            break
    return out


def lidarr_trigger_scan(cfg: dict, folder: str) -> int:
    """Fire DownloadedAlbumsScan for the given absolute path (Lidarr's view).
    Returns the command id."""
    r = lidarr("POST", "/command", cfg, json={
        "name": "DownloadedAlbumsScan",
        "path": folder,
    })
    return r.json()["id"]


# ─── MusicBrainz resolution ─────────────────────────────────────────────────

MB_BASE = "https://musicbrainz.org/ws/2"
MB_UA = "Spotiflarr/0.1 (homelab; +https://github.com/)"
mb_lock = threading.Lock()  # MB asks for ≤1 req/sec
_mb_last_call = 0.0


def mb_get(path: str, **params) -> dict:
    global _mb_last_call
    params["fmt"] = "json"
    with mb_lock:
        wait = 1.05 - (time.time() - _mb_last_call)
        if wait > 0:
            time.sleep(wait)
        r = requests.get(f"{MB_BASE}{path}", params=params,
                         headers={"User-Agent": MB_UA}, timeout=30)
        _mb_last_call = time.time()
    r.raise_for_status()
    return r.json()


SPOTIFY_ALBUM_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?album/([A-Za-z0-9]+)")


def find_spotify_album_url(relations: list[dict]) -> Optional[str]:
    """MB url-rels of type 'free streaming' point at Spotify, Tidal, etc.
    We only want Spotify ALBUM URLs."""
    for rel in relations:
        url = (rel.get("url") or {}).get("resource", "")
        if SPOTIFY_ALBUM_RE.search(url):
            return url
    return None


def mb_resolve_album_to_spotify(rg_mbid: str) -> Optional[str]:
    """Lidarr's foreignAlbumId is the MB Release Group ID. Look first at
    the release group's url-rels, then walk releases until we find a
    Spotify album URL. Returns None if nothing usable."""
    try:
        rg = mb_get(f"/release-group/{rg_mbid}", inc="url-rels")
    except Exception as e:
        log.warning(f"  MB release-group lookup failed for {rg_mbid}: {e}")
        return None
    url = find_spotify_album_url(rg.get("relations", []) or [])
    if url:
        return url

    # Walk releases. MB returns release stubs; need a follow-up call per
    # release for url-rels. Cap at 5 releases to avoid pathological cases.
    try:
        rg2 = mb_get(f"/release-group/{rg_mbid}", inc="releases")
    except Exception:
        return None
    for rel in (rg2.get("releases") or [])[:5]:
        try:
            full = mb_get(f"/release/{rel['id']}", inc="url-rels")
        except Exception:
            continue
        url = find_spotify_album_url(full.get("relations", []) or [])
        if url:
            return url
    return None


# ─── SpotiFLAC ──────────────────────────────────────────────────────────────

def safe_dirname(s: str) -> str:
    bad = '/\\:*?"<>|'
    return ("".join("_" if c in bad else c for c in s)).strip() or "Unknown"


def run_spotiflac(spotify_url: str, output_dir: Path,
                  concurrency: int, service: str) -> tuple[bool, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "spotiflac",
        "-o", str(output_dir),
        "-c", str(concurrency),
        spotify_url,
    ]
    # SPOTIFLAC_SERVICE is read by our patched main.go; valid values are
    # tidal / qobuz / amazon. Default in DEFAULTS is qobuz.
    env = {**os.environ, "SPOTIFLAC_SERVICE": service}
    log.info(f"  spotiflac → {output_dir.name} (service={service})")

    # Stream stdout/stderr line-by-line so the orchestrator log shows real
    # progress instead of going silent for 30s–several minutes per album.
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge so order is preserved
            text=True,
            bufsize=1,                  # line-buffered
            env=env,
        )
    except FileNotFoundError:
        return False, "spotiflac binary not found in container"

    last_lines: deque[str] = deque(maxlen=10)
    deadline = time.time() + 1800   # 30 min cap on a single album
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                log.info(f"  [spotiflac] {line}")
                last_lines.append(line)
            if time.time() > deadline:
                proc.kill()
                return False, "spotiflac timed out (>30 min)"
        proc.wait(timeout=10)
    except Exception as e:
        proc.kill()
        return False, f"spotiflac stream error: {e}"

    if proc.returncode != 0:
        tail = " | ".join(list(last_lines)[-3:])
        return False, f"spotiflac rc={proc.returncode}: {tail[:300]}"

    flacs = list(output_dir.rglob("*.flac"))
    if not flacs:
        return False, "spotiflac succeeded but no .flac landed"
    return True, f"got {len(flacs)} track(s)"


# ─── ntfy ───────────────────────────────────────────────────────────────────

def ntfy(cfg: dict, title: str, body: str, priority: str = "default") -> None:
    if not cfg.get("ntfy_url") or not cfg.get("ntfy_token"):
        return
    try:
        requests.post(
            f"{cfg['ntfy_url'].rstrip('/')}/{cfg['ntfy_topic']}",
            data=body.encode("utf-8"),
            headers={
                "Authorization": f"Bearer {cfg['ntfy_token']}",
                "X-Title": title,
                "X-Tags": "spotiflarr",
                "X-Priority": priority,
            },
            timeout=10,
        )
    except Exception as e:
        log.warning(f"ntfy failed: {e}")


# ─── Main loop ──────────────────────────────────────────────────────────────

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cycle(cfg: dict, state: State) -> None:
    """One pass: fetch wanted, pick up to N candidates, process each."""
    if not cfg.get("lidarr_api_key"):
        log.warning("LIDARR_API_KEY not set — set it via /settings or env, skipping cycle")
        state.last_cycle_at = now_utc()
        state.last_cycle_result = "no api key"
        return

    try:
        wanted = lidarr_wanted_missing(cfg)
    except Exception as e:
        log.error(f"couldn't pull wanted list: {e}")
        state.last_cycle_at = now_utc()
        state.last_cycle_result = f"lidarr fetch failed: {e}"
        return

    skip = set(state.denied) | set(state.no_spotify_url)
    candidates: list[dict] = []
    for album in wanted:
        mbid = album.get("foreignAlbumId")
        if not mbid or mbid in skip:
            continue
        candidates.append(album)
        if len(candidates) >= cfg["albums_per_run"]:
            break

    log.info(f"cycle: {len(wanted)} wanted, {len(candidates)} this run")

    successes = 0
    for album in candidates:
        mbid = album["foreignAlbumId"]
        title = album.get("title", "?")
        artist = (album.get("artist") or {}).get("artistName", "?")
        log.info(f"  [{artist} — {title}]  rg={mbid}")

        spotify_url = mb_resolve_album_to_spotify(mbid)
        if not spotify_url:
            log.info(f"  no Spotify URL in MB; skipping (Soularr can still try)")
            state.no_spotify_url.append(mbid)
            state.history.append({
                "time": now_utc(), "artist": artist, "album": title,
                "result": "no_spotify_url", "msg": "MB has no Spotify album rel",
            })
            continue

        log.info(f"  Spotify: {spotify_url}")
        folder_name = safe_dirname(f"{artist} - {title}")
        output_dir = OUTPUT_DIR / folder_name

        state.total_attempts += 1
        ok, msg = run_spotiflac(
            spotify_url, output_dir,
            cfg["spotiflac_concurrency"],
            cfg.get("spotiflac_service", "qobuz"),
        )
        if not ok:
            log.warning(f"  fail: {msg}")
            state.attempts[mbid] = state.attempts.get(mbid, 0) + 1
            if state.attempts[mbid] >= cfg["max_retries"]:
                log.warning(f"  deny-listing after {state.attempts[mbid]} attempts")
                state.denied.append(mbid)
                ntfy(cfg, "Spotiflarr: deny-listed",
                     f"{artist} — {title}: {msg}", "high")
            # Clean up partial download if any
            if output_dir.exists() and not any(output_dir.iterdir()):
                output_dir.rmdir()
            state.history.append({
                "time": now_utc(), "artist": artist, "album": title,
                "result": "fail", "msg": msg,
            })
            continue

        # Success at SpotiFLAC level — hand off to Lidarr
        lidarr_path = f"{cfg['lidarr_output_path'].rstrip('/')}/{folder_name}"
        try:
            cmd_id = lidarr_trigger_scan(cfg, lidarr_path)
            log.info(f"  Lidarr scan queued (cmd {cmd_id})")
        except Exception as e:
            log.error(f"  scan trigger failed: {e}")
            state.history.append({
                "time": now_utc(), "artist": artist, "album": title,
                "result": "scan_failed", "msg": str(e),
            })
            continue

        state.attempts.pop(mbid, None)
        state.total_successes += 1
        successes += 1
        state.history.append({
            "time": now_utc(), "artist": artist, "album": title,
            "result": "success", "msg": msg,
        })

    state.last_cycle_at = now_utc()
    state.last_cycle_result = f"{successes}/{len(candidates)} succeeded"

    if successes:
        ntfy(cfg, f"Spotiflarr: +{successes} albums",
             "\n".join(f"{h['artist']} — {h['album']}"
                       for h in state.history[-successes:]
                       if h.get("result") == "success"))


def loop_forever() -> None:
    while True:
        cfg = load_config()
        state = load_state()
        try:
            cycle(cfg, state)
        except Exception as e:
            log.exception(f"cycle blew up: {e}")
            state.last_cycle_at = now_utc()
            state.last_cycle_result = f"exception: {e}"
        save_state(state)

        # Re-read config in case it was edited via UI; sleep accordingly
        cfg = load_config()
        log.info(f"sleeping {cfg['interval_seconds']}s until next cycle")
        # Sleep in 30s chunks so a config change can shorten the wait if
        # someone presses "Run now" from the UI.
        slept = 0
        while slept < cfg["interval_seconds"]:
            if RUN_NOW.is_set():
                RUN_NOW.clear()
                log.info("manual trigger from UI")
                break
            time.sleep(30)
            slept += 30


# ─── Flask UI ───────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="templates")
RUN_NOW = threading.Event()


@app.route("/")
def index():
    cfg = load_config()
    state = load_state()
    spotiflac_ok = bool(shutil.which("spotiflac"))
    return render_template(
        "index.html",
        cfg=cfg, state=state, spotiflac_ok=spotiflac_ok,
        log_lines=list(log_buffer)[-50:],
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    cfg = load_config()
    if request.method == "POST":
        for key in ("lidarr_url", "lidarr_api_key", "lidarr_output_path",
                    "ntfy_url", "ntfy_token", "ntfy_topic"):
            cfg[key] = request.form.get(key, cfg[key]).strip()
        for key in ("interval_seconds", "albums_per_run",
                    "max_retries", "spotiflac_concurrency"):
            try:
                cfg[key] = int(request.form.get(key, cfg[key]))
            except ValueError:
                pass
        svc = request.form.get("spotiflac_service", "").strip().lower()
        if svc in {"qobuz", "amazon", "tidal"}:
            cfg["spotiflac_service"] = svc
        save_config(cfg)
        log.info("config updated via UI")
        return redirect(url_for("settings"))
    return render_template("settings.html", cfg=cfg)


@app.route("/run-now", methods=["POST"])
def run_now():
    RUN_NOW.set()
    return redirect(url_for("index"))


@app.route("/state/clear-denied", methods=["POST"])
def clear_denied():
    state = load_state()
    state.denied = []
    state.no_spotify_url = []
    state.attempts = {}
    save_state(state)
    log.info("deny-list and no-spotify-url cleared via UI")
    return redirect(url_for("index"))


@app.route("/healthz")
def healthz():
    return {"ok": True, "spotiflac": bool(shutil.which("spotiflac"))}


# ─── Entrypoint ─────────────────────────────────────────────────────────────

def main() -> int:
    log.info("starting spotiflarr")
    if not shutil.which("spotiflac"):
        log.error("spotiflac binary not on PATH — image broken? rebuild")
    threading.Thread(target=loop_forever, daemon=True).start()
    # Flask production-ish server. Single worker, threading enabled for the UI.
    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", 8181, app, threaded=True)
    log.info("UI on :8181")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
