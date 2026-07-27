# Architecture: admin+web site

This covers the internals of the piece described in README.md's "Containerized deployment"
and "Automation" sections — how `admin_app.py`, `scheduler.py`, and `generate_static_site.py`
fit together, and why. It complements README.md (setup/usage) rather than repeating it. For
how this app is actually deployed in production (the real CT, Traefik route, secrets pipeline),
see `mikro-iac/docs/poc-lyrics.md` in the infrastructure repo — this doc stops at the container
boundary.

## Why this exists

Song lyrics are CCLI-licensed for the specific service they're used in, not for being publicly
readable all week. A plain "regenerate nightly, serve forever" static site (the old `texter`
deployment this app replaces) has no way to express that — the previous version of this idea
really did serve copyrighted lyrics publicly 24/7. `admin_app.py` makes "closed by default,
opened deliberately for a service" a first-class state instead of an afterthought: `/` always
reflects exactly what `state.txt`/`current/index.html` say, and only the `/admin` routes (and
the scheduler) can change that.

## One process, four route groups

```mermaid
flowchart LR
    PCO[Planning Center API] -->|fetch songs/lyrics| GEN[generate_static_site.py]
    PCO <-->|Services LIVE: poll + control| LIVER
    subgraph app[admin_app.py -- single container/process :9000]
        GEN
        PUBLIC["/  (public, no auth)"]
        ADMINR["/admin/*  (ADMIN_PASSWORD)"]
        REMOTE["/remote/*  (ADMIN_PASSWORD)"]
        PROJECT["/live  (public, read-only,\ngated on state.txt)"]
        SCHED[scheduler.py<br/>pure logic, no Flask]
        LIVER[live_routes.py]
        LIVES[live_session.py<br/>pure logic, no Flask]
        ADMINR --> GEN
        ADMINR <--> SCHED
        ADMINR --> LIVER
        REMOTE --> LIVER
        PROJECT --> LIVER
        LIVER <--> LIVES
    end
    subgraph data[DATA_DIR volume]
        SITE[site/index.html<br/>+ index.plan.json]
        CURRENT[current/index.html]
        STATE[state.txt]
        OPENPLAN[open_plan.json]
        RULES[rules.json]
        LIVEJSON[live_session.json]
    end
    GEN -->|writes| SITE
    ADMINR -->|copies on Open/regenerate-while-open| CURRENT
    ADMINR --> STATE
    ADMINR --> OPENPLAN
    SCHED --> RULES
    LIVES --> LIVEJSON
    CURRENT -->|read| PUBLIC
    PUBLIC -->|public traffic| Internet
```

**`state.txt` gates both public routes.** `/` serves the whole plan's lyrics while open and a
placeholder while closed; `/live` serves the one currently-live song while open and the same
placeholder while closed. So there is exactly one switch deciding whether copyrighted lyrics are
being served at all, and `/live` can only ever be a subset of what `/` is already serving.

The two features are otherwise independent — starting a projection session doesn't open the
site, and opening the site doesn't start a session — but a session with the site closed
projects nothing, which the admin screen warns about explicitly so a blank projector is never a
mystery.

- **`/`** (no auth): read-only, and the only thing it does is return whatever
  `DATA_DIR/current/index.html` currently holds. It has no logic of its own to distinguish
  "open" from "closed" — both are just different file contents it happens to be serving at the
  time. This asymmetry is deliberate: the CCLI-relevant decision logic lives in exactly one
  place (the `/admin` routes and the scheduler), and the public route can't be tricked into
  bypassing it because it has nothing to bypass.
- **`/admin/*`** (HTTP Basic Auth): the only routes with write access to `DATA_DIR`. Runs
  `generate_static_site.py` as a subprocess, serves the control UI, and — via the background
  scheduler thread the same process hosts — the automation described below.
- Both route groups run in the same Flask app/process and read/write the same `DATA_DIR`
  directly — no shared volume or network call between containers, because there's only one
  container. `/admin` is reachable at the same host/port as `/`; the deployment's Basic Auth
  is what gates it, not network placement (see `mikro-iac/docs/poc-lyrics.md` for how it's
  actually exposed).

