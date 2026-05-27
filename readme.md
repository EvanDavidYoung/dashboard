# Anki Dashboard

Personal dashboard displaying Anki study stats and weather. Built with Flask + vanilla JS.

## Requirements

- Python 3.12+
- [Anki](https://apps.ankiweb.net/) running with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on (listens on `localhost:8765`)
- [uv](https://github.com/astral-sh/uv) for package management

## Running

```bash
REFRESH_API_KEY=mysecret uv run python server.py
```

Open `http://localhost:5555` in a browser.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `REFRESH_API_KEY` | *(unset)* | Protects the `/sync` endpoint. If unset, sync is unprotected. |
| `WEATHER_LOCATION` | `Taipei` | City name passed to wttr.in for weather data. |
| `PORT` | `5555` | Port the server listens on. |

## Features

### Anki stats
- **Review heatmap** — GitHub-style calendar showing daily review counts for the current deck. Navigate years with ◄/►.
- **Today's session** — Cards studied, time, again %, learn/review/relearn/filtered breakdown, correct answers on mature cards.
- **All-time total** — Total hours studied across all years for the deck.
- **Sync button** — Triggers AnkiWeb sync via AnkiConnect.

The deck is set to `Mandarin HSK 1000-5000` by default (edit `state.deck` in `index.html` to change it).

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
