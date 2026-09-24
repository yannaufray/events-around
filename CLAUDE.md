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
   - `fetch_pole_prehistoire` — dedicated single-page HTML scraper (source 3ter) for
     pole-prehistoire.com's events listing
   - `fetch_brivetourisme` — paginated HTML scraper (source 3quater) for brive-tourisme.com's agenda
     widget (`config.json["brive_tourisme"]`); each list item is already a single date/commune (the site
     itself splits recurring/multi-date events into one row per occurrence), so unlike culturedordogne
     this one is fully parsed — but it spans all of Corrèze around Brive (Tulle, Turenne...), not just
     Brive-ville, and the listing only gives a commune name, not per-item coordinates, so every occurrence
     is approximated with Brive-la-Gaillarde's own lat/lon (`config.json["brive_tourisme"]["lat"/"lon"]`) —
     imprecise for outlying communes, but needed so the radius filter (and the UI's distance-tier menu)
     doesn't treat these as "always at Montignac" the way genuinely coordinate-less local feeds are.
     Paginates via `?id1[currentPage]=N` (results sorted by date) and stops once a page's dates fall past
     the fetch window.
   - `fetch_perigueux` — regex-based RSS scraper (source 3quinquies) for perigueux.fr's agenda feed
     (`config.json["perigueux"]`); the feed's XML is invalid (undeclared `ev:` namespace) so `xml.etree`
     can't parse it, hence regex instead of a real XML parser
   - `fetch_sarlat_mairie` / `fetch_sarlat_centreculturel` — server-rendered HTML scrapers (source
     3sexies/3septies) for sarlat.fr's WordPress/Elementor+JetEngine agenda and
     sarlat-centreculturel.fr's own season listing (`config.json["sarlat_mairie"]` /
     `["sarlat_centreculturel"]`); Sarlat's own tourism-office site (sarlat-tourisme.com) is a JS
     single-page app (Woody/tourism-system.com CMS, mustache-style `{% title %}` templates filled by an
     authenticated API call) with no usable server-rendered markup, so it's skipped — sarlat.fr and
     sarlat-centreculturel.fr cover most of the same events and both render real HTML server-side.
   - `fetch_vezere_perigord` — source 3octies, for vezere-perigord.fr (Office de Tourisme Vézère Périgord
     Noir, covers Terrasson-Lavilledieu/Le Lardin-Saint-Lazare/Hautefort — none of which have their own
     usable site — not Montignac itself; `config.json["vezere_perigord"]`). Runs the same Woody CMS /
     tourism-system.com stack as sarlat-tourisme.com and looks like a JS SPA at first, but unlike
     sarlat-tourisme.com its agenda listing (`?listpage=N`) embeds the full item list server-side as JSON
     in `<script>var itemsData = [...]</script>` (extracted with `_extract_js_json`, a brace-counting
     helper — a regex alone can't find the matching end past nested strings/braces) — so it's scraped, not
     skipped. sarlat-tourisme.com itself is still skipped: its page has no equivalent embedded JSON blob.
     Each item carries its own GPS (unlike Brive Tourisme/Périgueux, which approximate every occurrence
     with one town-level point) and sometimes a `link`/`website`; without either the URL is left empty
     (`_card()` falls back to a Google search, same as DATAtourisme items with no dedicated page).
   - `fetch_marches` — not a scraper: weekly markets (Montignac, Sarlat, Brive, Périgueux) have no
     structured per-date listing worth scraping (they're always-the-same-weekday recurring events), so
     they're entered by hand in `config.json["marches"]` (day/hours verified against each town's official
     site) and rendered as "lieux" like libraries/cinemas, not as dated `Event`s
   - `config.json["liens_utiles"]` (no fetch function — built directly in `main()`) — a fallback for
     sources whose events aren't worth scraping at all: markup too irregular/brittle to parse reliably
     (e.g. culturedordogne.fr's "saison" listings: several dates/communes per touring item, some items
     with no date at all in the listing), or where even a correctly-parsed single date/place per item
     would misrepresent a multi-date touring event. Each entry is rendered as a link-out card via
     `_place_card`, in a prominent "À voir aussi" block placed right under the page title (not buried at
     the bottom) — with a `lignes` description giving the general period/frequency (e.g. "toute l'année
     scolaire") rather than fabricated per-event dates. Reuse this before writing a new parser for a
     similarly irregular source; only actually scrape events if a single reliable date per item is
     achievable without misleading users about recurring/touring events.
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
