# planning-center-songs

Exports the song list + lyrics from a Planning Center Services plan into a
single Markdown file, formatted so you can paste it straight into a Notion
page (Notion auto-converts pasted Markdown headings/dividers into blocks).

There's no Notion API integration here on purpose -- upload is a manual
copy/paste step.

## Setup

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
see comments in `.env.example`. Run `uv run update_lyrics.py
--list-service-types` to find a service type's id if you want to pin one.

## Usage

```bash
# Nearest upcoming plan, plain lyrics only
uv run update_lyrics.py

# A specific date
uv run update_lyrics.py --date 2024-08-04

# A specific plan by id (skips date lookup)
uv run update_lyrics.py --plan-id 12345 --service-type-id 6789

# Include chord charts instead of plain lyrics, custom output path
uv run update_lyrics.py --include-chords -o lovsang.md

# List service types (to find --service-type-id)
uv run update_lyrics.py --list-service-types

# Verbose logging for troubleshooting
uv run update_lyrics.py -v
```

Output is a Markdown file named `<PAGE_TITLE_PREFIX> - <date>.md` by default
(e.g. `Lovsång Brokyrkan - 2024-08-04.md`), with:

- `# <prefix> - <date>` as the page title
- `## <song title>` for each song
- the song's CCLI number (if present) in italics
- the lyrics (or chord chart, with `--include-chords`) below it
- a `---` divider between songs

### Pasting into Notion

Open (or create) the destination page in Notion and paste the file's
contents directly into the page body. Notion recognizes Markdown on paste
and converts `#`/`##` into headings and `---` into a divider automatically.

## Self-hosted static site (texter.brokyrkan.nu)

`generate_static_site.py` is a sibling script that renders the same song
list as one self-contained static HTML page (no external assets/JS, no
build step) instead of a Notion-ready Markdown file:

```bash
uv run generate_static_site.py                       # nearest upcoming plan -> site/index.html
uv run generate_static_site.py --include-chords -o site/index.html
```

It always fetches plain lyrics unless `--include-chords` is given, and never
includes PDF chord-chart links -- those are short-lived signed URLs, too
fragile for a page that's regenerated once and left up all day. Otherwise
its flags mirror `update_lyrics.py`'s (`--date`, `--plan-id`,
`--service-type-id`, `--title-prefix`, `--list-service-types`).

In production this runs nightly on its own small server (see
`../mikro-iac/apps/texter.nix`), serving the generated `site/` directory at
`texter.brokyrkan.nu` through a static-file container. The site is public
(no login) and regenerates from scratch every night, so there's nothing to
back up -- if the CT is ever rebuilt, the next nightly run repopulates it.

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
