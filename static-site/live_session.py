#!/usr/bin/env python3
"""
live_session.py

State + pure logic behind the live projection feature: which plan is being
projected, what the projector should have on screen at this instant, the
display's access token, and the light/dark theme.

Like scheduler.py, this module deliberately has **no Flask, HTTP, or
threading** in it -- just dataclasses, JSON persistence, and one decision
function (`resolve_display`) that turns "what Planning Center says is live"
into "what the projector shows". live_routes.py owns the HTTP surface and the
polling; this module owns the rules, so they can be tested without a server
or network (tests/test_live_session.py).

Persisted on the same DATA_DIR volume admin_app.py already uses:
    <DATA_DIR>/live_session.json   the active projection session, if any
    <DATA_DIR>/display_token.txt   the projector's bearer token (see below)

## Why the display gets a token instead of a password

The projector browser is unattended, usually on a machine in an AV booth
that several people can walk up to, and there's no practical way to "log it
out" after a service. Giving it the admin password would mean the booth
machine holds the credential that controls whether copyrighted lyrics are
served to the public internet. Instead it gets a long random token embedded
in its URL, which:

  - is read-only by construction -- every route reachable with it is a GET
    that renders or reports state, so a compromised display can't advance
    the plan, take Planning Center control, or open the public site;
  - can be rotated from the admin screen the moment a laptop goes missing,
    without touching anyone else's access;
  - survives being bookmarked on a device with no keyboard.

The tradeoff, worth being explicit about: a URL token lands in browser
history and any reverse-proxy access log that records query-free paths. That
is an accepted risk for a page whose entire content is song lyrics the
congregation is looking at anyway -- it is *not* a pattern to copy for the
admin routes, which stay behind a password.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional

from pco_client import LiveStatus, PlanItem, SongLyrics

log = logging.getLogger("live_session")

# Long enough that online guessing is hopeless even with no rate limiting in
# front of it (the app deliberately has none -- see ARCHITECTURE.md "Auth").
DISPLAY_TOKEN_BYTES = 32

Theme = Literal["dark", "light"]
Mode = Literal["follow", "control"]

# White-on-black is the default because that's what a darkened room wants and
# what the previous proof of concept did; "light" exists for bright rooms and
# for projectors whose blacks wash out to grey anyway.
DEFAULT_THEME: Theme = "dark"


# --------------------------------------------------------------------------
# Display token (persisted at <DATA_DIR>/display_token.txt)
# --------------------------------------------------------------------------


def _token_path(data_dir: Path) -> Path:
    return data_dir / "display_token.txt"


def read_display_token(data_dir: Path) -> Optional[str]:
    path = _token_path(data_dir)
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def ensure_display_token(data_dir: Path) -> str:
    """Return the display token, minting one on first use."""
    existing = read_display_token(data_dir)
    if existing:
        return existing
    return rotate_display_token(data_dir)


def rotate_display_token(data_dir: Path) -> str:
    """Mint a fresh display token, invalidating every existing display URL."""
    token = secrets.token_urlsafe(DISPLAY_TOKEN_BYTES)
    path = _token_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".txt.tmp")
    tmp_path.write_text(token, encoding="utf-8")
    os.replace(tmp_path, path)
    return token


def token_matches(data_dir: Path, candidate: str) -> bool:
    """Constant-time comparison of a presented token against the stored one.

    Mirrors the constant-time credential check admin_app._require_auth already
    uses -- same reasoning, same lack of rate limiting behind it.
    """
    stored = read_display_token(data_dir)
    if not stored or not candidate:
        return False
    return secrets.compare_digest(stored, candidate)


# --------------------------------------------------------------------------
# The active session (persisted at <DATA_DIR>/live_session.json)
# --------------------------------------------------------------------------


@dataclass
class LiveSessionState:
    """Which plan is being projected right now, and how.

    Only the plan reference and the operator's choices live here -- the songs
    and running order are cached separately in memory (PlanCache) because
    they're re-fetchable from Planning Center at any time and would bloat a
    file that gets rewritten on every theme toggle.

    `mode` is the safety-relevant field: "follow" means this app has never
    written to Planning Center and the projector is a passive mirror of
    whatever device actually holds control. "control" means an operator
    deliberately took control through the admin screen, displacing whoever
    had it.
    """

    service_type_id: str
    plan_id: str
    plan_title: str
    started_at: str
    mode: Mode = "follow"
    theme: Theme = DEFAULT_THEME


def _session_path(data_dir: Path) -> Path:
    return data_dir / "live_session.json"


def read_session(data_dir: Path) -> Optional[LiveSessionState]:
    path = _session_path(data_dir)
    if not path.exists():
        return None
    try:
        return LiveSessionState(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("Corrupt live_session.json (%s); treating as no session.", exc)
        return None


def write_session(data_dir: Path, session: LiveSessionState) -> None:
    path = _session_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def clear_session(data_dir: Path) -> None:
    _session_path(data_dir).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Cached plan contents
# --------------------------------------------------------------------------


@dataclass
class PlanCache:
    """A plan's running order plus the lyrics for its songs.

    Held in memory by live_routes.py and rebuilt on demand (session start,
    "Reload plan", process restart). Not persisted: it's a pure cache of
    Planning Center data, and a stale copy on disk is worse than a refetch.
    """

    items: list[PlanItem] = field(default_factory=list)
    songs_by_item_id: dict[str, SongLyrics] = field(default_factory=dict)

    @staticmethod
    def build(items: list[PlanItem], songs: list[SongLyrics]) -> "PlanCache":
        return PlanCache(
            items=items,
            songs_by_item_id={s.item_id: s for s in songs if s.item_id},
        )

    @property
    def song_items(self) -> list[PlanItem]:
        return [i for i in self.items if i.is_song]

    def index_of(self, item_id: Optional[str]) -> Optional[int]:
        """Position of an item in the *full* running order, not just songs."""
        if not item_id:
            return None
        for index, item in enumerate(self.items):
            if item.id == item_id:
                return index
        return None


# --------------------------------------------------------------------------
# The decision: what is on the projector right now?
# --------------------------------------------------------------------------

DisplayStatus = Literal["waiting", "song", "hold", "stale"]


@dataclass
class DisplayState:
    """What the projector should render, and what the remote should report.

    `status` is what the display branches on:
      - "waiting": nothing is live yet (service hasn't started, or Planning
        Center reports no current item). Show a neutral holding screen.
      - "song": a song we have lyrics for is live. Show them.
      - "hold": a *non-song* item is live -- sermon, announcements, offering.
        Show a blank screen. This is the case that makes the whole item_id
        round trip worth it: without it the projector would sit on the
        previous song's lyrics through the entire sermon.
      - "stale": Planning Center is unreachable, or reports an item this
        cache has never heard of (the plan was edited mid-service). Keep the
        last frame rather than blanking; surface the problem on the remote.
    """

    status: DisplayStatus
    title: str = ""
    lyrics: str = ""
    ccli_number: Optional[str] = None
    item_id: Optional[str] = None
    # 1-based position among *songs*, for the remote's "3 / 5" readout. None
    # whenever a non-song item is live, since it has no song position.
    song_position: Optional[int] = None
    total_songs: int = 0
    note: str = ""


def resolve_display(cache: PlanCache, live: LiveStatus) -> DisplayState:
    """Turn Planning Center's live state into what the projector shows.

    Pure: no network, no clock, no I/O -- every branch here is a unit test in
    tests/test_live_session.py. The ordering of the checks is the contract:
    unreachable beats unknown-item beats non-song beats song, because each
    later case assumes the earlier ones were ruled out.
    """
    total_songs = len(cache.song_items)

    if not live.reachable:
        return DisplayState(
            status="stale",
            total_songs=total_songs,
            note=live.error or "Planning Center unreachable",
        )

    if live.current_item_id is None:
        return DisplayState(
            status="waiting",
            total_songs=total_songs,
            note="Nothing is live in Planning Center yet.",
        )

    item = next((i for i in cache.items if i.id == live.current_item_id), None)
    if item is None:
        # PCO is live on an item this cache predates -- someone added or
        # reordered items after the session started. Holding the last frame
        # is safer than blanking mid-song; the remote nudges the operator to
        # reload the plan.
        return DisplayState(
            status="stale",
            item_id=live.current_item_id,
            total_songs=total_songs,
            note="Planning Center is on an item this session hasn't loaded -- reload the plan.",
        )

    song = cache.songs_by_item_id.get(item.id)
    if song is None:
        return DisplayState(
            status="hold",
            title=item.title,
            item_id=item.id,
            total_songs=total_songs,
            note=f"{item.title} is live (not a song) -- display is blank.",
        )

    song_items = cache.song_items
    position = next((n for n, i in enumerate(song_items, start=1) if i.id == item.id), None)

    return DisplayState(
        status="song",
        title=song.title,
        lyrics=song.body(include_chords=False),
        ccli_number=song.ccli_number,
        item_id=item.id,
        song_position=position,
        total_songs=total_songs,
    )


def steps_between(cache: PlanCache, from_item_id: Optional[str], to_item_id: str) -> Optional[int]:
    """How many go_to_next_item calls get Planning Center from one item to another.

    Negative means go_to_previous_item. None means one of the two items isn't
    in this cache's running order.

    This exists because Services LIVE exposes only relative movement -- there
    is no "go to item X" action -- so the remote's tap-a-song-to-jump list has
    to walk there one step at a time. Callers are expected to cap how far
    they're willing to walk (see live_routes.MAX_JUMP_STEPS) rather than fire
    an unbounded burst of writes at Planning Center.
    """
    to_index = cache.index_of(to_item_id)
    if to_index is None:
        return None
    from_index = cache.index_of(from_item_id)
    if from_index is None:
        return None
    return to_index - from_index
