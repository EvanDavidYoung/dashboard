# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

```bash
REFRESH_API_KEY=mysecret WEATHER_LOCATION="Taipei" uv run python server.py
```

The server listens on port 5555 by default; override with `PORT=<n>`. AnkiConnect must be running inside Anki on the same machine (port 8765).

Env vars:
- `REFRESH_API_KEY` — protects `/sync` and `/api/podcasts/sync`; if unset, both endpoints are unprotected
- `WEATHER_LOCATION` — city name passed to wttr.in (default: `Taipei`)
- `OVERCAST_DB` — path to overcast-to-sqlite database (default: `overcast.db`)
- `OVERCAST_AUTH` — path to overcast-to-sqlite auth cookie file (default: `auth.json`)
- `OVERCAST_CLI` — overcast-to-sqlite binary name/path (default: `overcast-to-sqlite`)
- `PORT` — server port (default: `5555`)
- `CLAUDE_PROJECTS_DIR` — Claude Code transcript dir scanned for token usage (default: `~/.claude/projects`)
- `CLAUDE_WINDOW_HOURS` — rolling usage-window length in hours (default: `5`)
- `CLAUDE_WINDOW_TOKEN_LIMIT` — approximate per-window token budget for the progress bar (default: `20000000`); set to `0` to hide the bar
- `VLLM_BASE_URL` — inference endpoint; accepts a host root *or* a base already ending in `/v1`
- `VLLM_API_KEY` — sent as `Authorization: Bearer …`; for a Modal Endpoint this is the combined `wk-<id>.ws-<secret>` proxy token
- `VLLM_MODEL` — served model id (default: `Qwen/Qwen3.6-35B-A3B`)
- `VLLM_WARM_TIMEOUT` — total seconds to keep polling a cold endpoint (default: `720`)
- `VLLM_POLL_INTERVAL` — seconds between warm-up polls (default: `5`)

The `VLLM_*` group is read from `.env` *before* the ambient shell (`env_file_first()` in `server.py`), because `~/.zshenv` exports a stale `VLLM_API_KEY` for a different endpoint and `load_dotenv()` will not overwrite an already-exported variable. Every other var uses normal precedence, so `FOO=bar uv run python server.py` still works.

## Architecture

`server.py` is the entire backend — Flask routes proxying AnkiConnect, wttr.in, and an Overcast SQLite database:

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
| `GET /api/podcasts?year=Y` | Per-show totals (all-time) + daily listening heatmap for the year |
| `POST /api/podcasts/sync` | Shells out to `overcast-to-sqlite save`; pulls fresh data from Overcast; `require_api_key` |
| `GET /api/usage/claude-code` | Reconstructs the current rolling token-usage window from `~/.claude/projects/**/*.jsonl` and returns totals + per-model breakdown; unauthenticated |

All AnkiConnect calls go through `anki_request(action, **params)`, which POSTs to `http://localhost:8765` using the AnkiConnect v6 JSON protocol. Responses are flat lists: `[reviewTime_ms, cardId, usn, ease, ivl, lastIvl, factor, time_ms, type]` — key indices are `[0]` ts, `[3]` ease, `[5]` lastIvl, `[7]` time_ms, `[8]` type.

`/api/stats` and `/api/today` are intentionally separate so year navigation in the heatmap never resets the today's stats display.

### Podcast data (`/api/podcasts`)

Reads from `overcast.db` (produced by `overcast-to-sqlite`). Episode durations are not stored in the Overcast export, so they are fetched from RSS XML on first load and cached permanently in a `episode_durations` table (`enclosureUrl PK, duration_seconds`). The gap query (`NOT EXISTS`) ensures each feed's RSS is only fetched when it has uncached played/in-progress episodes — subsequent requests are pure SQL.

Daily heatmap values use `DATE(e.userUpdatedDate)` for day assignment. Totals per day are capped at 86400s (24h) to prevent bulk "mark as played" sync events from inflating a single day.

## Frontend (`templates/index.html`)

Self-contained SPA — vanilla JS, no framework, Gruvbox dark color scheme via CSS variables (`:root` block at top of `<style>`).

**Widget system**: Each widget is a plain JS object `{ init(), refresh() }` registered in the `WIDGETS` array at the bottom of the `<script>` block. `Promise.all(WIDGETS.map(w => w.init()))` boots them all in parallel. To add a new widget: add its HTML container, implement the object, push to `WIDGETS`.

**Current widgets** (in render order):
- `WeatherWidget` — fetches `/api/weather`, renders conditions + contextual alert chips
- `TodayWidget` — fetches `/api/today`, renders session stats; never re-fetches on year changes
- `ChineseTotalWidget` — fetches `/api/total-time` + `/api/podcasts` in parallel; shows combined hours with Anki/listening breakdown
- `PodcastWidget` — fetches `/api/podcasts?year=Y`; renders daily listening heatmap (orange/yellow) + per-show bar chart; year nav + ↺ button in header. The ↺ button calls `syncAndRefresh()`: hits `POST /api/podcasts/sync` first, then refreshes both `PodcastWidget` and `ChineseTotalWidget` in parallel.
- `AnkiWidget` — fetches `/api/stats` for heatmap + `/api/total-time`; year nav only re-calls `refresh()` which updates the heatmap only
- `ClaudeCodeWidget` — fetches `/api/usage/claude-code`; renders current-window token total, an optional progress bar (only when `CLAUDE_WINDOW_TOKEN_LIMIT > 0`), a reset countdown, and per-category/per-model chips. Lives in the Usage tab.

**Tabs**: A `.tab-bar` of `.tab-btn[data-tab=…]` toggles `.tab-pane#tab-<name>`. Tabs: `main` (the dashboard widgets), `tools` (button-only actions: Slack Backup, Obsidian Publish — these are wired via direct `addEventListener`, NOT in the `WIDGETS` array), and `usage` (`ClaudeCodeWidget` + a static link card to `modal.com/settings/usage`, since Modal exposes no API for remaining credits). The Claude Code window/limit are *reconstructed/approximate* — transcripts don't store Anthropic's real reset time or token cap.

**Heatmap**: Both the Anki and podcast heatmaps share `buildHeatmapGrid(gridEl, monthsEl, year, data, levelFn, cellClass, tooltipFmt)`. CSS class `.hm-grid` sets the 53×7 grid layout. `.hm-cell` uses a blue scale; `.phm-cell` uses an orange/yellow scale (thresholds: 0.5/1/1.5/2h). `startDow = (jan1.getDay() + 6) % 7` aligns Jan 1 to the correct day-of-week.

**Mobile**: `min-width: 0` on `.widget` prevents grid items from overflowing the viewport. `width: 100%` on `.heatmap-wrap` gives `overflow-x: auto` a definite width to scroll within. Breakpoint at 600px.

**Sync button** (header): fires Anki sync (`POST /sync`) and Overcast sync (`POST /api/podcasts/sync`) in parallel via `Promise.allSettled`. Each updates its own widgets on completion without blocking the other. The button re-enables and shows ✓/✗ once both settle.

`main.py` is an unused scaffold from `uv init`.
