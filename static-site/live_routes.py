#!/usr/bin/env python3
"""
live_routes.py

The HTTP surface of the live projection feature -- three route groups that
admin_app.py mounts into its single Flask app:

    /live              the projector -- and anyone's phone. Public, but only
                       serves lyrics while the public site is open, so it is
                       strictly a subset of what "/" already serves. See
                       live_session.py for why it needs no credential.
    /remote/*          the operator's controller. Behind ADMIN_PASSWORD, the
                       same credential as /admin -- see admin_app._require_auth
                       for why this isn't a separate one.
    /admin/live/*      pick a plan, start/stop a session, take or release
                       Planning Center control.
                       Covered by admin_app's existing /admin Basic Auth.

Decisions live in live_session.py (pure, tested); this module does I/O,
caching, and rendering. The one piece of real policy here is the Planning
Center poll cache below.

## Follow vs. control

A session always starts in **follow** mode: this app reads Planning Center's
live state and mirrors it onto the projector, and has never written anything.
Whoever is running Services LIVE from their own device stays in charge and
doesn't know we exist.

**Control** is entered only by an explicit, confirmed click on /admin/live.
Planning Center allows exactly one controller per plan, so taking it boots
whoever had it with no warning on their end -- which is why it is never
implicit, never on a timer, and never a side effect of opening a page.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable, Optional

import requests
from flask import Blueprint, Flask, Response, jsonify, redirect, request, url_for

from live_session import (
    DEFAULT_THEME,
    DisplayState,
    LiveSessionState,
    PlanCache,
    clear_session,
    read_session,
    resolve_display,
    steps_between,
    write_session,
)
from pco_client import (
    LiveStatus,
    PlanningCenterError,
    collect_songs,
    get_live_status,
    get_plan_by_id,
    get_plan_item_summaries,
    list_selectable_plans,
    list_service_types,
    live_go_to_next_item,
    live_go_to_previous_item,
    live_release_control,
    live_take_control,
    plan_display_title,
)

log = logging.getLogger("live_routes")

# How long a Planning Center live-status read is reused across callers.
#
# The projector polls ~every 1.5s and the remote ~every 2s, and there may be
# several of each on a church network. Without this, ten open tabs would mean
# ten times the API traffic for identical data and would walk straight into
# Planning Center's rate limit mid-service. One cached read serves them all,
# so the API cost is fixed no matter how many devices are watching.
LIVE_POLL_CACHE_SECONDS = 1.5

# Ceiling on how many go_to_next_item/go_to_previous_item calls one
# tap-to-jump may fire. Services LIVE has no "go to item X" action, so a jump
# is a walk (see live_session.steps_between); this stops a mis-tap on a long
# plan from firing a burst of writes at Planning Center.
MAX_JUMP_STEPS = 20


@dataclass
class LiveContext:
    """Everything the routes need from admin_app, passed in rather than
    imported, so this module has no circular dependency on it."""

    data_dir: Path
    session: requests.Session
    lock: threading.Lock
    # Returns True when the public site is open. Injected rather than
    # imported: admin_app owns the open/closed concept (state.txt), and
    # importing it here would be circular.
    site_is_open: Callable[[], bool]


_ctx: Optional[LiveContext] = None

# In-memory cache of the projected plan's contents. Not persisted: it's a
# pure mirror of Planning Center data, and a stale copy on disk would be
# worse than refetching after a restart.
_cache_lock = threading.Lock()
_plan_cache: Optional[PlanCache] = None
_cached_plan_id: Optional[str] = None

_live_lock = threading.Lock()
_live_cached: Optional[LiveStatus] = None
_live_cached_at: float = 0.0
_live_cached_key: Optional[tuple[str, str]] = None


def ctx() -> LiveContext:
    if _ctx is None:  # pragma: no cover -- init_app is called at import time
        raise RuntimeError("live_routes.init_app() was never called")
    return _ctx


# --------------------------------------------------------------------------
# Plan cache + Planning Center polling
# --------------------------------------------------------------------------


def load_plan_cache(session_state: LiveSessionState, force: bool = False) -> PlanCache:
    """Fetch (or reuse) the running order + lyrics for the projected plan."""
    global _plan_cache, _cached_plan_id
    with _cache_lock:
        if not force and _plan_cache is not None and _cached_plan_id == session_state.plan_id:
            return _plan_cache

    items = get_plan_item_summaries(ctx().session, session_state.service_type_id, session_state.plan_id)
    songs = collect_songs(
        ctx().session, session_state.service_type_id, session_state.plan_id, include_pdf_links=False
    )
    cache = PlanCache.build(items, songs)

    with _cache_lock:
        _plan_cache = cache
        _cached_plan_id = session_state.plan_id
    log.info(
        "Loaded plan %s for projection: %d item(s), %d song(s).",
        session_state.plan_id,
        len(items),
        len(cache.song_items),
    )
    return cache


def cache_for(session_state: Optional[LiveSessionState]) -> Optional[PlanCache]:
    """The cached plan contents, but only if they belong to `session_state`.

    Read under the lock so a concurrent reload can't be observed half-swapped
    (cache from the old plan, id from the new one).
    """
    if session_state is None:
        return None
    with _cache_lock:
        return _plan_cache if _cached_plan_id == session_state.plan_id else None


def drop_plan_cache() -> None:
    global _plan_cache, _cached_plan_id, _live_cached, _live_cached_key
    with _cache_lock:
        _plan_cache, _cached_plan_id = None, None
    with _live_lock:
        _live_cached, _live_cached_key = None, None


def poll_live(session_state: LiveSessionState, force: bool = False) -> LiveStatus:
    """Read Planning Center's live state, reusing a recent read if there is one."""
    global _live_cached, _live_cached_at, _live_cached_key
    key = (session_state.service_type_id, session_state.plan_id)
    now = time.monotonic()

    with _live_lock:
        fresh = (
            _live_cached is not None
            and _live_cached_key == key
            and (now - _live_cached_at) < LIVE_POLL_CACHE_SECONDS
        )
        if fresh and not force:
            return _live_cached

    status = get_live_status(ctx().session, session_state.service_type_id, session_state.plan_id)

    with _live_lock:
        _live_cached, _live_cached_at, _live_cached_key = status, time.monotonic(), key
    return status


