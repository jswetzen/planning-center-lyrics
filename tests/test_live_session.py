"""
Tests for live_session.py -- the pure decision layer behind the projector.

The cases that matter here are the ones where a naive implementation shows
the *wrong* thing on a screen the whole congregation is looking at: leaving
the last song's lyrics up through the sermon, blanking mid-song because of a
network blip, or following a plan edit it hasn't loaded.
"""

import json

import pytest

import live_session
from live_session import (
    DEFAULT_THEME,
    LiveSessionState,
    PlanCache,
    clear_session,
    ensure_display_token,
    read_session,
    resolve_display,
    rotate_display_token,
    steps_between,
    token_matches,
    write_session,
)
from pco_client import LiveStatus, PlanItem, SongLyrics


def _item(item_id, title, item_type="song", sequence=0):
    return PlanItem(id=item_id, title=title, item_type=item_type, sequence=sequence)


def _song(item_id, title, lyrics="la la la", ccli="1234"):
    return SongLyrics(title, lyrics, "", ccli, item_id=item_id)


@pytest.fixture
def cache():
    """A realistic running order: welcome, two songs, sermon, closing song."""
    items = [
        _item("i1", "Welcome", "item", 1),
        _item("i2", "Song A", "song", 2),
        _item("i3", "Song B", "song", 3),
        _item("i4", "Sermon", "item", 4),
        _item("i5", "Song C", "song", 5),
    ]
    songs = [_song("i2", "Song A"), _song("i3", "Song B"), _song("i5", "Song C")]
    return PlanCache.build(items, songs)


# --------------------------------------------------------------------------
# resolve_display -- the four statuses
# --------------------------------------------------------------------------


def test_song_item_shows_its_lyrics(cache):
    state = resolve_display(cache, LiveStatus(current_item_id="i3"))
    assert state.status == "song"
    assert state.title == "Song B"
    assert state.lyrics == "la la la"
    assert state.ccli_number == "1234"


def test_song_position_counts_songs_not_items(cache):
    """"Song C" is the 5th item but only the 3rd song -- the remote's
    "n / total" readout must not count the welcome and the sermon."""
    state = resolve_display(cache, LiveStatus(current_item_id="i5"))
    assert (state.song_position, state.total_songs) == (3, 3)


def test_non_song_item_blanks_the_display(cache):
    """The regression this whole feature is built around: without resolving
    item ids, the projector sits on Song B's lyrics for the entire sermon."""
    state = resolve_display(cache, LiveStatus(current_item_id="i4"))
    assert state.status == "hold"
    assert state.lyrics == ""
    assert "Sermon" in state.note
    assert state.song_position is None


def test_nothing_live_yet_waits(cache):
    state = resolve_display(cache, LiveStatus(current_item_id=None))
    assert state.status == "waiting"
    assert state.lyrics == ""


def test_unreachable_planning_center_is_stale_not_blank(cache):
    """A network blip must not blank a projector mid-song -- the display
    holds its last frame and the remote reports why."""
    state = resolve_display(cache, LiveStatus(reachable=False, error="boom"))
    assert state.status == "stale"
    assert state.note == "boom"


def test_item_added_after_session_started_is_stale(cache):
    """Someone edits the plan mid-service; PCO goes live on an item this
    cache has never seen. Hold, and tell the operator to reload."""
    state = resolve_display(cache, LiveStatus(current_item_id="i99"))
    assert state.status == "stale"
    assert "reload" in state.note.lower()


def test_song_item_with_no_lyrics_record_holds(cache):
    """A song item whose song record failed to load is in `items` but not in
    `songs_by_item_id` -- it must hold, not raise."""
    partial = PlanCache.build(cache.items, [_song("i2", "Song A")])
    state = resolve_display(partial, LiveStatus(current_item_id="i3"))
    assert state.status == "hold"


def test_empty_plan_does_not_crash():
    state = resolve_display(PlanCache.build([], []), LiveStatus(current_item_id="i1"))
    assert state.status == "stale"
    assert state.total_songs == 0


# --------------------------------------------------------------------------
# steps_between -- relative movement for tap-to-jump
# --------------------------------------------------------------------------


def test_steps_between_counts_all_items_not_just_songs(cache):
    """Jumping Song B -> Song C crosses the sermon, so it's two LIVE steps,
    not one -- Services LIVE walks the whole running order."""
    assert steps_between(cache, "i3", "i5") == 2


def test_steps_between_is_negative_going_backwards(cache):
    assert steps_between(cache, "i5", "i2") == -3


def test_steps_between_is_zero_for_the_same_item(cache):
    assert steps_between(cache, "i3", "i3") == 0


def test_steps_between_unknown_item_is_none(cache):
    assert steps_between(cache, "i3", "nope") is None
    assert steps_between(cache, None, "i3") is None


# --------------------------------------------------------------------------
# Display token
# --------------------------------------------------------------------------


def test_token_is_minted_once_and_reused(tmp_path):
    first = ensure_display_token(tmp_path)
    assert ensure_display_token(tmp_path) == first
    assert len(first) > 30


def test_rotating_invalidates_the_old_token(tmp_path):
    old = ensure_display_token(tmp_path)
    new = rotate_display_token(tmp_path)
    assert new != old
    assert token_matches(tmp_path, new)
    assert not token_matches(tmp_path, old)


def test_token_match_rejects_empty_and_missing(tmp_path):
    assert not token_matches(tmp_path, "anything")  # nothing stored yet
    ensure_display_token(tmp_path)
    assert not token_matches(tmp_path, "")


# --------------------------------------------------------------------------
# Session persistence
# --------------------------------------------------------------------------


def test_session_round_trips(tmp_path):
    session = LiveSessionState(
        service_type_id="st1", plan_id="p1", plan_title="Sunday", started_at="2026-07-26T09:00:00+00:00"
    )
    write_session(tmp_path, session)
    loaded = read_session(tmp_path)
    assert loaded == session
    assert loaded.mode == "follow"  # never starts in control
    assert loaded.theme == DEFAULT_THEME


def test_no_session_reads_as_none(tmp_path):
    assert read_session(tmp_path) is None


def test_corrupt_session_reads_as_none_instead_of_raising(tmp_path):
    (tmp_path / "live_session.json").write_text("{not json", encoding="utf-8")
    assert read_session(tmp_path) is None


def test_session_with_unknown_field_reads_as_none(tmp_path):
    """A file written by a newer version shouldn't crash the app on startup."""
    (tmp_path / "live_session.json").write_text(json.dumps({"wat": 1}), encoding="utf-8")
    assert read_session(tmp_path) is None


def test_clear_session_is_idempotent(tmp_path):
    clear_session(tmp_path)  # nothing there yet
    write_session(
        tmp_path,
        LiveSessionState(
            service_type_id="st1", plan_id="p1", plan_title="S", started_at="2026-07-26T09:00:00+00:00"
        ),
    )
    clear_session(tmp_path)
    assert read_session(tmp_path) is None
