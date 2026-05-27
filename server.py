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
from collections import defaultdict
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

ANKI_CONNECT_URL = "http://localhost:8765"
API_KEY = os.environ.get("REFRESH_API_KEY", "")
WEATHER_LOCATION = os.environ.get("WEATHER_LOCATION", "Taipei")


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5555))
    if not API_KEY:
        print("WARNING: REFRESH_API_KEY is not set — /sync endpoint is unprotected")
    print(f"Listening on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
