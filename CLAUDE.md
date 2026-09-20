# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python script (`agenda_local.py`, ~1400 lines, stdlib only, Python 3.9+) that aggregates
local events around Montignac (Dordogne, France) from several public sources and generates two static
outputs, published to GitHub Pages via `.github/workflows/agenda.yml` (daily cron + on push to `main`):

- `sortie/agenda.ics` — a subscribable iCal calendar
- `sortie/index.html` — a mobile-friendly page ("ce week-end" / "mercredi" / etc.)

All configuration (search center/radius, category radius steps, feed URLs, scraped pages, libraries,
cinemas) lives in `config.json` — read it before changing behavior tied to a specific place/source.

## Commands

```bash
python3 agenda_local.py             # fetch everything, generate sortie/agenda.ics + sortie/index.html
python3 agenda_local.py --probe     # diagnostic: check OpenAgenda access/geo field without full run
python3 -m unittest discover -s tests -v   # run the test suite (what CI runs)
python3 -m unittest tests.test_agenda_local.TestDistance -v   # run a single test class
```

No build step, no package manager, no external dependencies — everything is `stdlib` (`urllib`,
`html.parser`, `dataclasses`, `zoneinfo`, etc.), by design ("Python 3.9+, aucune dépendance externe").

`DATATOURISME_KEY` is required for the DATAtourisme source: read from the `DATATOURISME_KEY` env var
first, falling back to a `.env` file next to the script (see `datatourisme_key()`). The GitHub Actions
workflow injects it from a repo secret; locally it comes from `.env` (gitignored, never read it into
chat/logs).

## Architecture

The script is organized as a fixed pipeline, in `main()`:

1. **Fetch** — each source is independent and wrapped so a broken source doesn't kill the run:
   - `fetch_openagenda` — Opendatasoft public dataset, geo-filtered by radius (no key)
   - `fetch_datatourisme` — DATAtourisme REST API (needs `DATATOURISME_KEY`)
   - `fetch_feeds` — generic `.ics` feeds listed in `config.json["flux_ics"]`
   - `fetch_web_pages` — HTML scraper (`_TextLines`, an `HTMLParser` subclass) for the Montignac town
     agenda pages listed in `config.json["pages_web"]`, paginated
   - `fetch_library_hours` / `fetch_cinema_info` — scrape opening-hours/info blocks for libraries and
     cinemas (`config.json["bibliotheques"]` / `["cinemas"]`); these are "lieux" (places), not
     dated events, and are rendered separately in the HTML
2. **Merge** — `dedupe()` (title/date/place fuzzy matching via `difflib`), `apply_distance()` (haversine
   filter against `config.json["rayon_km"]`, using per-category radius tiers in `paliers_rayon`),
   `mark_long_running()` (flags multi-day/ongoing events differently from single-date ones)
3. **Write** — `write_ics()` (RFC5545 folding/escaping helpers: `_fold`, `_ics_escape`, `event_uid`) and
   `write_html()` (builds the categorized/time-bucketed page: `_bucketize`, `_section`, `_card`,
   `_place_card`, favorites via `_fav_button`)

Everything funnels through the `Event` dataclass (near top of file). `category` is assigned per-source
by keyword classifiers (`openagenda_category`, `datatourisme_category`) against the shared `CATEGORIES`
list and keyword sets (`_SPORT_KEYWORDS`, `_NATURE_KEYWORDS`, etc.) — when adding a new source or tuning
classification, match the same category vocabulary so cross-source dedupe and HTML filtering stay
consistent.

Time windows (`weekend_window`, `next_weekend_window`, `week_window`) are computed relative to
`datetime.now(TZ)` with `TZ = ZoneInfo("Europe/Paris")` — the HTML's "this weekend"/"this week" sections
shift day-to-day, which is why the workflow runs on a daily cron rather than only on push.

## Workflow expectations

After any edit to `agenda_local.py` or `config.json`, run `python3 agenda_local.py` to regenerate
`sortie/index.html` and `sortie/agenda.ics` — do this proactively, without waiting to be asked, so the
user can immediately open `sortie/index.html` in a browser to check the change. Don't wait for
confirmation first; only skip it if the edit obviously doesn't affect output (e.g. a comment-only change).

## Tests

`tests/test_agenda_local.py` is network-free: HTML/JSON parsers are tested against fixtures recorded
once from the real sources and stored in `tests/fixtures/` — never hit the live network from a test.
Comments in French are load-bearing (radius tiers, category keyword rationale); keep new ones in French
to match.
