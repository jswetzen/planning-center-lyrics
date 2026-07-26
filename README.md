# planning-center-songs

Pulls the song list + lyrics out of a Planning Center Services plan. The
main thing here is a **self-hosted static lyrics site with a containerized
admin+web deployment** (`static-site/`), which also hosts a **live projector
+ remote synced to Planning Center's Services LIVE session**. A Notion-export
script (`notion-export/`) and an older standalone remote/wall-display proof of
concept (`experimental/`) are also included.

In a hurry? See [QUICKSTART.md](QUICKSTART.md) to get the containerized site
running. This file has the full picture, including the other two tools.

> [!WARNING]
> **This is vibe-coded and alpha quality.** 100% of the code was written by
> Claude Code; I've only clicked through the admin UI myself (seems
> functional) and haven't exercised the scheduled auto-open/close
> automation against a real service yet. I have not done a real security
> review -- including of how the Planning Center access token is stored and
> handled -- and haven't otherwise vetted it for fitness for any particular
> purpose. There's unit test coverage for the scheduler/state-machine logic
> (`tests/`, run in CI), but no integration tests against the real Planning
> Center API and no coverage at all for the Notion-export or experimental
> tools. Anyone looking closely at the code will likely spot rough edges
> quickly. Use at your own risk; read the code before trusting it with your
> own Planning Center credentials or deploying it publicly.

## Repo layout

```
src/pco_client/       Shared Planning Center API client (used by everything below)
static-site/          Main feature: static HTML site + admin/web Flask app + compose
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

Everything for this lives in `static-site/`: `Dockerfile` + `compose.yaml`
package `generate_static_site.py` and `admin_app.py` as a single Flask app
serving two route groups:

- **`/`** -- public, unauthenticated. Serves whatever `current/index.html`
  currently holds -- the generated lyrics page, or a "come back Sunday"
  placeholder. This is what's actually reachable from the internet (e.g. as
  texter.brokyrkan.nu).
- **`/admin`** -- three buttons: regenerate the site from Planning Center,
  open it (copy the generated page into `current/`), or close it (replace
  `current/` with the placeholder). Gated by HTTP Basic Auth
  (`ADMIN_USERNAME` / `ADMIN_PASSWORD` -- see `.env.example`; the app
  refuses to start without a password set). Basic Auth isn't encrypted on
  its own, so still keep this behind TLS (a reverse proxy) rather than
  exposing it directly.

This exists because song lyrics are CCLI-licensed for the service they're
used in, not for being publicly readable all week -- **the site defaults to
closed** (both on first run and on every container restart) until someone
explicitly opens it from the admin UI.

```bash
cp .env.example .env   # from repo root -- fill in Planning Center credentials + ADMIN_PASSWORD
cd static-site
podman compose pull && podman compose up -d
```

`compose.yaml` pulls the prebuilt `ghcr.io/jswetzen/planning-center-lyrics:main`
image -- the same one `.github/workflows/docker-build.yml` publishes on every
push to main -- rather than building locally, so `podman compose pull`
always fetches the latest `main` before starting. If you're iterating on the
Dockerfile or app code itself, see the comment at the top of `compose.yaml`
for how to point it at a local build instead.

Then visit the admin UI (`http://<host>:9000/admin`, log in with
`ADMIN_USERNAME` / `ADMIN_PASSWORD`) to regenerate + open the site for the
service, and close it again afterwards. The generated site itself is served
at `http://<host>:9000/`; put a reverse proxy in front of that port for a
real domain + TLS.

### Automation (scheduled open/close)

The admin UI's "Manage rules" screen lets you configure automation rules --
one per Planning Center service type -- so the site opens and closes itself
instead of requiring a manual click for every service. A background
scheduler (no cron/systemd timer needed; it runs as a thread inside the
same process) re-checks enabled rules every `SCHEDULER_POLL_SECONDS`
(default 300).

