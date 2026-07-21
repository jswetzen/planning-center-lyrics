# Quickstart

Get the containerized static lyrics site running. This covers only the main
feature (`static-site/`); see [README.md](README.md) for the Notion export
script and the experimental live-display POC.

## 1. Get a Planning Center Personal Access Token

1. Log in to Planning Center and go to
   https://api.planningcenteronline.com/oauth/applications
2. Under **Personal Access Tokens**, click **New Personal Access Token**.
3. Copy the **Application ID** and **Secret** -- the secret is only shown
   once.

## 2. Configure

From the repo root:

```bash
cp .env.example .env
```

Edit `.env` and fill in at least:

```
PLANNING_CENTER_APP_ID=...
PLANNING_CENTER_SECRET=...
ADMIN_PASSWORD=...
```

`ADMIN_PASSWORD` gates the admin UI below -- pick something real, the
container refuses to start without it.

## 3. Build and start the containers

```bash
cd static-site
podman compose up --build -d
```

(No podman-compose bundled? `podman-compose up --build -d` works too. Plain
`docker compose` also works if that's what you have.)

This starts a single container listening on port `9000`, serving:

- `/` -- the actual public-facing site (starts closed)
- `/admin` -- the control panel, gated by HTTP Basic Auth

## 4. Generate + open the site

Visit `http://<host>:9000/admin`, log in with `admin` / your
`ADMIN_PASSWORD`, then:

1. Click **Regenerate now** -- fetches the nearest upcoming plan's songs
   from Planning Center.
2. Click **Open (serve lyrics)** -- makes the generated page live at
   `http://<host>:9000/`.

## 5. Close it after the service

Lyrics are CCLI-licensed for the service they're used in, not for being
public all week. Click **Close (serve placeholder)** in the admin UI when
you're done -- and remember the site also defaults to closed on every
container restart, so this isn't optional cleanup, it's the normal flow.

## Going further

- Put a real reverse proxy (TLS) in front of port `9000` for a public
  domain -- both `/` and `/admin` are served from the same port, and Basic
  Auth on `/admin` isn't encrypted on its own, so it relies entirely on
  that TLS termination rather than network isolation.
- Don't want to click regenerate/open/close every service? The admin UI's
  "Manage rules" screen can automate it per service type -- see README.md's
  "Automation" section.
- Full details, the Notion export tool, and the experimental live display
  are all in [README.md](README.md).