def current_display(session_state: Optional[LiveSessionState]) -> tuple[DisplayState, Optional[LiveStatus]]:
    """The projector's current frame, plus the raw live status behind it."""
    if not ctx().site_is_open():
        # No lyrics may be served at all right now. Checked before anything
        # else -- including before a Planning Center poll -- so a closed site
        # can't leak a song through this route no matter what LIVE reports.
        return DisplayState(status="closed", note="The site is closed."), None
    if session_state is None:
        return DisplayState(status="waiting", note="No projection session is running."), None
    try:
        cache = load_plan_cache(session_state)
    except PlanningCenterError as exc:
        return DisplayState(status="stale", note=f"Could not load the plan: {exc}"), None
    live = poll_live(session_state)
    return resolve_display(cache, live), live


def _display_json(state: DisplayState, session_state: Optional[LiveSessionState]) -> dict:
    return {
        "status": state.status,
        "title": state.title,
        "lyrics": state.lyrics,
        "ccli_number": state.ccli_number,
        "item_id": state.item_id,
        "song_position": state.song_position,
        "total_songs": state.total_songs,
        "note": state.note,
        "theme": session_state.theme if session_state else DEFAULT_THEME,
        "plan_title": session_state.plan_title if session_state else "",
    }


# --------------------------------------------------------------------------
# /live -- the projector, and anyone's phone. Public, read-only.
# --------------------------------------------------------------------------

live_bp = Blueprint("live", __name__, url_prefix="/live")


@live_bp.route("/")
def display_page():
    return _DISPLAY_HTML.replace("__STATE_URL__", url_for("live.display_state"))


@live_bp.route("/state.json")
def display_state():
    session_state = read_session(ctx().data_dir)
    state, _ = current_display(session_state)
    return jsonify(_display_json(state, session_state))