For each enabled rule, at every check the scheduler looks up *today's* plan
for that service type and only acts if it looks real enough to trust: the
plan must exist, have at least one song, and have a scheduled time whose
duration isn't a degenerate placeholder (Planning Center accounts can and do
accumulate these -- exact 0-duration entries have been observed in the
wild). If any of that fails, the site is left exactly as it was and the
reason is recorded on the rule, visible on the settings screen. When it
passes, the site opens automatically at the plan's scheduled start time and
closes at its scheduled end time.

Manual actions (the three buttons above) always take priority -- automation
will never re-open something a human just closed, or auto-close something a
human opened manually. It only ever opens when the site is closed with
nothing already live, and only auto-closes a window it opened itself.

`SCHEDULER_TIMEZONE` (default `Europe/Stockholm`) controls what "today"
means for this comparison; set it to your church's local timezone, since
Planning Center's API returns times in UTC.

### Live projection (admin UI)

The admin UI's **Live projection** screen turns the same container into a
projector + remote for an actual service, synced to Planning Center's own
**Services LIVE** session. Pick a service type and a plan, start a session,
and you get two URLs:

- **the projector** (`/project/<token>`) -- full-screen lyrics for whatever
  item Services LIVE currently has open. No password: it authenticates with
  a long random token in its own URL, rotatable from the admin screen. It is
  read-only, so a leaked link can't control anything.
- **the remote** (`/remote`) -- Prev/Next, tap-a-song-to-jump, and a
  black-on-white / white-on-black toggle for the projector. Gated by its own
  `REMOTE_USERNAME`/`REMOTE_PASSWORD` (see `.env.example`), **separate from
  the admin password** -- the person running the service doesn't need the
  credential that can publish lyrics publicly.

Three credentials, three different jobs:

| Route | Who gets in | Can it write? |
|---|---|---|
| `/` | anyone | no |
| `/project/<token>` | anyone with the link | no -- read-only by construction |
| `/remote` | `REMOTE_PASSWORD` (or `ADMIN_PASSWORD`) | drives the plan, only in control mode |
| `/admin/*` | `ADMIN_PASSWORD` only | everything |

The admin password also opens `/remote` — browsers cache Basic Auth per host, so a browser
already logged into `/admin` would otherwise hit a login box it could never satisfy. The
direction that matters is still enforced: `REMOTE_PASSWORD` cannot reach `/admin`.

#### Follow mode vs. control mode

A session always starts in **follow mode**: the app only *reads* Planning
Center and mirrors it onto the projector. Whoever is running Services LIVE
from their own iPad stays in charge and never notices the projector exists.

**Control mode** is entered only by an explicit, confirmed click.

> [!WARNING]
> Planning Center allows exactly **one controller per plan**. Taking control
> disconnects whoever currently has it, with no warning on their end. Don't
> click "Take control" unless you're the one running the service. Stopping
> the session releases control automatically, so it isn't left parked on a
> projector after everyone's gone home.

When a *non-song* item is live -- sermon, offering, announcements -- the
projector goes blank rather than leaving the last song's lyrics up. If
Planning Center becomes unreachable mid-service, the display holds its last
frame instead of blanking, and the remote shows why.

> [!NOTE]
> The Services LIVE integration **has been exercised end-to-end against a
> real plan** (2026-07-26): taking control, next/previous, tap-to-jump, the
> theme toggle, and releasing control all work, and the projector correctly
> resolved live items to lyrics. What has *not* been tried is a real service
> in progress with a leader controlling from their own device -- so the
> contention path (following someone else, then taking control away from
> them) is still only covered by unit tests. Try that on a plan nobody
> depends on before relying on it.

If your `podman` doesn't bundle a compose provider, install
[`podman-compose`](https://github.com/containers/podman-compose) instead --
`podman compose` transparently shells out to it. Plain `docker compose`
works the same way if that's what you have instead.

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

## Experimental: standalone live remote + wall display

> [!NOTE]
> **Superseded by the admin UI's "Live projection" screen** (see above),
> which does the same job inside the deployed container, syncs to Planning
> Center's own Services LIVE session instead of keeping a private index, and
> has real authentication. This standalone script is kept for the case where
> you want a projector on a laptop with no container, no admin password, and
> no Planning Center LIVE involvement at all.

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
