<div align="center">

<img src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎬</text></svg>" width="80" height="80" alt="">

# Merlin Cinemas

**All-in-one cinema tracker for Cornwall — now showing, special events, coming soon, and calendar feeds.**

[![GitHub Pages](https://img.shields.io/badge/GitHub%20Pages-live-22d3ee?style=flat-square)](https://evenwebb.github.io/merlin-cinemas/)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-3776AB?style=flat-square)](https://python.org)
[![CI](https://img.shields.io/badge/CI-daily%2009%3A00%20UTC-22d3ee?style=flat-square)](.github/workflows/scrape.yml)

[Live page](https://evenwebb.github.io/merlin-cinemas/) · [Repository](https://github.com/evenwebb/merlin-cinemas)

</div>

---

## What it does

A single Python file scrapes all seven Merlin Cinemas in Cornwall and generates a complete static site with no server, no database — just GitHub Pages.

**7 cinemas**: Capitol (Bodmin), Flora (Helston), Phoenix (Falmouth), Regal (Redruth), Royal (St Ives), Savoy (Penzance), The Ritz (Penzance).

**Three index sections**: Now Showing (poster grid with list/card toggle), Special Events (RBO, NT Live, Double Bills, Toddler Cinema with colored tags), Coming Soon (cards with synopses, ratings, cinema showings).

**135+ film detail pages**: Full showtime tables with Date / Time / Cinema / Screen columns, per-cinema filter pills, accessibility badges (WA, CC, MM, SAV, LIC), nearest cinema row reordering, TMDb backdrops, trailers, star ratings, synopsis, cast, director, IMDb / Rotten Tomatoes / Trakt links, and BBFC age rating logos.

**7 cinema pages**: Per-venue Now Showing + Coming Soon with map links and calendar feeds.

**Special screenings**: RBO (Royal Ballet & Opera), NT Live (National Theatre), Double Bill, Toddler Cinema, Kids Club, with Q&A — detected via pattern matching, displayed with colored banners and descriptive info boxes, and grouped in their own section.

**iCal + subscribe**: 7 per-cinema `.ics` feeds, Google Calendar one-click, iOS webcal links, copy-to-clipboard, and inline how-to instructions.

**SEO**: sitemap.xml (143 URLs), canonical URLs, Open Graph / Twitter Card meta tags, Schema.org Movie structured data.

---

## Quick Start

```bash
git clone https://github.com/evenwebb/merlin-cinemas.git
cd merlin-cinemas
pip install -r requirements.txt
python3 cinema_scraper.py
```

For TMDb enrichment (posters, ratings, cast, backdrops):

```bash
TMDB_API_KEY=your_key_here python3 cinema_scraper.py
```

Runs offline from cache once populated:

```bash
FORCE_REBUILD=1 python3 cinema_scraper.py
```

---

## Features

### Scraping
| Area | Details |
|---|---|
| **Dual-source** | `/coming-soon/` for future release dates + film pages for full multi-date showtime schedules |
| **Parallel** | `ThreadPoolExecutor` for 7 cinemas simultaneously, `requests.Session` reuse |
| **Showtimes** | Date, time, screen number, accessibility tags (Wheelchair Access, Subtitled, Mini Movie Deal, Super Saver, Licensed), admit-one.eu booking links |
| **Merlin-specific** | Handles subdomain-based cinema routing, `data-film` attributes, `.films_on_day [data-frame]` parsing, ordinal date suffixes |

### Enrichment
| Area | Details |
|---|---|
| **TMDb** | Posters (w500), large posters (w780), backdrops (w780), synopses, star ratings, genres, director, cast, trailers, IMDb IDs. Cached 30 days |
| **Cache-only mode** | Runs without `TMDB_API_KEY` by loading existing cache from disk |
| **Title cleaning** | Strips screening suffixes (Toddler Cinema, Double Bill, with Q&A, NT Live:, RBO prefix) before TMDb search |
| **Progressive fallback** | Drops trailing words one at a time and re-searches TMDb for unknown suffix patterns |
| **Non-film skip** | 80+ patterns to skip live events, tribute shows, comedy nights from TMDb enrichment |
| **BBFC** | Age rating extracted from Merlin `data-cert` attributes, TBC stripped, official logo images served locally |

### Index page
| Area | Details |
|---|---|
| **Now Showing** | Poster grid (default) with list view toggle. 2-wide mobile, 5-wide desktop |
| **Special Events** | Dedicated section for RBO, NT Live, Double Bill, Toddler Cinema with colored type tags |
| **Coming Soon** | Rich cards with poster, synopsis, runtime, star rating, genres, BBFC logo, cinema showings |
| **Cinema filter** | 8 pill buttons (All + 7 towns) to filter films by location |
| **Nearest cinema** | Browser geolocation highlights the closest cinema's filter button |
| **Quick-nav** | Pills to jump to Now Showing, Special Events, Coming Soon, Subscribe |
| **Calendar promo** | Fixed banner at top: desktop full text, mobile shortened |
| **Subscribe section** | Per-cinema iOS / Google / Copy / Show URL buttons with inline how-to |
| **Just added** | "New" badge on films first seen in last 7 days |
| **View toggle** | Cards / posters toggle for Coming Soon, list / posters for Now Showing |

### Film detail pages
| Area | Details |
|---|---|
| **Showtime table** | Date / Time / Cinema / Screen columns with cinema filter pills |
| **Accessibility badges** | Color-coded WA, CC, MM, SAV, LIC, AD, AF badges with popup key |
| **Nearest cinema** | Geolocation reorders rows to put closest cinema first with highlight |
| **Backdrop** | Full-width TMDb backdrop behind page with gradient overlay |
| **Trailer** | YouTube embed, prioritises "Trailer" over "Teaser" |
| **Meta** | BBFC logo, runtime, star rating, genres, synopsis, director, cast |
| **Links** | IMDb, Rotten Tomatoes, Trakt |
| **Screening info** | Descriptive info box for special events explaining what they are |

### Special screenings
| Type | Color | Description |
|---|---|---|
| RBO | Red | Royal Ballet & Opera broadcasts from Covent Garden |
| NT Live | Cyan | National Theatre Live broadcasts |
| Double Bill | Purple | Two films or film + event on one ticket |
| Toddler Cinema | Amber | Family screenings with relaxed atmosphere |
| Kids Club | Amber | Kids' activities and special pricing |
| with Q&A | Orange | Film + live Q&A session |
| Silver Screen | Gray | Over-60s exclusive screenings |
| Mini Movie Deal | Purple | Ticket including popcorn and drink |
| Super Saver | Green | Discounted tickets |

### UI / UX
| Area | Details |
|---|---|
| **Dark theme** | CSS custom properties, cyan/purple accent gradient |
| **Mobile responsive** | Scrollable tables, adaptive grids, breakpoints at 480/600/640/680/768/1024px |
| **Accessibility** | Skip-to-content link, ARIA labels, prefers-reduced-motion, keyboard-dismissible popups |
| **Print styles** | `@media print` hides backgrounds, nav, buttons |
| **Performance** | `loading="lazy"` images, fingerprint change detection, cache-based enrichment |

---

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `CINEMAS` | All 7 enabled | Toggle individual venues on/off |
| `MAX_WORKERS` | `min(4, cpu_count)` | Thread pool size |
| `HTTP_RETRIES` | 3 | Retry attempts per request |
| `CACHE_EXPIRY_DAYS` | 7 | Film detail cache TTL |
| `TMDB_CACHE_DAYS` | 30 | TMDb cache TTL |
| `TMDB_API_KEY` (env) | — | Enables live TMDb enrichment |
| `CALENDAR_TIMEZONE` (env) | `Europe/London` | iCal timezone |
| `HEALTH_MIN_FILMS` (env) | 1 | Minimum films before health check fails |
| `HEALTH_MIN_CINEMAS` (env) | 1 | Minimum cinemas before health check fails |

---

## GitHub Pages

1. Settings → Pages → Deploy from a branch
2. Branch: `main`, folder: `/docs`
3. Published at `https://evenwebb.github.io/merlin-cinemas/`

## GitHub Actions

Workflow at `.github/workflows/scrape.yml`:
- Schedule: daily 09:00 UTC, manual trigger
- Timeout: 15 minutes, 2 retries with escalating delays
- Restores caches between runs, auto-commits changes
- Optional failure issue creation

## Dependencies

```
requests>=2.31,<3
beautifulsoup4>=4.12,<5
```

---

## License

MIT