# --------------------------------------------------------------------------
# /remote -- the operator's controller. Its own password.
# --------------------------------------------------------------------------

remote_bp = Blueprint("remote", __name__, url_prefix="/remote")


@remote_bp.route("/")
def remote_page():
    return _REMOTE_HTML


@remote_bp.route("/state.json")
def remote_state():
    session_state = read_session(ctx().data_dir)
    state, live = current_display(session_state)
    payload = _display_json(state, session_state)

    cache = cache_for(session_state)
    payload["running_order"] = (
        [
            {"item_id": i.id, "title": i.title, "is_song": i.is_song, "is_current": i.id == state.item_id}
            for i in cache.items
        ]
        if cache
        else []
    )
    payload["mode"] = session_state.mode if session_state else "follow"
    payload["holds_control"] = bool(live and live.holds_control)
    payload["controller_name"] = live.controller_name if live else None
    payload["session_active"] = session_state is not None
    return jsonify(payload)


def _require_session() -> tuple[Optional[LiveSessionState], Optional[Response]]:
    session_state = read_session(ctx().data_dir)
    if session_state is None:
        return None, (jsonify({"error": "No projection session is running."}), 409)
    return session_state, None


def _require_control() -> tuple[Optional[LiveSessionState], Optional[Response]]:
    """Guard every route that writes to Planning Center.

    Refuses in follow mode instead of silently taking control: the whole
    point of follow mode is that this app never writes, so a Next press from
    a remote that hasn't been given control must fail loudly rather than
    quietly boot the leader's iPad.
    """
    session_state, error = _require_session()
    if error:
        return None, error
    if session_state.mode != "control":
        return None, (
            jsonify(
                {
                    "error": "This session is in follow mode. Take control from the admin screen "
                    "before driving Planning Center from here."
                }
            ),
            409,
        )
    return session_state, None


@remote_bp.route("/next", methods=["POST"])
def remote_next():
    session_state, error = _require_control()
    if error:
        return error
    try:
        live_go_to_next_item(ctx().session, session_state.service_type_id, session_state.plan_id)
    except PlanningCenterError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(_display_json(*_refreshed(session_state)))


@remote_bp.route("/prev", methods=["POST"])
def remote_prev():
    session_state, error = _require_control()
    if error:
        return error
    try:
        live_go_to_previous_item(ctx().session, session_state.service_type_id, session_state.plan_id)
    except PlanningCenterError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(_display_json(*_refreshed(session_state)))


@remote_bp.route("/goto/<item_id>", methods=["POST"])
def remote_goto(item_id: str):
    """Walk Planning Center to a specific item, one step at a time.

    Services LIVE has no absolute "go to item" action, so this issues the
    computed number of next/previous calls -- capped, because a mis-tap on a
    long plan should not turn into an unbounded burst of API writes.
    """
    session_state, error = _require_control()
    if error:
        return error
    try:
        cache = load_plan_cache(session_state)
        live = poll_live(session_state, force=True)
        steps = steps_between(cache, live.current_item_id, item_id)
        if steps is None:
            return jsonify({"error": "That item isn't in the loaded plan -- reload it."}), 409
        if abs(steps) > MAX_JUMP_STEPS:
            return jsonify({"error": f"That jump is {abs(steps)} steps; the limit is {MAX_JUMP_STEPS}."}), 409

        move = live_go_to_next_item if steps > 0 else live_go_to_previous_item
        for _ in range(abs(steps)):
            move(ctx().session, session_state.service_type_id, session_state.plan_id)
    except PlanningCenterError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(_display_json(*_refreshed(session_state)))


@remote_bp.route("/theme", methods=["POST"])
def remote_theme():
    """Flip the projector between white-on-black and black-on-white.

    Lives on the remote, not the display, because the display is read-only
    and because the person who needs to flip it is standing at the back of
    the room looking at the screen, not at the booth machine's keyboard.
    """
    session_state, error = _require_session()
    if error:
        return error
    with ctx().lock:
        session_state.theme = "light" if session_state.theme == "dark" else "dark"
        write_session(ctx().data_dir, session_state)
    return jsonify(_display_json(*_refreshed(session_state)))