**History note:** this used to be two containers (`admin` + a stock `web` static-file server)
communicating only through a shared volume, with `/admin` reachable only on the LAN. It was
collapsed into one process because the admin UI needed to be reachable from outside the LAN,
and the two-container split wasn't buying enough given how low-stakes a CCLI licensing lapse
on this site actually is (see git history for the prior topology if you need it).

## `DATA_DIR` layout

```
DATA_DIR/
├── site/
│   ├── index.html        latest output of generate_static_site.py
│   └── index.plan.json   {service_type_id, plan_id, plan_title} it was generated from
├── current/
│   └── index.html        what the "/" route actually serves (copy of site/ or the
│                          "come back Sunday" placeholder)
├── state.txt             "open" | "closed" (defaults to closed if missing/malformed)
├── open_plan.json        which plan is live right now + who opened it (see OpenPlan below)
├── rules.json            configured automation rules + last-evaluated bookkeeping
└── live_session.json     active projection session, if any (see Live projection below)
```

`site/` and `current/` are intentionally separate: **regenerate always refreshes `site/`**, but
only touches the public-facing `current/` if the site is currently open. This means clicking
"Regenerate now" mid-service to pull in a last-minute Planning Center edit doesn't risk
flashing the placeholder page at anyone mid-song — regeneration and publication are decoupled.

## State machine

Two independent-looking but coupled pieces of state:

1. **`state.txt`**: `open` or `closed`. Determines what `current/index.html` contains.
2. **`open_plan.json`** (`OpenPlan` dataclass, `scheduler.py`): only meaningful when state is
   `open`. Records *which* plan is live and — critically — `opened_by: "manual" | "automation"`.
   Automation windows also carry `window_ends_at`; manual opens don't (a human closes it when
   they close it).

All four mutating routes (`/admin/regenerate`, `/admin/open`, `/admin/close`, and the
scheduler's own tick) go through a single `threading.Lock` (`admin_app._lock`) —
`app.run(threaded=True)` means each HTTP request is its own thread, running concurrently with
the scheduler's background thread, and all of them touch the same on-disk files. The public
`/` route only reads `current/index.html` and isn't part of this locking — a regenerate/open/
close racing a request can at worst serve the version of the file from just before or just
after the write, never a torn one (each write is a single `Path.write_text` call).

**Self-healing**: every scheduler tick starts by checking for a stale `open_plan.json` while
`state.txt` says closed (a crash mid-write, or someone hand-editing `state.txt`) and clears it
before doing anything else — otherwise a stale record could confuse the auto-close timer or the
"who opened this" logic.

## Automation (`scheduler.py`)

Deliberately has **no Flask, HTTP, or threading** in it — pure logic + JSON persistence, so
it's unit-testable without a running server (`tests/test_scheduler.py`). `admin_app.py` owns
the actual background thread (`_scheduler_loop`, sleeps `SCHEDULER_POLL_SECONDS`, default 300)
and the open/close state machine (`_tick`); `scheduler.evaluate_rule` only ever *decides*, never
*acts*.

**The guardrail** (`evaluate_rule` → `_is_sane_window`) exists because real Planning Center data
on this account has produced exact 0-duration placeholder time windows sitting right next to
legitimate ones in the same service type. A rule only fires if:
- today's plan for that service type exists and has at least one song item,
- it has a `plan_times` row (preferring `time_type == "service"`, but falling back to any row
  since this account tags placeholders the same way),
- that window's duration is sane (configurable `min_window_minutes`/`max_window_minutes`,
  defaults 15min–12h — generous on purpose) and starts on the expected local date.

Anything that fails is recorded as `skipped` with the specific reason, visible on
`/admin/settings` — automation never guesses; it either has a plan it trusts or it does nothing.

### Planning Center's plan-time data model, and how to schedule a plan correctly

A Planning Center plan does **not** carry an independent "date" field you set directly. What
looks like a date (`attributes.dates`, e.g. `"23 July 2026"`, or `"No dates"` for a plan with
none) is a *display string computed from the plan's `PlanTime` rows* — Service Time, Rehearsal
Time, or Other Time blocks you add from the plan's "Times" tab in the Services UI. No PlanTime
rows means no date, no matter what the plan's title or position in a series implies.

**To make a plan discoverable by date (and therefore automatable), it needs an actual PlanTime**
with the real start/end on the intended day — not just existing under the right service type.
Creating a plan and leaving its Times tab empty produces a plan `find_plan_by_date` (and the
scheduler built on it) can never correctly place.

