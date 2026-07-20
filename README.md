# planning-center-songs

Pulls the song list + lyrics out of a Planning Center Services plan. The
main thing here is a **self-hosted static lyrics site with a containerized
admin/web deployment** (`static-site/`); a Notion-export script
(`notion-export/`) and an untested live remote/wall-display proof of concept
(`experimental/`) are also included.

In a hurry? See [QUICKSTART.md](QUICKSTART.md) to get the containerized site
running. This file has the full picture, including the other two tools.

## Repo layout

```
src/pco_client/       Shared Planning Center API client (used by everything below)
static-site/          Main feature: static HTML site + admin/web containers + compose
notion-export/        Notion-ready Markdown export (the original script)
experimental/         Live remote/wall display -- untested proof of concept
```

## Setup (all tools)

This project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

### 1. Get a Planning Center Personal Access Token

1. Log in to Planning Center and go to
   https://api.planningcenteronline.com/oauth/applications
2. Under **Personal Access Tokens**, click **New Personal Access Token**.
3. Give it a name (e.g. "lyrics export script").
4. Copy the **Application ID** and **Secret** it generates -- the secret is
   only shown once.
5. The account generating the token needs at least viewer access to the
   Services product/team the plan lives in.

### 2. Configure `.env`

```bash
cp .env.example .env
```

Fill in:

```
PLANNING_CENTER_APP_ID=...
PLANNING_CENTER_SECRET=...
```

`PLANNING_CENTER_SERVICE_TYPE_ID` and `PAGE_TITLE_PREFIX` are optional --
see comments in `.env.example`. Run `uv run static-site/generate_static_site.py
--list-service-types` to find a service type's id if you want to pin one.
`.env` lives at the repo root and is shared by all three tools below.

## Self-hosted static site (main feature)

`static-site/generate_static_site.py` fetches a plan's songs and renders
them as one self-contained static HTML page (no external assets/JS, no
build step):

```bash
uv run static-site/generate_static_site.py                       # nearest upcoming plan -> site/index.html
uv run static-site/generate_static_site.py --include-chords -o site/index.html
```

It always fetches plain lyrics unless `--include-chords` is given, and never
includes PDF chord-chart links -- those are short-lived signed URLs, too
fragile for a page that's regenerated once and left up all day. Its flags
otherwise mirror `update_lyrics.py`'s (`--date`, `--plan-id`,
`--service-type-id`, `--title-prefix`, `--list-service-types`).

Each song is a native `<details>`/`<summary>` toggle -- collapsed to just
the title (+ CCLI number) by default, with the browser's built-in
disclosure arrow to expand it. No JS needed, so the page stays
self-contained.

You can run it standalone (via a systemd timer / cron job, serving `site/`
with any static-file web server), or use the containerized setup below,
which adds the ability to take the site down between services.

### Containerized deployment (podman)

Everything for this lives in `static-site/`: `Dockerfile.admin` +
`Dockerfile.web` + `nginx.conf` + `compose.yaml` package
`generate_static_site.py` as two containers sharing a volume:

- **`web`** -- a plain nginx:alpine container that serves whatever's in the
  shared volume's `current/index.html`. This is what's actually reachable
  from the internet (e.g. as texter.brokyrkan.nu).
- **`admin`** -- a small Flask app (`admin_app.py`) with three buttons:
  regenerate the site from Planning Center, open it (copy the generated
  page into `current/`), or close it (replace `current/` with a "come back
  Sunday" placeholder). Gated by HTTP Basic Auth (`ADMIN_USERNAME` /
  `ADMIN_PASSWORD` -- see `.env.example`; it refuses to start without a
  password set). Basic Auth isn't encrypted on its own, so still keep this
  behind TLS (a reverse proxy) rather than exposing it directly.

This exists because song lyrics are CCLI-licensed for the service they're
used in, not for being publicly readable all week -- **the site defaults to
closed** (both on first run and on every container restart) until someone
explicitly opens it from the admin UI.

```bash
cp .env.example .env   # from repo root -- fill in Planning Center credentials + ADMIN_PASSWORD
cd static-site
podman compose up --build -d
```

The build context is the repo root (both Dockerfiles need `pyproject.toml`/
`uv.lock`/`src/` from up there), which `static-site/compose.yaml` already
points at -- just run `podman compose` from inside `static-site/`.