@remote_bp.route("/reload", methods=["POST"])
def remote_reload():
    session_state, error = _require_session()
    if error:
        return error
    try:
        load_plan_cache(session_state, force=True)
    except PlanningCenterError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(_display_json(*_refreshed(session_state)))


def _refreshed(session_state: LiveSessionState) -> tuple[DisplayState, LiveSessionState]:
    state, _ = current_display(session_state)
    return state, session_state


# --------------------------------------------------------------------------
# /admin/live -- pick a plan, start/stop, take/release control.
# Auth comes from admin_app's existing /admin Basic Auth gate.
# --------------------------------------------------------------------------

admin_live_bp = Blueprint("admin_live", __name__, url_prefix="/admin/live")


@admin_live_bp.route("/")
def live_index():
    session_state = read_session(ctx().data_dir)
    error = request.args.get("error")
    notice = request.args.get("notice")

    selected_st = request.args.get("service_type_id", "")
    try:
        service_types = list_service_types(ctx().session)
    except PlanningCenterError as exc:
        service_types = []
        error = error or f"Could not load service types: {exc}"

    plan_options = ""
    if selected_st:
        try:
            plan_options = "\n".join(
                f'<option value="{escape(p["id"])}">{escape(plan_display_title(p))}</option>'
                for p in list_selectable_plans(ctx().session, selected_st)
            )
        except PlanningCenterError as exc:
            error = error or f"Could not load plans: {exc}"
    if selected_st and not plan_options and not error:
        plan_options = '<option value="" disabled>No upcoming plans with a scheduled time</option>'

    if session_state is None:
        body = _LIVE_PICKER_TEMPLATE.format(
            service_type_options="\n".join(
                f'<option value="{escape(st["id"])}"'
                f'{" selected" if st["id"] == selected_st else ""}>'
                f'{escape(st["attributes"].get("name") or st["id"])}</option>'
                for st in service_types
            ),
            plan_options=plan_options,
            plans_url=url_for("admin_live.live_index"),
            start_url=url_for("admin_live.start"),
            plan_select_disabled="" if plan_options else " disabled",
        )
    else:
        state, live = current_display(session_state)
        display_url = url_for("live.display_page", _external=True)
        in_control = bool(live and live.holds_control)
        controller = (live.controller_name if live else None) or "nobody"

        body = _LIVE_ACTIVE_TEMPLATE.format(
            plan_title=escape(session_state.plan_title),
            mode=escape(session_state.mode),
            mode_class="control" if session_state.mode == "control" else "follow",
            theme=escape(session_state.theme),
            display_url=escape(display_url),
            now_showing=escape(_describe(state)),
            controller=escape(controller),
            control_button=(
                _RELEASE_BUTTON.format(url=url_for("admin_live.release"))
                if in_control
                else _TAKE_BUTTON.format(url=url_for("admin_live.take"), controller=escape(controller))
            ),
            remote_url=url_for("remote.remote_page"),
            stop_url=url_for("admin_live.stop"),
            reload_url=url_for("admin_live.reload_plan"),
            closed_warning=(
                ""
                if ctx().site_is_open()
                else '<div class="warn">The site is currently <strong>closed</strong>, so the '
                "projector shows the &ldquo;come back Sunday&rdquo; placeholder rather than "
                "lyrics. Open it from the status page to start projecting.</div>"
            ),
        )

    return _LIVE_PAGE_TEMPLATE.format(
        body=body,
        index_url=url_for("admin.index"),
        error_html=f'<p class="error">{escape(error)}</p>' if error else "",
        notice_html=f'<p class="notice">{escape(notice)}</p>' if notice else "",
    )


def _describe(state: DisplayState) -> str:
    if state.status == "song":
        position = f"{state.song_position}/{state.total_songs}" if state.song_position else "?"
        return f"{state.title} ({position})"
    return state.note or state.status


