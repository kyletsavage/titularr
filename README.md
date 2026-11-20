# Titularr

**Bulk-add every movie and show whose title matches your keywords to Radarr and Sonarr.**

Titularr is a companion tool for the *arr stack. You give it a word, phrase, or pattern, and it searches movie and TV metadata sources (starting with TMDB) for every movie and series whose title matches, then adds them all in one pass with the quality profile, root folder, and monitoring options you choose. From there, Radarr and Sonarr handle downloading as usual, and your media server picks everything up through your existing libraries.

Want every movie with "Christmas" in the title for a holiday collection? Every show with "Star Trek" in the name? Every film titled "Dracula" across a century of remakes? That's what Titularr is for.

> **Status:** Early development. The command-line tool works: it searches TMDB, previews what it would keep, and adds titles to Radarr and Sonarr with `--apply`. Commands and config may still change between versions. See the [roadmap](#roadmap) for what's planned.

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

radarr:                # optional: skip movies you already have, and add with --apply
  url: http://radarr:7878
  api_key: ${RADARR_API_KEY}
  quality_profile: HD-1080p   # needed for --apply; also: monitored, minimum_availability,
  root_folder: /movies        # search_on_add (default: off), tags
sonarr:                # optional: skip series you already have, and add with --apply
  url: http://sonarr:8989
  api_key: ${SONARR_API_KEY}
  quality_profile: HD-1080p   # needed for --apply; also: monitor, season_folder, series_type,
  root_folder: /tv            # monitor_new_items, search_on_add (default: off), tags

history: titularr.db   # optional: where --apply runs are recorded (default: titularr.db
                       # in the config file's folder)

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
      runtime_min: 80  # minutes (also runtime_max); movies only, one TMDB request per title
    exclusions:        # this query's own, checked after the global list
      titles: [parody]
    radarr:            # this query's own add settings, replacing the radarr section's
      root_folder: /holiday   # (and sonarr: for series)
    max_adds: 10       # with --apply --yes: more titles than this, and none are added
    scheduled: true    # included in `titularr run --scheduled` (default: false)
```

Unknown keys are reported as errors, so typos don't go unnoticed.

**Radarr and Sonarr are optional.** With them configured, previews leave out titles you already have (shown as `in library` with `--show-dropped`). Titularr only changes them when you use `--apply`. Find each API key under Settings → General → Security. If a `radarr:` or `sonarr:` section is present but can't be used (say, its API key variable isn't set), you get a warning and that library check is skipped. Series that Sonarr has no TMDB ID for can't be recognized, and a warning lists them.

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

# Add the kept titles to Radarr and Sonarr, after a preview and a yes/no question
uv run titularr run holiday-movies --apply

# See what past --apply runs added, then the titles of run 3
uv run titularr history
uv run titularr history 3

# Run every saved query that has `scheduled: true`, adding without asking
uv run titularr run --scheduled --apply --yes
```

**Every command is a preview** unless you add `--apply`, so a forgotten flag can never bulk-add your library. With `--apply`, Titularr shows the preview, then asks one question before adding the kept movies to Radarr and the kept series to Sonarr. `--yes` skips the question, for scripts. Without `--yes`, `--apply` refuses to run where nobody can answer. Because nobody reviews a `--yes` run, it has an add limit: if more than 10 titles would be added, none are, the run lists them and exits with an error. Set a saved query's own limit with `max_adds:`, or use `--max-adds` with `search`. Runs that ask first have no limit. Your quality profiles and root folders are checked against each app before searching. If one title fails to add, the rest are still added, and the failures are listed.

Sonarr adds series by their TVDB ID, so each series is looked up in Sonarr by its TMDB ID first. A series Sonarr can't find (usually one with no TVDB entry) is listed as `not found in Sonarr` and counted separately. It isn't an error, since running again won't change it.

**Tags** (`tags: [anime]` in the `radarr:` or `sonarr:` section, or in a query's own block) are added to every title. In Radarr and Sonarr, a title's tags decide which release profiles (required and ignored words), indexers, download clients and delay profiles apply to it, so they change what gets grabbed. That's why each tag must already exist in the app. A missing one stops the run before searching and lists the app's tags, and Titularr never creates tags. The question before adding names the tags, and a query's tags replace the section's (`tags: []` means none for that query).

**History.** Every `--apply` run is recorded in a SQLite file, `titularr.db` next to your config file unless `history:` names another path (relative paths are from the config file's folder). It holds each run's time and query, and each title's result: added, already there, not found, or failed (with why). Each result is saved as soon as it happens, so a run that's stopped halfway still shows what it did. Previews, and runs where you answer "no", aren't recorded. `titularr history` lists recent runs, and `titularr history <run>` lists one run's titles. It only reads the history file, so the token and API key variables don't need to be set.

**Running on a schedule.** Titularr doesn't stay running or keep its own timer. Instead, mark the saved queries you want with `scheduled: true`, and have a scheduler (cron, a systemd timer, Airflow) run `titularr run --scheduled --apply --yes`. That runs each scheduled query in the order they appear in the config, each under a `== name ==` heading and recorded as its own run in the history. If one query fails (a search error, a failed add, more titles than its `max_adds`), the rest still run. The run ends with a line like `Scheduled queries: 3 run, 1 failed`, and exits with status 1 if any failed, so the scheduler can flag it. Without `--apply`, it previews every scheduled query.

## HTTP API

`titularr serve` starts a read-only HTTP API: list the saved queries, preview one, and read the run history, without a terminal. Nothing it does changes Radarr or Sonarr; adding still happens with `--apply` (or your scheduler).

```bash
# Clients must send this key in the X-Api-Key header; serve won't start without it
export TITULARR_API_KEY=pick-a-long-random-string
uv run titularr serve                    # http://127.0.0.1:8778
uv run titularr serve --host 0.0.0.0     # reachable from other machines too
```

| Route | What it returns |
| --- | --- |
| `GET /api/health` | `{"status": "ok"}`; needs no key |
| `GET /api/queries` | The saved queries and their settings |
| `GET /api/queries/{name}/preview` | Every title found, kept or dropped with the reasons, plus counts and warnings |
| `GET /api/history?limit=20` | Recent `--apply` runs, newest first |
| `GET /api/history/{run_id}` | One run and what it did with each title |

Interactive docs are at `/docs`: click **Authorize**, enter the key, and try each route. The config file is read on each request, so edits show up without a restart (it's also checked when `serve` starts). If TMDB fails part-way through a preview, the response still has what was found so far, with `error` saying what went wrong. If Radarr or Sonarr can't be reached, the preview fails with status 502.

### Running the API in Docker

Each push to `main` publishes an image to `ghcr.io/kyletsavage/titularr`, tagged `latest` and `sha-<commit>` (pin a `sha-` tag to stay on, or go back to, one build). The image is amd64 only, and the package is private. To pull it, log in once on the server with a GitHub personal access token (classic) that has only the `read:packages` scope. GHCR doesn't accept fine-grained tokens.

```bash
docker login ghcr.io -u <your-github-username>   # paste the token as the password
```

A `compose.yaml`, with a `.env` file next to it holding `TITULARR_API_KEY` and whatever variables your config uses (here `TMDB_API_TOKEN`, `RADARR_API_KEY` and `SONARR_API_KEY`):

```yaml
services:
  titularr:
    image: ghcr.io/kyletsavage/titularr:latest
    restart: unless-stopped
    ports:
      - "8778:8778"
    env_file: .env
    volumes:
      - ./config:/config:ro   # config.yaml, and titularr.db (the run history) if you have one
    extra_hosts:
      - "host.docker.internal:host-gateway"   # if your config reaches Radarr/Sonarr on the host this way
```

```bash
docker compose up -d     # start; `docker compose ps` shows it as healthy once the API answers
docker compose pull && docker compose up -d   # update to the newest image
```

Some details:
- **The container runs as uid 1000**, so it must be able to read the mounted files. Use `user:` in the compose file if your files belong to another user.
- **The read-only mount is enough** for the API, since it only reads the config and the history.
- **The image includes the whole CLI.** For example, `docker compose run --rm titularr validate` checks the mounted config.

## A word of caution

Broad keywords can match a *lot* of titles. Review the preview first, use filters and exclusions to keep results relevant, and leave `search_on_add` off (the default) until you've reviewed what landed in Radarr and Sonarr. Your indexers and your disks will thank you.

## Roadmap

- [x] Project scaffolding
- [x] TMDB movie search (all result pages)
- [x] TMDB series search
- [x] Title matching (substring, word, regex)
- [x] Filters (year, language, rating, votes)
- [x] Runtime filter (movies; a lookup per title)
- [x] Exclusions per run (TMDB IDs, title patterns)
- [x] Config file and preview output (`titularr search` / `run` / `validate`)
- [x] Radarr and Sonarr library awareness (skip titles you already have, read-only)
- [x] Run history (SQLite) and `titularr history`
- [x] Adding movies to Radarr with `--apply`
- [x] Adding series to Sonarr with `--apply` (including the TVDB ID lookup Sonarr needs)
- [x] Tags on added titles (existing tags only)
- [ ] More metadata sources (optional, e.g. Trakt, TVmaze, TVDB)
- [x] Saved queries, and `run --scheduled` for any scheduler (cron, Airflow)
- [x] Read-only HTTP API (`titularr serve`)
- [x] Docker image for running the API as a service (published to GHCR)
- [ ] Beyond-title search: collections, keywords, characters (e.g. every Batman movie)
- [ ] Radarr/Sonarr lookup as a source
- [ ] Optional web UI (including manual review and an exclusion blacklist)

## Contributing

The project is just getting started. Issues with ideas, use cases, and feature requests are welcome.

## License

GPL-3.0. See [LICENSE](LICENSE).