**The pitfall this caused (found 2026-07-23, fixed in `pco_client.find_plan_by_date`):** for a
plan with zero PlanTime rows, Planning Center's API doesn't return a null/empty `sort_date` — it
fills in the timestamp of *whenever you happened to call the API*, confirmed by re-fetching the
same plan a few seconds apart and watching `sort_date` tick forward with it. That means a
dateless draft plan left over in a service type spuriously matches **today's date on every single
lookup, every single day, forever** — and if it sorts earlier in the API's response than a real
dated plan for the same day, `find_plan_by_date` (which used to take the first string-prefix
match with no other filtering) would return the ghost plan and never reach the real one. This is
exactly what happened to the "Special Events" service type on this account: it had accumulated
several old dateless plans (leftover drafts/one-offs, e.g. `"Kingdom Intensive Okt 21"`,
`"Weekend25 Fredag Kväll"`), and one of them permanently shadowed a real plan created for
2026-07-23.

The fix: `find_plan_by_date` now skips any plan whose
`service_time_count + rehearsal_time_count + other_time_count == 0` before checking `sort_date`
at all — those three counts are returned directly on the plan resource, so this needs no extra
API call. A plan with no scheduled time literally cannot answer "what's the plan for this date,"
so excluding it is correct, not a workaround for bad data — but it's also why the guardrail in
`evaluate_rule` (above) is deliberately layered on top rather than trusting *any* single field:
Planning Center data on this account has repeatedly turned out to need cross-checking, not blind
trust, at more than one layer.

**Checklist for a plan you expect a rule to pick up:**
1. Plan exists under the rule's configured service type.
2. Plan has at least one song item.
3. Plan's Times tab has a real Service Time (or Rehearsal/Other) block with the correct date and
   a plausible (non-zero, non-multi-day) duration — this is what ends up in `plan_times` /
   `dates` / `sort_date`; skipping this step is the single most common way a plan silently fails
   to be picked up.
4. If it's still not firing, check `/admin/settings` for that rule's `last_action`/`last_reason`
   — every skip is logged there with the specific guardrail that rejected it, so start there
   before reaching for the API directly.

**Priority rule, enforced by `_tick`'s own control flow, not a special case**: manual actions
(the three UI buttons under `/admin`) always win immediately. Automation only ever opens when the site is
closed *and* nothing is already tracked as live; it only ever auto-closes a window it opened
itself (checked via `open_plan.opened_by == "automation"`) — it will never re-open something a
human just closed or auto-close something a human opened manually, because those states simply
never match automation's own preconditions.

## Live projection (`live_session.py` + `live_routes.py`)

A second feature sharing the same process and `DATA_DIR`: a projector and a remote for an
actual service, synced to Planning Center's own **Services LIVE** session (the thing a worship
leader drives from the Services app). Same split as the scheduler — `live_session.py` is pure
logic + JSON persistence with no Flask in it; `live_routes.py` owns the HTTP surface.

```
DATA_DIR/
└── live_session.json    active projection session (plan ref, mode, theme)
```

**Reading what's live is a two-hop indirection.** The `live` resource doesn't report the
current *Item*; it reports a `current_item_time` relationship pointing at an **ItemTime**, and
only the sideloaded ItemTime carries the link back to the plan item. `pco_client.get_live_status`
does that resolution behind `include=current_item_time,next_item_time,controller` and hands
callers plain item ids. This is also why `SongLyrics` gained an `item_id`: `collect_songs`
previously discarded it, and without it there's no way to answer "is this the song on screen".

### `can_control` does not mean "we are controlling" (found 2026-07-26, against the live API)

The `live` resource's `can_control` attribute is a **permission** flag — "this token is allowed
to control" — and it reads `true` even when nobody holds control and no LIVE session exists at
all. It is *not* "we are the controller", which is what the name suggests and what the first
implementation assumed.

The failure that assumption produced was quiet and total: `live_take_control` short-circuited on
`can_control` being already true, so it never POSTed `toggle_control`, so no LIVE session was
ever claimed — and every subsequent action failed with

```
403 Forbidden — User with id … cannot read
    AppGraph::V2018_11_01::Actions::LiveGoToNextItemAction with id nil
```

