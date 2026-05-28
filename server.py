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
import os
import re
import shutil
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

ANKI_CONNECT_URL = "http://localhost:8765"
API_KEY = os.environ.get("REFRESH_API_KEY", "")
WEATHER_LOCATION = os.environ.get("WEATHER_LOCATION", "Taipei")
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
        loc = requests.utils.quote(WEATHER_LOCATION, safe="")
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
            "location":           WEATHER_LOCATION,
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5555))
    if not API_KEY:
        print("WARNING: REFRESH_API_KEY is not set — /sync endpoint is unprotected")
    print(f"Listening on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