@admin_live_bp.route("/start", methods=["POST"])
def start():
    service_type_id = request.form.get("service_type_id", "").strip()
    plan_id = request.form.get("plan_id", "").strip()
    if not service_type_id or not plan_id:
        return redirect(url_for("admin_live.live_index", error="Choose a service type and a plan."))

    try:
        _, plan = get_plan_by_id(ctx().session, plan_id, service_type_id)
    except PlanningCenterError as exc:
        return redirect(url_for("admin_live.live_index", error=str(exc)))

    session_state = LiveSessionState(
        service_type_id=service_type_id,
        plan_id=plan_id,
        plan_title=plan_display_title(plan),
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    drop_plan_cache()
    with ctx().lock:
        write_session(ctx().data_dir, session_state)

    try:
        load_plan_cache(session_state, force=True)
    except PlanningCenterError as exc:
        return redirect(url_for("admin_live.live_index", error=f"Session started, but: {exc}"))
    return redirect(url_for("admin_live.live_index", notice="Projection session started in follow mode."))


@admin_live_bp.route("/stop", methods=["POST"])
def stop():
    """End the session, releasing Planning Center control if we hold it.

    Releasing on the way out matters: leaving control parked on this app
    after the service would mean the next person to open Services LIVE on
    their phone finds themselves locked out by a projector nobody is looking
    at any more.
    """
    session_state = read_session(ctx().data_dir)
    if session_state and session_state.mode == "control":
        try:
            live_release_control(ctx().session, session_state.service_type_id, session_state.plan_id)
        except PlanningCenterError as exc:
            log.warning("Could not release Planning Center control while stopping: %s", exc)
    with ctx().lock:
        clear_session(ctx().data_dir)
    drop_plan_cache()
    return redirect(url_for("admin_live.live_index", notice="Projection session stopped."))


@admin_live_bp.route("/take", methods=["POST"])
def take():
    session_state = read_session(ctx().data_dir)
    if session_state is None:
        return redirect(url_for("admin_live.live_index", error="No session is running."))
    try:
        status = live_take_control(ctx().session, session_state.service_type_id, session_state.plan_id)
    except PlanningCenterError as exc:
        return redirect(url_for("admin_live.live_index", error=str(exc)))
    if not status.holds_control:
        return redirect(
            url_for(
                "admin_live.live_index",
                error="Planning Center did not hand over control -- check this token's permissions on the plan.",
            )
        )
    with ctx().lock:
        session_state.mode = "control"
        write_session(ctx().data_dir, session_state)
    return redirect(url_for("admin_live.live_index", notice="Control taken. The remote can now drive the plan."))


@admin_live_bp.route("/release", methods=["POST"])
def release():
    session_state = read_session(ctx().data_dir)
    if session_state is None:
        return redirect(url_for("admin_live.live_index", error="No session is running."))
    try:
        live_release_control(ctx().session, session_state.service_type_id, session_state.plan_id)
    except PlanningCenterError as exc:
        return redirect(url_for("admin_live.live_index", error=str(exc)))
    with ctx().lock:
        session_state.mode = "follow"
        write_session(ctx().data_dir, session_state)
    return redirect(url_for("admin_live.live_index", notice="Control released. Back to follow mode."))


@admin_live_bp.route("/reload", methods=["POST"])
def reload_plan():
    session_state = read_session(ctx().data_dir)
    if session_state is None:
        return redirect(url_for("admin_live.live_index", error="No session is running."))
    try:
        load_plan_cache(session_state, force=True)
    except PlanningCenterError as exc:
        return redirect(url_for("admin_live.live_index", error=str(exc)))
    return redirect(url_for("admin_live.live_index", notice="Plan reloaded from Planning Center."))


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

_DISPLAY_HTML = """<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<title>Projektion</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body { margin: 0; padding: 0; height: 100%; overflow: hidden;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; }
  body { background: #000; color: #fff; transition: background 200ms, color 200ms; }
  body.light { background: #fff; color: #000; }
  #wrap { height: 100vh; width: 100vw; display: flex; align-items: center;
    justify-content: center; box-sizing: border-box; padding: 5vh 5vw; text-align: center; }
  #lyrics { white-space: pre-wrap; line-height: 1.3; font-weight: 600; }
  #idle { font-size: 3vh; opacity: 0.35; }
  /* Deliberately faint: a connection warning belongs on the operator's
     remote, not blazing across a screen a congregation is looking at. */
  #warn { position: fixed; bottom: 1.2vh; right: 1.5vw; font-size: 1.6vh;
    opacity: 0.3; display: none; }
</style>
</head>
<body>
  <div id="wrap"><div id="lyrics"></div><div id="idle"></div></div>
  <div id="warn"></div>
<script>
const STATE_URL = "__STATE_URL__";
let lastKey = null;

function fit() {
  const el = document.getElementById('lyrics');
  const wrap = document.getElementById('wrap');
  if (!el.textContent.trim()) return;
  let size = 11;
  el.style.fontSize = size + 'vh';
  let guard = 0;
  while (guard++ < 60 && size > 1.2 &&
         (el.scrollHeight > wrap.clientHeight || el.scrollWidth > wrap.clientWidth)) {
    size -= 0.3;
    el.style.fontSize = size + 'vh';
  }
}

function show(text) {
  const el = document.getElementById('lyrics');
  document.getElementById('idle').textContent = '';
  el.textContent = text;
  fit();
}

async function poll() {
  let data;
  try {
    const res = await fetch(STATE_URL, {cache: 'no-store'});
    if (!res.ok) throw new Error(res.status);
    data = await res.json();
  } catch (e) {
    // Our own server is unreachable. Hold the current frame -- blanking a
    // projector mid-song is far worse than showing lyrics a few seconds stale.
    document.getElementById('warn').style.display = 'block';
    document.getElementById('warn').textContent = 'offline';
    return;
  }
  document.getElementById('warn').style.display = 'none';
  document.body.classList.toggle('light', data.theme === 'light');

  // "stale" means Planning Center is unreachable or has moved to an item we
  // don't know about -- same reasoning as above, keep what's on screen.
  if (data.status === 'stale') return;

  const key = data.status + ':' + (data.item_id || '') + ':' + data.title;
  if (key === lastKey) return;
  lastKey = key;

  if (data.status === 'song') {
    show(data.lyrics);
  } else if (data.status === 'hold') {
    // A non-song item is live (sermon, offering). Blank, not stale lyrics.
    show('');
  } else if (data.status === 'closed') {
    // Site closed: no lyrics may be served. Same message the front page
    // shows, so a phone bookmarked on this URL reads sensibly all week.
    show('');
    document.getElementById('idle').textContent =
      'Sidan visas bara under gudstjänsten.';
  } else {
    show('');
    document.getElementById('idle').textContent = data.plan_title || '';
  }
}

window.addEventListener('resize', fit);
poll();
setInterval(poll, 1500);
</script>
</body>
</html>
"""

_REMOTE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Song Remote</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<style>
  html, body { margin: 0; padding: 0; background: #111; color: #fff; height: 100%;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; }
  #plan { padding: 12px 16px 0; color: #999; font-size: 13px;
    text-transform: uppercase; letter-spacing: 0.05em; }
  #current { padding: 4px 16px 12px; font-size: 21px; font-weight: 700; }
  #banner { margin: 0 16px 12px; padding: 10px 12px; border-radius: 8px;
    font-size: 13px; line-height: 1.4; display: none; }
  #banner.follow { background: #2a2a12; color: #e8d98a; display: block; }
  #banner.error { background: #3a1616; color: #f3a0a0; display: block; }
  #buttons { display: flex; gap: 10px; padding: 0 16px 12px; }
  button.nav { flex: 1; font-size: 22px; padding: 26px 0; border: none;
    border-radius: 12px; background: #2d6cdf; color: #fff; font-weight: 700; }
  button.nav:disabled { background: #2a2a2a; color: #666; }
  button.nav:active:enabled { background: #1d4fa8; }
  #tools { display: flex; gap: 8px; padding: 0 16px 14px; }
  #tools button { flex: 1; padding: 12px 4px; background: #333; color: #ccc;
    border: none; border-radius: 10px; font-size: 13px; }
  #list { list-style: none; margin: 0; padding: 0 0 40px; }
  #list li { padding: 15px 20px; border-bottom: 1px solid #222; font-size: 17px; }
  #list li.song.active { background: #2d6cdf; font-weight: 700; }
  #list li.other { color: #777; font-size: 15px; font-style: italic; }
  #list li.other.active { background: #444; color: #ddd; }
</style>
</head>
<body>
  <div id="plan"></div>
  <div id="current">Loading&hellip;</div>
  <div id="banner"></div>
  <div id="buttons">
    <button class="nav" id="prev">&larr; Prev</button>
    <button class="nav" id="next">Next &rarr;</button>
  </div>
  <div id="tools">
    <button id="theme">Flip black/white</button>
    <button id="reload">Reload plan</button>
  </div>
  <ul id="list"></ul>
<script>
let state = null;
let lastError = '';

function render() {
  if (!state) return;
  document.getElementById('plan').textContent = state.plan_title || '';

  let label = 'No session running';
  if (state.session_active) {
    if (state.status === 'song') {
      label = (state.song_position || '?') + ' / ' + state.total_songs + '  ' + state.title;
    } else if (state.status === 'hold') {
      label = state.title + '  (display blank)';
    } else {
      label = state.note || state.status;
    }
  }
  document.getElementById('current').textContent = label;

  const banner = document.getElementById('banner');
  const controlling = state.mode === 'control';
  if (lastError) {
    banner.className = 'error';
    banner.textContent = lastError;
  } else if (state.session_active && !controlling) {
    banner.className = 'follow';
    banner.textContent = 'Follow mode \\u2014 mirroring ' + (state.controller_name || 'Planning Center')
      + '. Take control from the admin screen to drive the plan from here.';
  } else {
    banner.className = '';
    banner.style.display = 'none';
  }

  document.getElementById('prev').disabled = !controlling;
  document.getElementById('next').disabled = !controlling;

  const list = document.getElementById('list');
  list.innerHTML = '';
  (state.running_order || []).forEach((item) => {
    const li = document.createElement('li');
    li.textContent = item.title;
    li.className = (item.is_song ? 'song' : 'other') + (item.is_current ? ' active' : '');
    if (item.is_song && controlling) li.addEventListener('click', () => post('/remote/goto/' + item.item_id));
    list.appendChild(li);
  });
}

async function refresh() {
  try {
    const res = await fetch('/remote/state.json', {cache: 'no-store'});
    state = await res.json();
    render();
  } catch (e) { /* transient -- next tick */ }
}

async function post(path) {
  try {
    const res = await fetch(path, {method: 'POST'});
    const data = await res.json();
    lastError = res.ok ? '' : (data.error || 'Request failed');
    if (res.ok) { Object.assign(state, data); }
    render();
  } catch (e) {
    lastError = 'Could not reach the server.';
    render();
  }
}

document.getElementById('prev').addEventListener('click', () => post('/remote/prev'));
document.getElementById('next').addEventListener('click', () => post('/remote/next'));
document.getElementById('theme').addEventListener('click', () => post('/remote/theme'));
document.getElementById('reload').addEventListener('click', () => post('/remote/reload'));
document.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowRight' || e.key === ' ') post('/remote/next');
  if (e.key === 'ArrowLeft') post('/remote/prev');
});

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""