The `with id nil` is the tell: the action had no live session to act on. Nothing in the 403 says
"you never took control", which is what made it worth writing down.

The reliable signal is the **`controller` relationship compared against the token's own person
id** (`get_my_person_id`, `GET /services/v2/me`, cached on the session since this sits behind a
2-second poll). That's `LiveStatus.holds_control`, and it's what take/release and the UI branch
on. `can_control` is still exposed but only means what it actually means. Confirmed working
end-to-end against a real plan: take → `controller` becomes us → `go_to_next_item` returns 200.

A second-order bug from the same confusion: `live_release_control` also branched on
`can_control`, so pressing "Release" while *someone else* held control would have toggled
control **to** us — the exact opposite of the button's label. It now no-ops unless we actually
hold it.

**Follow vs. control.** A session always starts in `follow` mode, where the app has never
written to Planning Center — it polls and mirrors, and whoever holds control keeps it.
`control` mode is entered only through a confirmed click on `/admin/live`. This matters because
Planning Center allows **exactly one controller per plan**, and `toggle_control` boots the
incumbent with no warning on their end. Nothing takes control implicitly: not a page load, not
a timer, not a Next press (`_require_control` returns 409 in follow mode rather than silently
escalating). Stopping a session releases control, so it isn't left parked on a projector after
the service.

**Why the projector shows a blank rather than the last song**: Services LIVE walks the whole
running order, so the current item is regularly a sermon or an announcement. `resolve_display`
distinguishes four cases — `song`, `hold` (a known non-song item → blank), `waiting` (nothing
live yet), and `stale` (Planning Center unreachable, or on an item this session's cache predates
→ *keep the last frame*, since blanking a projector mid-song is worse than a few seconds of
staleness). All four are unit-tested in `tests/test_live_session.py`.

**Poll cost is fixed, not per-viewer.** The projector polls every 1.5s and the remote every 2s,
and a church may have several of each open. `live_routes.poll_live` caches the Planning Center
read for `LIVE_POLL_CACHE_SECONDS`, so API traffic doesn't scale with the number of open tabs
and can't walk into a rate limit mid-service. The projector's polling path also deliberately
avoids `admin_app._lock`, so it can't block behind a regenerate subprocess.

**Tap-to-jump is a walk, not a seek.** Services LIVE exposes only `go_to_next_item` /
`go_to_previous_item` — there is no absolute "go to item X". `steps_between` computes the delta
across the *full* running order (a song-to-song jump usually crosses non-song items), and
`MAX_JUMP_STEPS` caps how far one tap will walk so a mis-tap can't fire an unbounded burst of
writes.

## Lyric shaping (`pco_client`)

`split_stanzas`, `looks_like_section_label`, `dedupe_stanzas` and `wrap_lines` live in
`pco_client` next to `clean_text`/`SongLyrics`, because the projector and the PowerPoint export
both need the same answer to "what is a screenful". They are **projection-specific** and
deliberately not applied to `/` or the Notion export: those are reading and reference surfaces
where `CHORUS:` is useful navigation and the band wants the structure.

Every threshold below came from a corpus of **19 distinct songs / 511 lines across five
Sundays** on this account, not from guesswork.

### Why the projector needs this at all