Then visit the admin UI (`http://<host>:9000/`, log in with `ADMIN_USERNAME`
/ `ADMIN_PASSWORD`) to regenerate + open the site for the service, and close
it again afterwards. The generated site itself is served at
`http://<host>:8080/`; put a reverse proxy in front of that port for a real
domain + TLS.

If your `podman` doesn't bundle a compose provider, install
[`podman-compose`](https://github.com/containers/podman-compose) instead --
`podman compose` transparently shells out to it. Plain `docker compose`
works the same way if that's what you have instead.

There's no scheduling here yet (regenerating and opening/closing are both
manual) -- see the module docstring in `admin_app.py` if you want to extend
it.

## Notion export

`notion-export/update_lyrics.py` is the original script: it exports a
plan's songs + lyrics into a single Markdown file, formatted so you can
paste it straight into a Notion page (Notion auto-converts pasted Markdown
headings/dividers into blocks). There's no Notion API integration here on
purpose -- upload is a manual copy/paste step.

```bash
# Nearest upcoming plan, plain lyrics only
uv run notion-export/update_lyrics.py

# A specific date
uv run notion-export/update_lyrics.py --date 2024-08-04

# A specific plan by id (skips date lookup)
uv run notion-export/update_lyrics.py --plan-id 12345 --service-type-id 6789

# Include chord charts instead of plain lyrics, custom output path
uv run notion-export/update_lyrics.py --include-chords -o lovsang.md

# List service types (to find --service-type-id)
uv run notion-export/update_lyrics.py --list-service-types

# Verbose logging for troubleshooting
uv run notion-export/update_lyrics.py -v
```

Output is a Markdown file named `<PAGE_TITLE_PREFIX> - <date>.md` by default
(e.g. `Lovsång Brokyrkan - 2024-08-04.md`), with:

- `# <prefix> - <date>` as the page title
- `## <song title>` for each song
- the song's CCLI number (if present) in italics
- the lyrics (or chord chart, with `--include-chords`) below it
- a `---` divider between songs

Open (or create) the destination page in Notion and paste the file's
contents directly into the page body. Notion recognizes Markdown on paste
and converts `#`/`##` into headings and `---` into a divider automatically.

## Experimental: live remote + wall display

**Untested proof of concept -- this has not actually been run against a
real plan/device pair yet.** Included for reference; expect rough edges,
and don't rely on it for a live service without trying it first.

`experimental/remote_display.py` is a small local Flask app that lets a
worship leader flip through a plan's songs from one device (phone/tablet)
while a second device (e.g. a wall projector's browser) shows the current
song's lyrics full-screen.

```bash
uv run experimental/remote_display.py                       # nearest upcoming plan
uv run experimental/remote_display.py --date 2024-08-04
uv run experimental/remote_display.py --plan-id 12345 --service-type-id 6789
uv run experimental/remote_display.py --port 8000
```

It prints two URLs to visit from devices on the same local network:

- `/remote` -- the leader's controller (Prev/Next buttons, tap-to-jump list)
- `/display` -- the wall-facing display (large centered lyrics, no chords)

Both pages poll a small JSON endpoint every couple of seconds; there's no
websocket/push machinery, no database, and no dependency on Planning Center
Music Stand's own internal "Sessions" feature -- just the documented public
Planning Center API. State lives in memory only and resets when the process
restarts. No container packaging exists for this yet.

## Notes / things to double-check

- **Which arrangement is used**: a song can have several arrangements
  (e.g. "Acoustic", "Full band"). The script uses whichever arrangement the
  plan item points to; if a plan item doesn't specify one, it falls back to
  the song's first listed arrangement and logs which one it picked. Run
  with `-v` to see this.
- **Plain lyrics vs. chords**: Planning Center stores plain lyrics and the
  chord chart as separate fields on an arrangement. `--include-chords`
  switches to the chord chart field; without it, the plain lyrics field is
  used. If a song has no plain lyrics entered in Planning Center, the
  chord chart is not automatically substituted (and vice versa).
- **No lyrics found**: if neither field is populated for a song in
  Planning Center, the script writes a placeholder note instead of failing
  the whole run.
- Generated `.md` exports and `.env` are gitignored since they contain
  copyrighted lyrics / secrets -- don't commit them.
- **Song lyrics are copyrighted.** This tool only reproduces lyrics your
  Planning Center account already has access to, but *displaying or
  distributing* them (Notion page, wall projector, public static site, etc.)
  is your responsibility to license, typically via a
  [CCLI](https://us.ccli.com/) license -- hence why each song's CCLI number
  is included in the output.

## License

MIT -- see [LICENSE](LICENSE).
