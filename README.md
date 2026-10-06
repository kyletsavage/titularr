# Titularr

**Bulk-add every movie and show whose title matches your keywords to Radarr and Sonarr.**

Titularr is a companion tool for the *arr stack. You give it a word, phrase, or pattern, and it searches Radarr's and Sonarr's metadata lookups for every movie and series whose title matches, then adds them all in one pass with the quality profile, root folder, and monitoring options you choose. From there, Radarr and Sonarr handle downloading as usual, and your media server picks everything up through your existing libraries.

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

## Configuration (draft)

Configuration will likely be a YAML file plus environment variables for secrets. A rough sketch:

```yaml
radarr:
  url: http://radarr:7878
  api_key: ${RADARR_API_KEY}
  quality_profile: HD-1080p
  root_folder: /movies

sonarr:
  url: http://sonarr:8989
  api_key: ${SONARR_API_KEY}
  quality_profile: HD-1080p
  root_folder: /tv

queries:
  - name: holiday-movies
    match: "christmas"
    mode: word          # substring | word | regex
    targets: [radarr]
    filters:
      year_min: 1940
      min_votes: 500
    tags: [holiday]
    search_on_add: false
```

## Usage (draft)

```bash
# Preview matches without adding anything
titularr run holiday-movies --dry-run

# Add everything that matches
titularr run holiday-movies

# One-off search without a saved query
titularr search "star trek" --targets sonarr --dry-run
```

## A word of caution

Broad keywords can match a *lot* of titles. Always start with `--dry-run`, use filters to keep results relevant, and consider leaving `search_on_add` off until you've reviewed what landed in Radarr and Sonarr. Your indexers and your disks will thank you.

## Roadmap

- [x] Project scaffolding
- [ ] First metadata source (TMDB) with full-result search
- [ ] Matching modes (substring, word, regex), filters, and exclusions
- [ ] Config file loading and dry-run output
- [ ] Radarr and Sonarr library awareness (skip titles you already have)
- [ ] Run history (SQLite)
- [ ] Adding matches to Radarr and Sonarr
- [ ] More metadata sources (TVDB, then others)
- [ ] Saved queries and scheduling
- [ ] Docker service with an HTTP API
- [ ] Beyond-title search: collections, keywords, characters (e.g. every Batman movie)
- [ ] Radarr/Sonarr lookup as a source
- [ ] Optional web UI

## Contributing

The project is just getting started. Issues with ideas, use cases, and feature requests are welcome.

## License

GPL-3.0. See [LICENSE](LICENSE).
