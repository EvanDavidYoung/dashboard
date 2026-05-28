# Mandarin Dashboard

Personal dashboard for Mandarin study — Anki flashcard stats, Chinese podcast listening time, and weather. Built with Flask + vanilla JS.

## Requirements

- Python 3.12+
- [Anki](https://apps.ankiweb.net/) running with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on (listens on `localhost:8765`)
- [uv](https://github.com/astral-sh/uv) for package management
- [overcast-to-sqlite](https://github.com/hbmartin/overcast-to-sqlite) (optional) for podcast stats — exports Overcast data to `overcast.db`

## Running

```bash
REFRESH_API_KEY=mysecret uv run python server.py
```

Open `http://localhost:5555` in a browser.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `REFRESH_API_KEY` | *(unset)* | Protects `/sync` and `/api/podcasts/sync`. If unset, both are unprotected. |
| `WEATHER_LOCATION` | `Taipei` | City name passed to wttr.in for weather data. |
| `OVERCAST_DB` | `overcast.db` | Path to the overcast-to-sqlite database file. |
| `OVERCAST_AUTH` | `auth.json` | Path to the overcast-to-sqlite auth cookie file. |
| `OVERCAST_CLI` | `overcast-to-sqlite` | Path or name of the overcast-to-sqlite binary. |
| `PORT` | `5555` | Port the server listens on. |

## Features

### Anki stats
- **Review heatmap** — GitHub-style calendar showing daily review counts for the current deck. Navigate years with ◄/►.
- **Today's session** — Cards studied, time, again %, learn/review/relearn/filtered breakdown, correct answers on mature cards.
- **All-time total** — Total hours studied across all years for the deck.
- **Sync button** — Triggers AnkiWeb sync via AnkiConnect.

The deck is set to `Mandarin HSK 1000-5000` by default (edit `state.deck` in `index.html` to change it).

### Chinese podcast listening
- **Listening heatmap** — Daily listening time calendar (orange/yellow gruvbox scale, 2h = max color). Navigate years with ◄/►.
- **Per-show breakdown** — Horizontal bar chart of hours + episode count per show, sorted by total time.
- **Total hours** — All-time listening hours shown inside the podcast widget.
- **Sync button** — The ↺ button in the podcast widget header pulls fresh data from Overcast via `overcast-to-sqlite`, then refreshes the heatmap and totals. Requires `auth.json` (run `overcast-to-sqlite auth` once to create it).

Episode durations are fetched from RSS on first load and cached permanently in `episode_durations` (SQLite). Subsequent loads are pure SQL (~0.2s). `userUpdatedDate` timestamps are used to assign episodes to calendar days; daily totals are capped at 24h to prevent bulk sync artifacts from inflating a single day.

### Total Chinese Time
Combined widget showing all-time Anki hours + podcast listening hours with a single summed total.

### Weather
Current conditions for the configured city via [wttr.in](https://wttr.in) (no API key required). Contextual alerts are shown for:
- ☂️ Rain forecast (current precip or today's hourly max > 1 mm)
- 🔥 Extreme heat (feels like ≥ 104°F) / 🌡️ Heat wave (≥ 95°F) / 😓 Hot and humid (≥ 88°F)
- 🧥 Cold (≤ 50°F)
- 💨 Strong winds (≥ 60 km/h) / 🌬️ Breezy (≥ 40 km/h)
- 🕶️ Extreme UV (≥ 11) / High UV (≥ 8)

## API endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/` | — | Dashboard SPA |
| `GET` | `/health` | — | AnkiConnect reachability check |
| `POST` | `/sync` | API key | Trigger AnkiWeb sync |
| `GET` | `/api/decks` | — | Sorted list of deck names |
| `GET` | `/api/stats?deck=X&year=Y` | — | Heatmap data (daily counts) for a deck/year |
| `GET` | `/api/today?deck=X` | — | Today's session stats, always current day |
| `GET` | `/api/total-time?deck=X` | — | All-time total hours studied for a deck |
| `GET` | `/api/weather` | — | Current weather from wttr.in |
| `GET` | `/api/podcasts?year=Y` | — | Per-show totals (all-time) + daily heatmap for the year |
| `POST` | `/api/podcasts/sync` | API key | Run `overcast-to-sqlite save` to pull fresh data from Overcast |
