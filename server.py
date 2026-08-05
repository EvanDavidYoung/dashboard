"""
Anki personal dashboard server.

Usage:
    REFRESH_API_KEY=secret uv run python server.py

Endpoints:
    GET  /              — dashboard UI
    GET  /health        — unauthenticated; checks AnkiConnect reachability
    POST /sync          — triggers AnkiWeb sync (requires API key if set)
    GET  /sync?api_key= — same, for clients that can't set headers
    GET  /api/decks     — list of all Anki deck names
    GET  /api/stats     — review heatmap + today's stats for a deck/year
    GET  /api/weather   — current weather via wttr.in
"""

import functools
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from dotenv import dotenv_values, load_dotenv
from flask import Flask, jsonify, render_template, request, send_from_directory

load_dotenv()  # read key=value pairs from a local .env into os.environ
_ENV_FILE = dotenv_values()  # same pairs, but unshadowed by the ambient shell


def env_file_first(name, default=""):
    """Config where the project .env outranks an inherited shell variable.

    load_dotenv() never overwrites a variable that is already exported, so a
    stale global wins silently. ~/.zshenv exports VLLM_API_KEY for a *different*
    (opencode / vllm-secondbrain) endpoint, which is not the credential this app
    wants. Use this for the VLLM_* group so .env is the source of truth; plain
    os.environ.get elsewhere keeps `FOO=bar uv run python server.py` working.
    """
    val = _ENV_FILE.get(name)
    return val if val not in (None, "") else os.environ.get(name, default)

app = Flask(__name__)

ANKI_CONNECT_URL = "http://localhost:8765"
API_KEY = os.environ.get("REFRESH_API_KEY", "")
WEATHER_LOCATIONS = {"ny": "New York", "sf": "San Francisco", "taipei": "Taipei"}
WEATHER_DEFAULT   = os.environ.get("WEATHER_LOCATION", "New York")
OVERCAST_DB   = os.environ.get("OVERCAST_DB",   "overcast.db")
OVERCAST_AUTH = os.environ.get("OVERCAST_AUTH", "auth.json")
OVERCAST_CLI  = os.environ.get("OVERCAST_CLI",  "overcast-to-sqlite")

CHINESE_PODCASTS = [
    "Dashu Mandarin Podcast",
    "Howto.Zhongwen好土中文",
    "Lazy Chinese（Comprehensible Input + TPRS）| Slow Easy Chinese Stories | Simple Chinese",
    "Learn Mandarin in Mandarin with Huimin",
    "Learning Chinese through Stories",
    "MaoMi Chinese",
    "中級中文 Chinese Level Up",
    "大鹏说中文 - Speak Chinese with Da Peng",
    "瞎扯学中文 Convo Chinese",
    "迷誠品",
    "思文，败类",
]


