#!/usr/bin/env python3
"""
live_session.py

State + pure logic behind the live projection feature: which plan is being
projected, what the projector should have on screen at this instant, and the
light/dark theme.

Like scheduler.py, this module deliberately has **no Flask, HTTP, or
threading** in it -- just dataclasses, JSON persistence, and one decision
function (`resolve_display`) that turns "what Planning Center says is live"
into "what the projector shows". live_routes.py owns the HTTP surface and the
polling; this module owns the rules, so they can be tested without a server
or network (tests/test_live_session.py).

Persisted on the same DATA_DIR volume admin_app.py already uses:
    <DATA_DIR>/live_session.json   the active projection session, if any

## Why the projector view needs no credential at all

It shows one song -- whichever Planning Center says is live right now --
and it is served **only while the public site is open**, the same
`state.txt` gate `/` already uses. That makes it strictly less than what `/`
is already serving to anyone on the internet at that moment: same
availability window, one song instead of the whole plan. A credential
protecting a strict subset of already-public data protects nothing.

Being public is also what makes it useful beyond the projector: the
congregation can follow the current song on their phones from the same URL.

This deliberately hangs off the open/closed state rather than off "is a
session running", because **Planning Center never clears
`current_item_time`**. Confirmed 2026-07-26: hours after a service ended,
with control released and nobody driving, the live resource still reported
the last song. A session left running would therefore serve that song's
lyrics indefinitely -- exactly the all-week exposure the open/closed
machinery exists to prevent. `state.txt` is the gate that already has an
answer for when lyrics may be served, so it's the one to reuse.

An earlier version gated this with an unguessable token in the URL. That
was removed as protection that bought nothing while costing a URL nobody
could type -- see ARCHITECTURE.md for the full reasoning.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional

from pco_client import (
    LiveStatus,
    PlanItem,
    SongLyrics,
    dedupe_stanzas,
    split_stanzas,
    wrap_lines,
)

log = logging.getLogger("live_session")

Theme = Literal["dark", "light"]
Mode = Literal["follow", "control"]

# White-on-black is the default because that's what a darkened room wants and
# what the previous proof of concept did; "light" exists for bright rooms and
# for projectors whose blacks wash out to grey anyway.
DEFAULT_THEME: Theme = "dark"


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


def project_lyrics(song: SongLyrics) -> str:
    """Shape one song's lyrics for the projector.

    Three passes, all of which exist to buy font size, because the whole song
    shares one screen (Services LIVE reports which *item* is current, never
    which stanza, so there is nothing to page through):

    1. **Strip section labels** -- band notes like "VERSE 1:"/"REFRÄNG:" must
       never appear on a wall, and they were 16% of lines in the corpus.
    2. **Drop repeated stanzas** -- a chorus printed three times only steals
       room here, unlike in the deck where each repeat is a slide of its own.
    3. **Break over-long lines at sensible points** -- the browser was
       already wrapping them, just wherever the edge happened to fall.

    Together these took a 19-song corpus from 511 lines to 397. Long songs
    still end up small; that's inherent to showing a whole song at once.
    """
    stanzas = dedupe_stanzas(split_stanzas(song.plain_lyrics, strip_labels=True))
    if not stanzas:
        # Preserves body()'s "_No lyrics found..._" note rather than blanking
        # a song the plan says should be on screen.
        return song.body(include_chords=False)
    return "\n\n".join(wrap_lines(stanza) for stanza in stanzas)


@dataclass
class PlanCache:
    """A plan's running order plus the lyrics for its songs.

    Held in memory by live_routes.py and rebuilt on demand (session start,
    "Reload plan", process restart). Not persisted: it's a pure cache of
    Planning Center data, and a stale copy on disk is worse than a refetch.
    """

    items: list[PlanItem] = field(default_factory=list)
    songs_by_item_id: dict[str, SongLyrics] = field(default_factory=dict)
    # Projector-ready text, computed once here rather than on every poll --
    # the display asks for this ~every 1.5s and the shaping never changes
    # between refetches of the plan.
    projected_by_item_id: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def build(items: list[PlanItem], songs: list[SongLyrics]) -> "PlanCache":
        by_item_id = {s.item_id: s for s in songs if s.item_id}
        return PlanCache(
            items=items,
            songs_by_item_id=by_item_id,
            projected_by_item_id={i: project_lyrics(s) for i, s in by_item_id.items()},
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

DisplayStatus = Literal["waiting", "song", "hold", "stale", "closed"]


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
      - "closed": the public site is closed, so no lyrics may be served at
        all. Decided by the caller (live_routes) before resolve_display is
        ever reached, since this module has no notion of state.txt.
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
        lyrics=cache.projected_by_item_id.get(item.id, song.body(include_chords=False)),
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