Services LIVE reports which *item* is current, never which stanza, so there is nothing to page
through — the whole song shares one screen and the only lever on legibility is line count. (The
whole-song constraint is also a deliberate workflow choice: the musician playing is often the
one running projection, so nobody is free to advance stanzas. Following Music Stand's own
sub-item position would solve it, but that's a private API.)

Measured on the corpus, counting both before and after *after* wrapping at the same width, so
it's a fair comparison — the old page was already being wrapped by the browser, just at
arbitrary points:

| | visual lines |
|---|---|
| before | 579 |
| after | **448** (23% fewer) |

No song came out worse. Section labels reaching the projector went from 3 to **0**, and the
longest line from 76 characters to 42.

### Section labels

The corpus vocabulary is wider than an English-only guess would produce: `REFRÄNG:`, `VERS 1:`,
`STICK:`, `BRYGGA:`, `VAMP:`, `MELLANSPEL:`, `INSTRUMENTALT:`, plus compounds like
`INTRO/INSTRUMENTAL:` — in upper and title case, with and without colons.

`looks_like_section_label` is deliberately conservative: anchored, capped at 24 characters, and
every token must be a known structural word. **A false positive deletes a lyric silently, which
is far worse than leaving a stray `TAG:` on a wall.** The corpus case that justifies the caution
is `(Allt som du har sagt)` — a backing-vocal line a looser matcher would eat.

The known consequence, documented in the tests: a lyric line consisting of exactly one
structural word (`chorus`) is stripped. Requiring punctuation would fix it but would miss the
bare `INTRO` the corpus actually contains, so the trade goes this way.

**Labels also act as stanza breaks.** 3 of 86 labels in the corpus sat mid-block with no blank
line before them; treating them as plain text both left the marker on screen *and* merged two
sections into a single slide in the deck.

### De-duplication — projector only

5 of 19 songs repeat a stanza verbatim, and for those it removes up to 40% of the lines. It runs
for `/live` and **not** for the deck: a deck is advanced slide by slide, so a chorus sung three
times needs three slides, and collapsing them would break the running order mid-service. Exact
matches only after case/whitespace/punctuation folding — near-duplicates are left alone, since
guessing there risks dropping a verse that merely rhymes with another.

### Line breaking

Median longest-line in the corpus is 44 characters, max 76, with 68 lines over the 42-character
threshold. The first implementation preferred punctuation nearest the middle and produced a
**21/53** split on a 75-character line — worse than breaking at the midpoint space. `_best_break`
therefore tries sentence end, then clause punctuation, then any space, but **only accepts a
candidate that leaves both halves at least 35% of the line**. On real lyrics that lands the
break where a singer breathes:

```
Änglar sjung - er ut, he - e - lig. Hela skapel - sen sjunger he - e - lig.
  ->  Änglar sjung - er ut, he - e - lig.        (35)
      Hela skapel - sen sjunger he - e - lig.    (39)
```

A run with nowhere balanced to break is left long rather than mangled.

### What this does not fix

**Vilket underbart namn** is 42 lines even after shaping — roughly 18px on a 1080p screen. No
amount of text shaping makes a song that long readable from the back of a room while it all
shares one screen. The only lever that changes that ceiling without paging is a two-column
layout above a line threshold, and it's a partial win at best: halving the column width forces
more wrapping back.

## PowerPoint export (`pptx_export.py`)

Same shape as the other two logic modules: **no Flask**, takes `SongLyrics` and returns bytes,
so slide-splitting and font-sizing are unit-tested without a server (`tests/test_pptx_export.py`).
`admin_app.py` owns the route (`/admin/export`, `/admin/export/pptx`) and the plan lookup.

**Stateless on purpose.** The export writes nothing to `DATA_DIR` and touches no session, so it
can't interact with the open/closed machinery or a running projection — a test asserts the data
directory is byte-identical before and after an export.

**One slide per stanza.** The unit of projection is a screenful, not a song. Planning Center
stores lyrics as blank-line-separated stanzas, which is that unit already, so the split follows
the document's own structure rather than guessing at one.

**Section labels are stripped**, and this is not cosmetic: real songs on this account start
stanzas with `VERSE 1:`, `CHORUS:`, `TAG:`, `BRIDGE:`. Those are notes for the band, and
projecting them at a congregation is a visible mistake. `looks_like_section_label` is
deliberately conservative — anchored, length-capped (≤24 chars), and only ever applied to the
*first* line of a stanza — so "Bridge over troubled water" stays a lyric. Swedish labels
(`Refräng`, `Vers`, `Brygga`, `Omkväde`) are recognized too, since that's what these songs are
written in.

**Font sizes are estimated, not measured.** PowerPoint lays text out itself using fonts this
code can't see, and python-pptx's `fit_text()` needs font files present at render time —
unreliable in an Alpine container. `choose_font_size` applies a width constraint (longest line)
and a height constraint (line count) and takes the smaller, clamped to 18–54pt. The floor
matters more than the ceiling: below ~18pt the back row is squinting, so an over-long stanza is
better left slightly overflowing — visibly needing a manual split — than silently shrunk to
unreadable.

**`.pptx` only.** Keynote imports it; there is no writable Keynote format (`.key` is an
undocumented macOS-only bundle), so "export to Keynote" is served by handing Keynote a `.pptx`.

The CCLI number goes in the footer of *every* slide rather than on a title slide, because
reporting what was projected is the licence-holder's obligation and a number that only exists in
Planning Center is one nobody will transcribe afterwards.

This adds the repo's first dependency beyond flask/requests/dotenv (`python-pptx`, which pulls
lxml and Pillow). Those need compiling on Alpine/musl; the Dockerfile's builder stage already
installs `gcc`/`musl-dev` for exactly this case, and the image has been confirmed to build and
produce a valid deck.

