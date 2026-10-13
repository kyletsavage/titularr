# Titularr

**Bulk-add every movie and show whose title matches your keywords to Radarr and Sonarr.**

Titularr is a companion tool for the *arr stack. You give it a word, phrase, or pattern, and it searches movie and TV metadata sources (starting with TMDB) for every movie and series whose title matches, then adds them all in one pass with the quality profile, root folder, and monitoring options you choose. From there, Radarr and Sonarr handle downloading as usual, and your media server picks everything up through your existing libraries.

Want every movie with "Christmas" in the title for a holiday collection? Every show with "Star Trek" in the name? Every film titled "Dracula" across a century of remakes? That's what Titularr is for.

> **Status:** Early development. There is no working code yet. This README describes what the project is intended to do, and everything below is subject to change.

---

## Why

Radarr and Sonarr are built to add one title at a time, or to sync from curated lists (Trakt, IMDb, Letterboxd). Neither has a way to say "find everything with *this* in its title and add it all." Doing that by hand means searching, scrolling, and clicking through dozens or hundreds of results. Titularr automates that loop.

## Planned features

- **Keyword and pattern matching** on titles: plain substrings, whole-word matches, or regular expressions
- **Movies and shows** in one run, targeting Radarr, Sonarr, or both
- **Dry-run mode** that lists what *would* be added without touching anything, so you can review before committing
- **Filters** to narrow results by year range, original language, minimum rating or vote count, and runtime
- **Exclusion lists** for titles, IDs, or patterns you never want added
- **Per-run settings** for quality profile, root folder, tags, monitoring mode, and whether to trigger a search on add
- **Duplicate awareness**, so titles already in your library are skipped and reported rather than re-added
- **Saved queries** that can be re-run on a schedule to catch newly released matches
- **Docker image** for easy deployment alongside the rest of your stack

## Configuration

Titularr reads a YAML file: `config.yaml` in the current folder, or the path in the `TITULARR_CONFIG` environment variable. Copy [`config.example.yaml`](config.example.yaml) to `config.yaml` to start. `config.yaml` is git-ignored, so your settings stay local.

Secrets stay out of the file: any `${NAME}` is replaced with the environment variable `NAME`. Titularr searches [TMDB](https://www.themoviedb.org/), so you need a free TMDB account and its **API Read Access Token** (Settings → API), set as `TMDB_API_TOKEN`.

```yaml
sources:
  tmdb:
    token: ${TMDB_API_TOKEN}

radarr:                # optional: skip movies you already have
  url: http://radarr:7878
  api_key: ${RADARR_API_KEY}
sonarr:                # optional: skip series you already have
  url: http://sonarr:8989
  api_key: ${SONARR_API_KEY}

exclusions:            # never keep these, in any query ("global" exclusions)
  ids: [movie:1399]
  titles: [sing-along]

queries:
  holiday-movies:
    text: christmas
    kinds: [movie]     # movie and/or series (default: both)
    match: word        # substring (default) | word | regex (with pattern:)
    filters:
      year_min: 1940
      min_votes: 500   # also: year_max, min_rating, languages (default: English), keep_unknown
    exclusions:        # this query's own, checked after the global list
      titles: [parody]
```

Unknown keys are reported as errors, so typos don't go unnoticed.

**Radarr and Sonarr are optional.** With them configured, previews leave out titles you already have (shown as `in library` with `--show-dropped`). Titularr only *reads* your libraries. Find each API key under Settings → General → Security. If a `radarr:` or `sonarr:` section is present but can't be used (say, its API key variable isn't set), you get a warning and that library check is skipped. Series that Sonarr has no TMDB ID for can't be recognized, and a warning lists them.

## Usage

Install with [uv](https://docs.astral.sh/uv/) from a clone of this repository, then run commands with `uv run`:

```bash
uv sync

# Check config.yaml without contacting TMDB
uv run titularr validate

# Run a saved query from config.yaml
uv run titularr run holiday-movies

# One-off search; see `uv run titularr search --help` for every option
uv run titularr search "star trek" --kind series --min-votes 100

# Also list what was dropped, and why (no match, excluded, filtered, in library)
uv run titularr run holiday-movies --show-dropped
```

**Every command is a preview:** it lists the titles a query keeps and never changes anything. Adding to Radarr and Sonarr will need an explicit `--apply`, so a forgotten flag can never bulk-add your library.

## A word of caution

Broad keywords can match a *lot* of titles. Review the preview first, use filters and exclusions to keep results relevant, and, once adding exists, consider leaving `search_on_add` off until you've reviewed what landed in Radarr and Sonarr. Your indexers and your disks will thank you.

## Roadmap

- [x] Project scaffolding
- [x] TMDB movie search (all result pages)
- [x] TMDB series search
- [x] Title matching (substring, word, regex)
- [x] Filters (year, language, rating, votes)
- [ ] Runtime filter (needs a lookup per title)
- [x] Exclusions per run (TMDB IDs, title patterns)
- [x] Config file and preview output (`titularr search` / `run` / `validate`)
- [x] Radarr and Sonarr library awareness (skip titles you already have, read-only)
- [ ] Run history (SQLite)
- [ ] Adding matches to Radarr and Sonarr with `--apply` (including the TVDB ID lookup Sonarr needs for series)
- [ ] More metadata sources (optional, e.g. Trakt, TVmaze, TVDB)
- [ ] Saved queries and scheduling
- [ ] Docker service with an HTTP API
- [ ] Beyond-title search: collections, keywords, characters (e.g. every Batman movie)
- [ ] Radarr/Sonarr lookup as a source
- [ ] Optional web UI (including manual review and an exclusion blacklist)

## Contributing

The project is just getting started. Issues with ideas, use cases, and feature requests are welcome.

## License

GPL-3.0. See [LICENSE](LICENSE).
