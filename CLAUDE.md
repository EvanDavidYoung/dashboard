# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

```bash
REFRESH_API_KEY=mysecret WEATHER_LOCATION="Taipei" uv run python server.py
```

The server listens on port 5555 by default; override with `PORT=<n>`. AnkiConnect must be running inside Anki on the same machine (port 8765).

Env vars:
- `REFRESH_API_KEY` — protects `/sync`; if unset, the endpoint is unprotected
- `WEATHER_LOCATION` — city name passed to wttr.in (default: `Taipei`)
- `PORT` — server port (default: `5555`)

## Architecture

`server.py` is the entire backend — Flask routes proxying AnkiConnect and wttr.in:

| Route | Description |
|---|---|
| `GET /` | Serves the dashboard SPA |
| `GET /health` | Pings AnkiConnect, unauthenticated |
| `POST /sync` | Triggers AnkiWeb sync via AnkiConnect; `require_api_key` decorator |
| `GET /api/decks` | Returns sorted deck names from `deckNames` AnkiConnect action |
| `GET /api/stats?deck=X&year=Y` | Heatmap only — daily review counts for the year |
| `GET /api/today?deck=X` | Today's session stats, always anchored to current-day midnight UTC |
| `GET /api/total-time?deck=X` | All-time total hours (fetches all reviews with `startID=0`) |
| `GET /api/weather` | Proxies `wttr.in/{WEATHER_LOCATION}?format=j1`; no external API key needed |

All AnkiConnect calls go through `anki_request(action, **params)`, which POSTs to `http://localhost:8765` using the AnkiConnect v6 JSON protocol. Responses are flat lists: `[reviewTime_ms, cardId, usn, ease, ivl, lastIvl, factor, time_ms, type]` — key indices are `[0]` ts, `[3]` ease, `[5]` lastIvl, `[7]` time_ms, `[8]` type.

`/api/stats` and `/api/today` are intentionally separate so year navigation in the heatmap never resets the today's stats display.

## Frontend (`templates/index.html`)

Self-contained SPA — vanilla JS, no framework, Gruvbox dark color scheme via CSS variables (`:root` block at top of `<style>`).

**Widget system**: Each widget is a plain JS object `{ init(), refresh() }` registered in the `WIDGETS` array at the bottom of the `<script>` block. `Promise.all(WIDGETS.map(w => w.init()))` boots them all in parallel. To add a new widget: add its HTML container, implement the object, push to `WIDGETS`.

**Current widgets**:
- `WeatherWidget` — fetches `/api/weather`, renders conditions + contextual alert chips
- `TodayWidget` — fetches `/api/today`, renders session stats; never re-fetches on year changes
- `AnkiWidget` — fetches `/api/stats` for heatmap + `/api/total-time`; year nav only re-calls `refresh()` which updates the heatmap only

**Heatmap**: CSS Grid `repeat(7, 12px)` rows × `repeat(53, 12px)` columns, column-major fill. `startDow = (jan1.getDay() + 6) % 7` aligns Jan 1 to the correct day-of-week. Zero-review cells use `var(--bg2)` (visible warm grey). Active deck is hardcoded as `state.deck = 'Mandarin HSK 1000-5000'`.

**Mobile**: `min-width: 0` on `.widget` prevents grid items from overflowing the viewport. `width: 100%` on `.heatmap-wrap` gives `overflow-x: auto` a definite width to scroll within. Breakpoint at 600px.

`main.py` is an unused scaffold from `uv init`.