## Auth

Three credentials with three different jobs, all dispatched from the single `_require_auth`
hook (`@app.before_request`) so every route's answer to "who gets in" is readable in one place:

| Route | Gate | Write access |
|---|---|---|
| `/`, `/healthz` | none | none |
| `/live` | nobody — public, but only serves lyrics while the site is open | **none** — no POST route exists under `/live` |
| `/remote/*` | `ADMIN_PASSWORD`, same realm | drives Planning Center, control mode only |
| `/admin/*` | `ADMIN_PASSWORD` | everything |

### Why `/remote` isn't a second credential (tried and reverted, 2026-07-26)

It was one, on the reasonable-sounding principle that whoever runs a service from the back of
the room shouldn't hold the credential that can publish copyrighted lyrics to the internet.
That principle is still right; HTTP Basic Auth just can't express it on a single origin.

Browsers cache Basic Auth credentials **per origin, not per path or per realm**, and re-send
them preemptively to every path on that host. Once a browser had seen the `remote` realm, it
kept attaching those credentials to `/admin` requests and vice versa. Observed against a real
browser: six consecutive 401s on `/remote` while the user was entering the correct admin
password, because the browser kept overriding it with the cached remote credentials. An
intermediate fix (accept *either* credential on `/remote`) didn't help — the browser was
sending stale credentials, not no credentials.

One credential and one realm (`Basic realm="admin"`) removes the ambiguity outright, which is
the current design. `tests/test_live_routes.py` asserts both routes advertise the *same* realm
string, since that's the thing browsers key their cache on.

Reinstating the privilege split needs **cookie-based login**, where two identities on one origin
work properly — see the discussion of that migration below. The public `/live` route is
unaffected either way: it has no credential to collide with.

### The projector needs no credential (token removed 2026-07-26)

`/live` shows exactly one song — whichever Services LIVE says is current — and only while
`state.txt` says open. At any moment it is serving *less* than `/` is already serving to the
whole internet: same availability window, one song instead of the entire plan. A credential
guarding a strict subset of already-public data guards nothing, so there isn't one. Being public
is also what makes it useful past the projector: the congregation can follow the current song on
their phones from the same URL.

It was originally gated by an unguessable rotatable token, on the reasoning that an unattended
booth browser shouldn't hold a password. That reasoning was sound about *passwords* and wrong
about the threat: the content wasn't secret in the first place, and the token bought a URL
nobody could type or bookmark by hand.

**The part that isn't optional is the gate it hangs on.** Planning Center never clears
`current_item_time` — confirmed 2026-07-26, hours after a service ended, control released and
nobody driving, the live resource still reported the last song, and the projector route was
still serving its lyrics while `/` correctly showed the placeholder. So gating on "a session is
running" would have quietly reintroduced the all-week exposure this whole app exists to prevent.
`state.txt` already answers "may lyrics be served right now", so `/live` reuses it, checked
*before* Planning Center is polled at all (`current_display`) rather than by blanking a response
after the fact.

Credential comparisons all use `secrets.compare_digest` (constant-time,
avoids a timing side-channel on the credential comparison). There is **no rate limiting or lockout** on failed attempts —
acceptable given the password is a long random string (see the mikro-iac deployment's secret
generation), not something meant to be human-memorable, but worth knowing if this ever moves to
a weaker/user-chosen password. The app refuses to even start (`main()`) without
`PLANNING_CENTER_APP_ID`/`SECRET` and `ADMIN_PASSWORD` set — there's no unauthenticated fallback
mode.