_LIVE_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Site Admin &mdash; live projection</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0 auto; max-width: 40em; padding: 2rem 1.25rem 4rem;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.5; color: #1a1a1a; background: #fff; }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #eee; background: #111; }} }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 1.8rem; }}
  a {{ color: #2d6cdf; }}
  .error {{ color: #d33; }}
  .notice {{ color: #2a9d4a; }}
  .meta {{ color: #777; font-size: 0.9rem; }}
  .badge {{ display: inline-block; padding: 0.2em 0.7em; border-radius: 1em;
    font-weight: 700; font-size: 0.85rem; color: #fff; }}
  .badge.follow {{ background: #b8860b; }}
  .badge.control {{ background: #2a9d4a; }}
  code {{ background: rgba(128,128,128,0.18); padding: 0.15em 0.4em;
    border-radius: 4px; word-break: break-all; font-size: 0.85rem; }}
  form {{ display: inline; }}
  button {{ font-size: 0.95rem; padding: 0.5em 1em; margin: 0.25em 0.4em 0.25em 0;
    border: none; border-radius: 8px; background: #555; color: #fff; font-weight: 700; }}
  button.primary {{ background: #2d6cdf; }}
  button.danger {{ background: #b03030; }}
  select {{ font-size: 1rem; padding: 0.4em; margin: 0.2em 0.4em 0.2em 0; max-width: 100%; }}
  .warn {{ border-left: 3px solid #b8860b; padding: 0.4em 0 0.4em 0.8em;
    margin: 0.8rem 0; font-size: 0.9rem; color: #997404; }}
  @media (prefers-color-scheme: dark) {{ .warn {{ color: #d9ad4a; }} }}
</style>
</head>
<body>
<h1>Live projection</h1>
<p><a href="{index_url}">&larr; Back to status</a></p>
{error_html}
{notice_html}
{body}
</body>
</html>
"""

_LIVE_PICKER_TEMPLATE = """<p class="meta">No projection session is running.</p>
<h2>Start a session</h2>
<form method="get" action="{plans_url}">
  <select name="service_type_id" onchange="this.form.submit()" required>
    <option value="" disabled selected>Choose a service type&hellip;</option>
    {service_type_options}
  </select>
  <noscript><button type="submit">List plans</button></noscript>
</form>
<form method="post" action="{start_url}">
  <input type="hidden" name="service_type_id" value="">
  <select name="plan_id" required{plan_select_disabled}>
    <option value="" disabled selected>Choose a plan&hellip;</option>
    {plan_options}
  </select>
  <button type="submit" class="primary">Start session</button>
</form>
<script>
  // Carry the service type chosen above into the start form, so picking a
  // plan can't submit a plan id without the service type it belongs to.
  const st = new URLSearchParams(location.search).get('service_type_id') || '';
  document.querySelector('input[name=service_type_id]').value = st;
</script>
<p class="meta">Only upcoming plans with a real scheduled time are listed &mdash; dateless
draft plans are filtered out, since Planning Center can't place them on a date.</p>
"""

_LIVE_ACTIVE_TEMPLATE = """<p><span class="badge {mode_class}">{mode} mode</span></p>
<p><strong>{plan_title}</strong></p>
<p class="meta">Now showing: {now_showing}<br>
Planning Center control: {controller}<br>
Theme: {theme} (flip it from the remote)</p>

<h2>Projector &amp; phones</h2>
<p class="meta">Public, no password &mdash; it only ever shows the one song Planning Center
has live, and only while the site is open, so it shows strictly less than the front page
already does. Congregation can open it on their phones.</p>
<p><code>{display_url}</code></p>
{closed_warning}

<h2>Remote</h2>
<p class="meta"><a href="{remote_url}">Open the remote &rarr;</a> (same login as this page)</p>

<h2>Control</h2>
{control_button}

<h2>Session</h2>
<form method="post" action="{reload_url}"><button>Reload plan from Planning Center</button></form>
<form method="post" action="{stop_url}"><button class="danger">Stop session</button></form>
"""

_TAKE_BUTTON = """<div class="warn">Taking control disconnects whoever is running Services LIVE
right now ({controller}) &mdash; Planning Center allows one controller per plan and gives them no
warning. Leave this alone unless you're the one running the service.</div>
<form method="post" action="{url}"
      onsubmit="return confirm('Take control of Services LIVE? This disconnects whoever currently has it.');">
  <button class="primary">Take control</button>
</form>"""

_RELEASE_BUTTON = """<p class="meta">This app currently holds Planning Center control,
so the remote can drive the plan.</p>
<form method="post" action="{url}"><button>Release control</button></form>"""


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def init_app(app: Flask, context: LiveContext) -> None:
    global _ctx
    _ctx = context
    app.register_blueprint(live_bp)
    app.register_blueprint(remote_bp)
    app.register_blueprint(admin_live_bp)