def anki_request(action, **params):
    payload = {"action": action, "version": 6, "params": params}
    resp = requests.post(ANKI_CONNECT_URL, json=payload, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(body["error"])
    return body["result"]


def require_api_key(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if API_KEY:
            key = request.headers.get("X-API-Key") or request.args.get("api_key")
            if key != API_KEY:
                return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


def _anki_error_response(e):
    if isinstance(e, requests.exceptions.ConnectionError):
        return jsonify({"error": "AnkiConnect unreachable — is Anki open?"}), 502
    return jsonify({"error": str(e)}), 502


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/test")
def test_page():
    return send_from_directory("test", "clock-glyphs.html")


@app.route("/sync", methods=["GET", "POST"])
@require_api_key
def sync():
    try:
        anki_request("sync")
    except (requests.exceptions.ConnectionError, RuntimeError) as e:
        return _anki_error_response(e)
    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    try:
        version = anki_request("version")
        anki_status = f"connected (version {version})"
    except Exception as e:
        anki_status = f"unreachable: {e}"
    return jsonify({"status": "ok", "anki_connect": anki_status})


@app.route("/api/decks")
def api_decks():
    try:
        decks = anki_request("deckNames")
    except (requests.exceptions.ConnectionError, RuntimeError) as e:
        return _anki_error_response(e)
    return jsonify({"decks": sorted(decks)})


@app.route("/api/stats")
def api_stats():
    """Returns heatmap data only for the requested year. Today's stats are in /api/today."""
    deck = request.args.get("deck", "")
    if not deck:
        return jsonify({"error": "deck parameter required"}), 400

    try:
        year = int(request.args.get("year", datetime.now(timezone.utc).year))
    except ValueError:
        return jsonify({"error": "year must be an integer"}), 400

    start_id = int(datetime(year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    end_id   = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

    try:
        raw = anki_request("cardReviews", deck=deck, startID=start_id)
    except (requests.exceptions.ConnectionError, RuntimeError) as e:
        return _anki_error_response(e)

    heatmap = defaultdict(int)
    for r in raw:
        ts_ms = r[0]
        if ts_ms < end_id:
            day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
            heatmap[day] += 1

    return jsonify({"heatmap": dict(heatmap)})


def _compute_today_stats(reviews):
    total_cards   = len(reviews)
    total_time_ms = sum(r["time_ms"] for r in reviews)
    s_per_card    = (total_time_ms / 1000 / total_cards) if total_cards else 0
    again_count   = sum(1 for r in reviews if r["ease"] == 1)
    mature        = [r for r in reviews if r["type"] == 1 and r["last_ivl"] >= 21]
    mature_total  = len(mature)
    return {
        "total_cards":    total_cards,
        "total_time_min": round(total_time_ms / 60_000, 2),
        "s_per_card":     round(s_per_card, 2),
        "again_count":    again_count,
        "again_pct":      round((again_count / total_cards * 100) if total_cards else 0, 2),
        "learn":          sum(1 for r in reviews if r["type"] == 0),
        "review":         sum(1 for r in reviews if r["type"] == 1),
        "relearn":        sum(1 for r in reviews if r["type"] == 2),
        "filtered":       sum(1 for r in reviews if r["type"] == 3),
        "mature_correct": sum(1 for r in mature if r["ease"] != 1),
        "mature_total":   mature_total,
        "mature_pct":     round((sum(1 for r in mature if r["ease"] != 1) / mature_total * 100) if mature_total else 0, 2),
    }


@app.route("/api/today")
def api_today():
    """Returns today's session stats, always anchored to the current calendar day."""
    deck = request.args.get("deck", "")
    if not deck:
        return jsonify({"error": "deck parameter required"}), 400

    now           = datetime.now(timezone.utc)
    today_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start_id      = int(today_midnight.timestamp() * 1000)

    try:
        raw = anki_request("cardReviews", deck=deck, startID=start_id)
    except (requests.exceptions.ConnectionError, RuntimeError) as e:
        return _anki_error_response(e)

    reviews = [
        {"ease": r[3], "last_ivl": r[5], "time_ms": r[7], "type": r[8]}
        for r in raw
    ]
    return jsonify(_compute_today_stats(reviews))


@app.route("/api/total-time")
def api_total_time():
    deck = request.args.get("deck", "")
    if not deck:
        return jsonify({"error": "deck parameter required"}), 400
    try:
        raw = anki_request("cardReviews", deck=deck, startID=0)
    except (requests.exceptions.ConnectionError, RuntimeError) as e:
        return _anki_error_response(e)
    total_hours = sum(r[7] for r in raw) / 3_600_000
    return jsonify({"total_hours": round(total_hours, 1), "deck": deck})


@app.route("/api/weather")
def api_weather():
    try:
        city = WEATHER_LOCATIONS.get(request.args.get("loc", ""), WEATHER_DEFAULT)
        loc = requests.utils.quote(city, safe="")
        resp = requests.get(
            f"https://wttr.in/{loc}?format=j1",
            timeout=5,
            headers={"Accept": "application/json", "User-Agent": "anki-dashboard/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        current = data["current_condition"][0]
        # Check today's hourly forecast for rain later in the day
        today_hourly = data.get("weather", [{}])[0].get("hourly", [])
        hourly_max_precip = max(
            (float(h.get("precipMM", 0)) for h in today_hourly), default=0
        )
        return jsonify({
            "temp_f":             current["temp_F"],
            "temp_c":             current["temp_C"],
            "desc":               current["weatherDesc"][0]["value"],
            "humidity":           current["humidity"],
            "feels_like_f":       current["FeelsLikeF"],
            "precip_mm":          current.get("precipMM", "0"),
            "windspeed_kmph":     current.get("windspeedKmph", "0"),
            "uv_index":           current.get("uvIndex", "0"),
            "hourly_max_precip":  round(hourly_max_precip, 1),
            "location":           city,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── Podcast duration cache ────────────────────────────────────────────

def _overcast_con():
    if not os.path.exists(OVERCAST_DB):
        return None
    con = sqlite3.connect(OVERCAST_DB)
    con.row_factory = sqlite3.Row
    return con


def _ensure_duration_cache(con):
    """Create the duration cache table if it doesn't exist."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS episode_durations (
            enclosureUrl TEXT PRIMARY KEY,
            duration_seconds INTEGER NOT NULL
        )
    """)
    con.commit()


def _parse_duration(s):
    """Parse itunes:duration: HH:MM:SS, MM:SS, or raw integer seconds."""
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    parts = s.split(":")
    try:
        parts = [int(p) for p in parts]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
    except ValueError:
        pass
    return None


def _backfill_durations(con, feed_titles):
    """
    Fetch RSS for any feed in feed_titles that has played/in-progress
    episodes missing from the duration cache. Each feed is only fetched
    when it actually has a gap, and never fetched again once all its
    episodes are cached.
    """
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}
    placeholders = ",".join("?" * len(feed_titles))

    # Find feeds that have at least one uncached played/in-progress episode
    gaps = con.execute(f"""
        SELECT DISTINCT f.title, f.xmlUrl
        FROM feeds f
        JOIN episodes e ON e.feedId = f.overcastId
        WHERE f.title IN ({placeholders})
          AND (e.played = 1 OR e.progress > 0)
          AND e.enclosureUrl IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM episode_durations d
              WHERE d.enclosureUrl = e.enclosureUrl
          )
    """, feed_titles).fetchall()

    if not gaps:
        return  # cache is complete — no network calls needed

    headers = {"User-Agent": "anki-dashboard/1.0"}
    for row in gaps:
        try:
            resp = requests.get(row["xmlUrl"], headers=headers, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            rows = []
            for item in root.iter("item"):
                enc = item.find("enclosure")
                dur_el = item.find("itunes:duration", ns)
                if enc is None or dur_el is None:
                    continue
                url = enc.get("url", "").split("?")[0]
                secs = _parse_duration(dur_el.text)
                if url and secs:
                    rows.append((url, secs))
            con.executemany(
                "INSERT OR IGNORE INTO episode_durations (enclosureUrl, duration_seconds) VALUES (?, ?)",
                rows,
            )
            con.commit()
        except Exception:
            pass  # leave gaps; will retry next request


@app.route("/api/podcasts")
def api_podcasts():
    con = _overcast_con()
    if con is None:
        return jsonify({"error": f"overcast.db not found at {OVERCAST_DB}"}), 404

    try:
        year = int(request.args.get("year", datetime.now(timezone.utc).year))
    except ValueError:
        return jsonify({"error": "year must be an integer"}), 400

    _ensure_duration_cache(con)
    _backfill_durations(con, CHINESE_PODCASTS)

    placeholders = ",".join("?" * len(CHINESE_PODCASTS))
    rows = con.execute(f"""
        SELECT
            f.title,
            COUNT(CASE WHEN e.played = 1 THEN 1 END)                    AS episodes_played,
            COALESCE(SUM(CASE WHEN e.played = 1
                             THEN d.duration_seconds END), 0)            AS played_seconds,
            COALESCE(SUM(CASE WHEN e.played = 0 AND e.progress > 0
                             THEN e.progress END), 0)                    AS partial_seconds
        FROM feeds f
        JOIN episodes e ON e.feedId = f.overcastId
        LEFT JOIN episode_durations d ON d.enclosureUrl = e.enclosureUrl
        WHERE f.title IN ({placeholders})
          AND (e.played = 1 OR e.progress > 0)
        GROUP BY f.title
        ORDER BY (played_seconds + partial_seconds) DESC
    """, CHINESE_PODCASTS).fetchall()

    heatmap_rows = con.execute(f"""
        SELECT
            DATE(e.userUpdatedDate) AS day,
            COALESCE(SUM(CASE WHEN e.played = 1 THEN d.duration_seconds ELSE 0 END), 0) +
            COALESCE(SUM(CASE WHEN e.played = 0 AND e.progress > 0 THEN e.progress ELSE 0 END), 0) AS seconds
        FROM feeds f
        JOIN episodes e ON e.feedId = f.overcastId
        LEFT JOIN episode_durations d ON d.enclosureUrl = e.enclosureUrl
        WHERE f.title IN ({placeholders})
          AND (e.played = 1 OR e.progress > 0)
          AND DATE(e.userUpdatedDate) >= ? AND DATE(e.userUpdatedDate) < ?
        GROUP BY DATE(e.userUpdatedDate)
    """, CHINESE_PODCASTS + [f"{year}-01-01", f"{year + 1}-01-01"]).fetchall()

    con.close()

    feeds = []
    total_seconds = 0
    for row in rows:
        secs = row["played_seconds"] + row["partial_seconds"]
        total_seconds += secs
        feeds.append({
            "title":           row["title"],
            "episodes_played": row["episodes_played"],
            "hours":           round(secs / 3600, 1),
        })

    # Cap at 86400s (24h) — excess comes from bulk "mark as played" giving many episodes the same userUpdatedDate
    heatmap = {
        row["day"]: round(min(row["seconds"], 86400) / 3600, 2)
        for row in heatmap_rows
        if row["day"]
    }

    return jsonify({
        "feeds":       feeds,
        "total_hours": round(total_seconds / 3600, 1),
        "heatmap":     heatmap,
    })


@app.route("/api/podcasts/sync", methods=["POST"])
@require_api_key
def podcast_sync():
    binary = shutil.which(OVERCAST_CLI) or OVERCAST_CLI
    result = subprocess.run(
        [binary, "save", OVERCAST_DB, "-a", OVERCAST_AUTH],
        capture_output=True, text=True, timeout=120,
    )
    return jsonify({
        "ok":     result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }), (200 if result.returncode == 0 else 500)


SLACKDUMP_GH_REPO = os.environ.get("SLACKDUMP_GH_REPO", "EvanDavidYoung/slackdump-pipeline")

@app.route("/api/slackdump/backup", methods=["POST"])
@require_api_key
def slackdump_backup():
    gh = shutil.which("gh")
    if not gh:
        return jsonify({"ok": False, "error": "gh CLI not found in PATH"}), 500
    result = subprocess.run(
        [gh, "workflow", "run", "daily-backup.yml", "--repo", SLACKDUMP_GH_REPO],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return jsonify({
            "ok": False,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }), 500
    runs_url = f"https://github.com/{SLACKDUMP_GH_REPO}/actions"
    return jsonify({"ok": True, "message": f"Workflow dispatched. View run at {runs_url}"})


OBSIDIAN_PUBLISH_DIR = os.environ.get(
    "OBSIDIAN_PUBLISH_DIR", "/Users/evanyoung/Desktop/projects/ObsidianPublishScript"
)

@app.route("/api/obsidian/publish", methods=["POST"])
@require_api_key
def obsidian_publish():
    uv = shutil.which("uv")
    if not uv:
        return jsonify({"ok": False, "error": "uv not found in PATH"}), 500
    if not os.path.isdir(OBSIDIAN_PUBLISH_DIR):
        return jsonify({"ok": False, "error": f"Script dir not found: {OBSIDIAN_PUBLISH_DIR}"}), 500
    try:
        result = subprocess.run(
            [uv, "run", "obsidian_publish_script.py"],
            cwd=OBSIDIAN_PUBLISH_DIR,
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Publish script timed out after 600s"}), 500

    # The script logs to stderr; surface the PR URL (new, existing, or commented-on).
    output = "\n".join(filter(None, [result.stdout, result.stderr])).strip()
    ok = result.returncode == 0
    pr_match = re.search(r"https://github\.com/\S+/pull/\d+", output)
    pr_url = pr_match.group(0) if pr_match else None
    if not ok:
        message = "Publish failed."
    elif "Created new pull request" in output:
        message = f"Pull request created: {pr_url}"
    elif pr_url:
        message = f"Publish completed — existing PR: {pr_url}"
    else:
        message = "Publish completed (no open PR)."
    return jsonify({
        "ok": ok,
        "message": message,
        "pr_url": pr_url,
        "output": output,
    }), (200 if ok else 500)


# ── vLLM warm-up ─────────────────────────────────────────────────────────────
# The inference server (Modal) goes cold when idle. A chat completion is the
# truest "is it serving?" probe: it requires the engine to be awake.
#
# Modal Endpoints do NOT hold the connection open during a cold start — their
# edge proxy returns an immediate 503 with an empty body until the container's
# HTTP server binds, which for a big MoE checkpoint is minutes (weight load +
# KV alloc + FlashInfer autotune). So we cannot just set a long socket timeout
# and wait; we have to poll until the upstream comes up.
VLLM_BASE_URL    = env_file_first("VLLM_BASE_URL")       # host root or an OpenAI-style base ending in /v1 — both accepted
VLLM_API_KEY     = env_file_first("VLLM_API_KEY")
VLLM_MODEL       = env_file_first("VLLM_MODEL", "Qwen/Qwen3.6-35B-A3B")  # served id; skips /v1/models discovery
VLLM_WARM_TIMEOUT = int(env_file_first("VLLM_WARM_TIMEOUT", "720"))
VLLM_POLL_INTERVAL = float(env_file_first("VLLM_POLL_INTERVAL", "5"))

# Booting-upstream signals: Modal's proxy 503s, and some fronts use 502/504.
_VLLM_BOOTING_STATUS = {502, 503, 504}


def _vllm_api_root(url):
    """Return the OpenAI API root (…/v1) whether or not the configured URL has it."""
    base = url.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


@app.route("/api/vllm/warm", methods=["POST"])
@require_api_key
def vllm_warm():
    if not VLLM_BASE_URL:
        return jsonify({"ok": False, "error": "VLLM_BASE_URL not set"}), 500
    base = _vllm_api_root(VLLM_BASE_URL)
    # Modal accepts the combined proxy token as a bearer credential.
    headers = {"Authorization": f"Bearer {VLLM_API_KEY.strip()}"} if VLLM_API_KEY else {}

    model = VLLM_MODEL
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    start = time.monotonic()
    deadline = start + VLLM_WARM_TIMEOUT
    attempts = 0
    last = ""

    # Poll past "still booting" responses until the engine answers or we run out
    # of time. Each individual request still gets a generous socket timeout, so
    # this also handles fronts that *do* hold the connection open.
    while True:
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return jsonify({
                "ok": False, "model": model, "attempts": attempts - 1,
                "elapsed": round(time.monotonic() - start, 1),
                "error": f"Still warming after {VLLM_WARM_TIMEOUT}s ({last}) — hit it again to keep waiting",
            }), 504
        try:
            r = requests.post(
                f"{base}/chat/completions",
                json=payload, headers=headers, timeout=min(remaining, 120),
            )
        except requests.Timeout:
            last = "request timed out"
        except Exception as e:
            return jsonify({"ok": False, "model": model, "error": str(e)}), 502
        else:
            if r.status_code == 200:
                break
            if r.status_code not in _VLLM_BOOTING_STATUS:
                # A real error (401 bad key, 404 wrong path, 400 wrong model id) —
                # polling will never fix it, so surface it immediately.
                detail = (r.text or "").strip()[:300] or "(empty body)"
                return jsonify({
                    "ok": False, "model": model,
                    "error": f"HTTP {r.status_code} from {base}/chat/completions: {detail}",
                }), 502
            last = f"HTTP {r.status_code}, still booting"

        if time.monotonic() + VLLM_POLL_INTERVAL < deadline:
            time.sleep(VLLM_POLL_INTERVAL)

    elapsed = round(time.monotonic() - start, 1)
    cold = elapsed > 10  # a warm replica answers in well under a second
    short = model.split("/")[-1]
    return jsonify({
        "ok": True,
        "model": model,
        "elapsed": elapsed,
        "attempts": attempts,
        "state": "cold start" if cold else "already warm",
        "message": f"{short} is warm — responded in {elapsed:.1f}s ({'cold start' if cold else 'already warm'})",
    })


# ── Claude Code usage ────────────────────────────────────────────────────────
# Claude Code writes per-session transcripts to ~/.claude/projects/**/*.jsonl.
# Each assistant line carries a top-level ISO `timestamp`, `message.model`, and
# `message.usage` token counts. There is NO local record of Anthropic's real
# rate-limit window or token cap, so the "current window" is reconstructed from
# message timestamps (first activity → +WINDOW, resets after a >=WINDOW gap) and
# the limit is a user-tunable estimate calibrated against Claude Code's /status.
CLAUDE_PROJECTS_DIR = os.environ.get(
    "CLAUDE_PROJECTS_DIR", os.path.expanduser("~/.claude/projects")
)
CLAUDE_WINDOW_HOURS = float(os.environ.get("CLAUDE_WINDOW_HOURS", "5"))
# Approximate per-window token budget; 0 = unknown → frontend hides the bar.
CLAUDE_WINDOW_TOKEN_LIMIT = int(os.environ.get("CLAUDE_WINDOW_TOKEN_LIMIT", "20000000"))


def _parse_ts(value):
    """Parse an ISO-8601 timestamp (with trailing Z) to an aware UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _collect_claude_events(window):
    """Return (ts, model, usage) tuples for assistant lines written within the
    last `window` (+1h margin). Files are pre-filtered by mtime so we only read
    transcripts that could contain in-window activity."""
    cutoff = datetime.now(timezone.utc) - (window + timedelta(hours=1))
    events = []
    pattern = os.path.join(CLAUDE_PROJECTS_DIR, "**", "*.jsonl")
    for path in glob.glob(pattern, recursive=True):
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < cutoff:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    ts = _parse_ts(rec.get("timestamp"))
                    if not usage or ts is None:
                        continue
                    events.append((ts, msg.get("model") or "unknown", usage))
        except (OSError, UnicodeDecodeError):
            continue
    events.sort(key=lambda e: e[0])
    return events


@app.route("/api/usage/claude-code")
def claude_code_usage():
    window = timedelta(hours=CLAUDE_WINDOW_HOURS)
    try:
        events = _collect_claude_events(window)
    except Exception as e:  # never let a bad transcript 500 the dashboard
        return jsonify({"error": str(e)}), 500

    now = datetime.now(timezone.utc)
    empty = {
        "active": False,
        "window_start": None,
        "reset_at": None,
        "seconds_to_reset": 0,
        "total_tokens": 0,
        "breakdown": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "by_model": {},
        "limit": CLAUDE_WINDOW_TOKEN_LIMIT,
    }
    if not events:
        return jsonify(empty)

    # Reconstruct the active window: walk forward, resetting the window start
    # whenever an event lands beyond the prior window's reset point.
    window_start = events[0][0]
    for ts, _model, _usage in events:
        if ts >= window_start + window:
            window_start = ts
    reset_at = window_start + window
    if now >= reset_at:  # the last window has elapsed → no active window
        return jsonify(empty)

    breakdown = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    by_model = {}
    for ts, model, usage in events:
        if not (window_start <= ts < reset_at):
            continue
        parts = {
            "input": int(usage.get("input_tokens", 0) or 0),
            "output": int(usage.get("output_tokens", 0) or 0),
            "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
            "cache_creation": int(usage.get("cache_creation_input_tokens", 0) or 0),
        }
        m = by_model.setdefault(
            model, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
        )
        for k, v in parts.items():
            breakdown[k] += v
            m[k] += v
            m["total"] += v

    total = sum(breakdown.values())
    return jsonify({
        "active": True,
        "window_start": window_start.isoformat().replace("+00:00", "Z"),
        "reset_at": reset_at.isoformat().replace("+00:00", "Z"),
        "seconds_to_reset": int((reset_at - now).total_seconds()),
        "total_tokens": total,
        "breakdown": breakdown,
        "by_model": by_model,
        "limit": CLAUDE_WINDOW_TOKEN_LIMIT,
    })


# ── Static demo snapshot ────────────────────────────────────────────────
# Captures the live JSON from every read endpoint, bakes it into a
# self-contained copy of the dashboard whose fetch() is shimmed to serve the
# captured data (and fake the POST actions), and deploys that copy to
# Cloudflare Pages via wrangler.

DEMO_DECK        = "Mandarin HSK 1000-5000"
SNAPSHOT_DIR     = os.environ.get("SNAPSHOT_DIR", "demo")
CF_PAGES_PROJECT = os.environ.get("CF_PAGES_PROJECT", "dashboard-demo")
# Production branch the custom domain (dashboard.evanyoung.dev) serves, and the
# canonical public URL to report back after a deploy.
CF_PAGES_BRANCH  = os.environ.get("CF_PAGES_BRANCH", "main")
CF_PAGES_URL     = os.environ.get("CF_PAGES_URL", "https://dashboard.evanyoung.dev")

# Injected just before the dashboard's own <script>. Overrides fetch() so all
# /api reads return the captured snapshot and all POST actions resolve as a
# believable success — the live client-side clock keeps ticking untouched.
DEMO_SHIM = """<script>
/* ── Static demo shim — injected at snapshot build time ─────────────── */
(function () {
  const MOCK = __MOCK_JSON__;
  const resp = (o) => new Response(JSON.stringify(o), { status: 200, headers: { 'Content-Type': 'application/json' } });
  const nap  = (ms) => new Promise((r) => setTimeout(r, ms));
  const real = window.fetch.bind(window);

  window.fetch = async function (input, opts = {}) {
    const url  = typeof input === 'string' ? input : (input && input.url) || '';
    const [path, qs] = url.split('?');
    const q    = new URLSearchParams(qs || '');
    const method = (opts.method || 'GET').toUpperCase();
    await nap(160 + Math.random() * 220);

    if (method === 'POST') {
      if (path === '/api/slackdump/backup')
        return resp({ ok: true, stdout: 'Dispatched backup workflow → slackdump-pipeline (run #482)', stderr: '' });
      if (path === '/api/obsidian/publish')
        return resp({ ok: true, pr_url: 'https://github.com/EvanDavidYoung/obsidian-notes/pull/128', output: 'Published 342 notes · opened PR #128' });
      if (path === '/api/vllm/warm') { await nap(2400); return resp({ ok: true, message: 'Warm in 41.2s (cold start · snapshot restored)' }); }
      return resp({ ok: true });          // /sync, /api/podcasts/sync, /api/snapshot/publish …
    }

    if (path === '/api/weather')     return resp(MOCK.weather[q.get('loc') || 'ny'] || MOCK.weather.ny);
    if (path === '/api/today')       return resp(MOCK.today);
    if (path === '/api/total-time')  return resp(MOCK.total_time);
    if (path === '/api/stats')       return resp(MOCK.stats[q.get('year')] || { heatmap: {} });
    if (path === '/api/usage/claude-code') return resp(MOCK.usage);
    if (path === '/api/podcasts') {
      const y = q.get('year');
      if (!y) return resp(MOCK.podcasts_all);
      return resp(MOCK.podcasts[y] || { total_hours: 0, feeds: [], heatmap: {} });
    }
    return real(input, opts);           // let anything else (CDN) through
  };

  // Cosmetic: mark it as a demo, drop the publish button (no backend here).
  document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('snapshot-widget')?.remove();
    const h1 = document.querySelector('.dash-header h1');
    if (h1 && MOCK.captured_at) {
      const badge = document.createElement('span');
      badge.textContent = 'DEMO · ' + MOCK.captured_at;
      badge.style.cssText = 'margin-left:10px;font-size:0.6rem;font-weight:700;letter-spacing:.08em;' +
        'text-transform:uppercase;color:var(--fg4);border:1px solid var(--bg2);border-radius:10px;padding:2px 8px;vertical-align:middle;';
      h1.appendChild(badge);
    }
  });
})();
</script>
"""


def build_snapshot_html(overrides=None):
    """Render a self-contained demo copy of the dashboard from live endpoint data."""
    year  = datetime.now().year
    years = [year, year - 1]
    client = app.test_client()

    def g(path, **qs):
        return client.get(path, query_string=qs).get_json()

    data = {
        "captured_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        "weather":      {loc: g("/api/weather", loc=loc) for loc in WEATHER_LOCATIONS},
        "today":        g("/api/today", deck=DEMO_DECK),
        "total_time":   g("/api/total-time", deck=DEMO_DECK),
        "stats":        {str(y): g("/api/stats", deck=DEMO_DECK, year=y) for y in years},
        "podcasts":     {str(y): g("/api/podcasts", year=y) for y in years},
        "podcasts_all": g("/api/podcasts"),
        "usage":        g("/api/usage/claude-code"),
    }
    if overrides:
        data.update(overrides)

    with app.app_context():
        tpl = render_template("index.html")
    shim = DEMO_SHIM.replace("__MOCK_JSON__", json.dumps(data))
    # Inject the shim immediately before the dashboard's own inline script.
    marker = "<script>\n'use strict';"
    if marker not in tpl:
        raise RuntimeError("could not find dashboard <script> injection point")
    return tpl.replace(marker, shim + marker, 1)


@app.route("/api/snapshot/publish", methods=["POST"])
@require_api_key
def snapshot_publish():
    try:
        html = build_snapshot_html()
    except Exception as e:  # capture (Anki/network) failure
        return jsonify({"ok": False, "error": f"snapshot capture failed: {e}"}), 500

    out_dir = os.path.join(app.root_path, SNAPSHOT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    # Also publish the static clock-glyph test page (served at /test). It's
    # fully client-side, so a plain copy is all that's needed.
    test_src = os.path.join(app.root_path, "test", "clock-glyphs.html")
    if os.path.exists(test_src):
        shutil.copyfile(test_src, os.path.join(out_dir, "test.html"))

    # Always target the production branch so the custom domain reflects the deploy.
    cmd = ["npx", "--yes", "wrangler", "pages", "deploy", out_dir,
           f"--project-name={CF_PAGES_PROJECT}", f"--branch={CF_PAGES_BRANCH}",
           "--commit-dirty=true"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=420, cwd=app.root_path)
    except FileNotFoundError:
        return jsonify({"ok": False,
                        "error": "npx/wrangler not found — install Node.js, then `npm i -g wrangler`"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "wrangler deploy timed out after 7 min"}), 504

    output = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode != 0:
        return jsonify({"ok": False, "error": "wrangler deploy failed", "output": output}), 500

    # The custom domain always serves the production deploy; report it as the
    # canonical URL, and include the per-deploy preview URL for reference.
    previews = re.findall(r"https://[^\s]+\.pages\.dev", output)
    return jsonify({"ok": True, "url": CF_PAGES_URL,
                    "preview_url": previews[-1] if previews else None,
                    "output": output})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5555))
    if not API_KEY:
        print("WARNING: REFRESH_API_KEY is not set — /sync endpoint is unprotected")
    print(f"Listening on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