Basic Auth itself is not encrypted; the app relies entirely on TLS being terminated in front of
it (a reverse proxy — Traefik in the mikro-iac deployment). It is not safe to expose this
directly over plain HTTP. Since `/admin` is now reachable at the same public host/port as `/`
(see the history note above), Basic Auth is the *only* gate on it — there's no network-level
restriction (LAN-only, IP allowlist) backing it up unless the deployment adds one at the reverse
proxy. That's an accepted tradeoff here: the content this whole app protects is CCLI-licensed
lyrics, not sensitive data, so the admin UI needing to be reachable from outside the LAN mattered
more than keeping a second layer of network-level isolation on top of the password.

### Known gap: no CSRF protection, and why that's tied to Basic Auth

There are 17 state-changing `POST` routes (open/close the public site, start/stop a projection
session, take Planning Center control, edit automation rules) and **no CSRF tokens**. Basic Auth
provides no defence here: browsers attach cached credentials to any request aimed at that origin,
including a cross-site form POST from an attacker's page. An admin who is logged in and then
visits a hostile page could have the public site toggled, a session stopped, or LIVE control
taken mid-service. The app is internet-exposed (Traefik), so this isn't purely theoretical —
it's accepted for now because the blast radius is a lyrics site, not because it's safe.

This can't be fixed cleanly *without* moving off Basic Auth: a CSRF token has to be bound to a
session, and Basic Auth has no session to bind to. So the natural fix is a **cookie-based
login**, which would land three things at once:

1. `SameSite=Lax` session cookies, which browsers simply don't send on cross-site POSTs —
   removing the whole class — plus per-form tokens for defence in depth.
2. A **logout**, which Basic Auth cannot offer. This matters most for the remote: it's meant to
   run on a volunteer's phone, which currently keeps the credential indefinitely with no
   revocation short of changing the password for everyone.
3. The `/admin` vs `/remote` privilege split, which cookies can express on one origin and Basic
   Auth cannot (see above).

The cost is real: a `SECRET_KEY` that must survive restarts (mint it into `DATA_DIR` alongside
`live_session.json`, or every container restart logs everyone out), plus cookies and login/logout
routes in an app that currently holds no client-side state at all. The two roles also want
different session lifetimes — weeks for a phone used every Sunday, short for admin.

## `pco_client` (`src/pco_client/`)

Thin wrapper around the Planning Center Services REST API shared by every tool in this repo
(this admin app, the Notion export, the experimental remote display). Handles Basic Auth session
setup, pagination (`api_get_all_pages`), and plan/song/arrangement lookups. `scheduler.py` uses
`find_plan_by_date` + `get_plan_items` + `get_plan_times`; `generate_static_site.py` uses
`find_upcoming_plan`/`find_plan_by_date` + `collect_songs` for the actual lyrics rendering;
`live_routes.py` uses `get_live_status` + `live_take_control`/`live_go_to_*` and
`get_plan_item_summaries`. Nothing here is admin/web-app-specific — it has no knowledge of
`DATA_DIR`, auth, or the open/closed concept.

`get_live_status` never raises: it's on a path polled every couple of seconds by a projector
mid-service, so a transient failure returns `reachable=False` for the caller to hold its last
frame, rather than an exception that would blank a screen. `live_take_control` /
`live_release_control` read state before acting, because Planning Center only offers a raw
`toggle_control` — a blind second call would hand control straight back.

## CI / build pipeline (`.github/workflows/docker-build.yml`)

Three jobs on every push/PR to `main`:

- **`build`**: builds `static-site/Dockerfile` (confirms it still builds after a
  Dockerfile/dependency change) — not pushed anywhere by this job.
- **`test`**: `uv run pytest` — the PowerPoint export (`tests/test_pptx_export.py`), the
  scheduler/state-machine logic
  (`tests/test_scheduler.py`, `tests/test_admin_state_machine.py`), the Planning Center client
  including the Services LIVE response shapes (`tests/test_pco_client.py`), and the live
  projection logic and route/auth boundaries (`tests/test_live_session.py`,
  `tests/test_live_routes.py`).
- **`publish`**: **push events to `main` only**, gated on `build`+`test` both passing. Builds
  and pushes the image to `ghcr.io/jswetzen/planning-center-lyrics`, tagged `:main` (moving) and
  `:sha-<commit>` (pinned).

This is the only path production images reach GHCR — there's no manual `docker push` step
documented or expected.
