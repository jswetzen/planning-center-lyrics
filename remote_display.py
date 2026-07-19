#!/usr/bin/env python3
"""
remote_display.py

A small local web app that lets a worship leader flip through a Planning
Center Services plan's songs from one device (phone/iPad) while a second
device (wall projector browser) shows the current song's full lyrics.

There is no dependency on Planning Center Music Stand's own internal
"Sessions" feature -- this is a fully independent remote control, driven by
the documented public Planning Center API, running on your own local
network.

Two pages:
    /remote   the leader's controller: Prev/Next buttons + tap-to-jump list
    /display  the wall-facing display: large centered lyrics, no chords

Both pages poll a small JSON endpoint every couple of seconds; there's no
websocket/push machinery, which keeps this simple and robust on a church
WiFi network.

Usage:
    uv run remote_display.py                       # nearest upcoming plan
    uv run remote_display.py --date 2024-08-04
    uv run remote_display.py --plan-id 12345 --service-type-id 6789
    uv run remote_display.py --port 8000

Then, on the same local network:
    Leader's device:  http://<this-machine-ip>:8000/remote
    Wall display:     http://<this-machine-ip>:8000/display

Requires PLANNING_CENTER_APP_ID and PLANNING_CENTER_SECRET in the
environment (or a .env file). See README.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from pco_client import (
    PlanningCenterError,
    build_session,
    collect_songs,
    find_plan_by_date,
    find_upcoming_plan,
    get_plan_by_id,
)

log = logging.getLogger("remote_display")

app = Flask(__name__)

_state_lock = threading.Lock()
_state: dict = {
    "plan_title": "",
    "songs": [],  # list of {"title": str, "ccli_number": str|None, "lyrics": str}
    "index": 0,
}
# Set once at startup so /api/reload can re-fetch the same plan.
_plan_ref: dict = {"session": None, "service_type_id": None, "plan_id": None}


def _load_songs(session, service_type_id: str, plan_id: str) -> list[dict]:
    songs = collect_songs(session, service_type_id, plan_id, include_pdf_links=False)
    return [
        {
            "title": s.title,
            "ccli_number": s.ccli_number,
            "lyrics": s.body(include_chords=False),
        }
        for s in songs
    ]


def _public_state() -> dict:
    with _state_lock:
        index = _state["index"]
        songs = _state["songs"]
        current = songs[index] if songs else None
        return {
            "plan_title": _state["plan_title"],
            "index": index,
            "total": len(songs),
            "current": current,
            "titles": [s["title"] for s in songs],
        }


# --------------------------------------------------------------------------
# API routes
# --------------------------------------------------------------------------


@app.route("/api/state")
def api_state():
    return jsonify(_public_state())


@app.route("/api/next", methods=["POST"])
def api_next():
    with _state_lock:
        if _state["songs"]:
            _state["index"] = min(_state["index"] + 1, len(_state["songs"]) - 1)
    return jsonify(_public_state())


@app.route("/api/prev", methods=["POST"])
def api_prev():
    with _state_lock:
        _state["index"] = max(_state["index"] - 1, 0)
    return jsonify(_public_state())


@app.route("/api/goto/<int:index>", methods=["POST"])
def api_goto(index: int):
    with _state_lock:
        if _state["songs"]:
            _state["index"] = max(0, min(index, len(_state["songs"]) - 1))
    return jsonify(_public_state())


@app.route("/api/reload", methods=["POST"])
def api_reload():
    session = _plan_ref["session"]
    service_type_id = _plan_ref["service_type_id"]
    plan_id = _plan_ref["plan_id"]
    try:
        songs = _load_songs(session, service_type_id, plan_id)
    except PlanningCenterError as exc:
        return jsonify({"error": str(exc)}), 502
    with _state_lock:
        _state["songs"] = songs
        _state["index"] = 0
    log.info("Reloaded plan from Planning Center: %d song(s).", len(songs))
    return jsonify(_public_state())


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

_DISPLAY_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Lyrics Display</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html, body {
    margin: 0; padding: 0; height: 100%;
    background: #000; color: #fff;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    overflow: hidden;
  }
  #wrap {
    height: 100vh; width: 100vw;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    box-sizing: border-box; padding: 4vh 6vw;
    text-align: center;
  }
  #title {
    position: fixed; top: 1.5vh; left: 0; right: 0;
    text-align: center; font-size: 2.2vh; letter-spacing: 0.05em;
    text-transform: uppercase; color: #888;
  }
  #lyrics {
    white-space: pre-wrap;
    line-height: 1.35;
    font-weight: 600;
  }
  #empty {
    color: #555; font-size: 3vh;
  }
</style>
</head>
<body>
  <div id="title"></div>
  <div id="wrap"><div id="lyrics"></div></div>
  <div id="empty" style="display:none">Waiting for the leader to select a song&hellip;</div>

<script>
let lastKey = null;

function fitText() {
  const el = document.getElementById('lyrics');
  const wrap = document.getElementById('wrap');
  if (!el.textContent.trim()) return;
  let size = 12; // vh, starting guess
  el.style.fontSize = size + 'vh';
  // Shrink until it fits both dimensions, or we hit a sane floor.
  let guard = 0;
  while (guard++ < 40 && size > 1.5 &&
         (el.scrollHeight > wrap.clientHeight || el.scrollWidth > wrap.clientWidth)) {
    size -= 0.4;
    el.style.fontSize = size + 'vh';
  }
}

async function poll() {
  try {
    const res = await fetch('/api/state', {cache: 'no-store'});
    const data = await res.json();
    const titleEl = document.getElementById('title');
    const lyricsEl = document.getElementById('lyrics');
    const emptyEl = document.getElementById('empty');

    titleEl.textContent = data.plan_title || '';

    if (!data.current) {
      lyricsEl.style.display = 'none';
      emptyEl.style.display = 'block';
      lastKey = null;
      return;
    }

    const key = data.index + ':' + data.current.title;
    emptyEl.style.display = 'none';
    lyricsEl.style.display = 'block';
    if (key !== lastKey) {
      lyricsEl.textContent = data.current.lyrics;
      fitText();
      lastKey = key;
    }
  } catch (e) {
    // Transient network hiccup -- just try again next tick.
  }
}

window.addEventListener('resize', fitText);
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
  html, body {
    margin: 0; padding: 0;
    background: #111; color: #fff;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    height: 100%;
  }
  #plan-title {
    padding: 10px 16px 0; color: #999; font-size: 14px;
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  #current {
    padding: 4px 16px 14px; font-size: 22px; font-weight: 700;
    border-bottom: 1px solid #333;
  }
  #buttons {
    display: flex; gap: 10px; padding: 14px 16px;
  }
  button.nav {
    flex: 1; font-size: 22px; padding: 22px 0; border: none; border-radius: 12px;
    background: #2d6cdf; color: #fff; font-weight: 700;
  }
  button.nav:active { background: #1d4fa8; }
  #reload {
    display: block; margin: 0 16px 14px; padding: 10px; width: calc(100% - 32px);
    background: #333; color: #ccc; border: none; border-radius: 10px; font-size: 14px;
  }
  #list { list-style: none; margin: 0; padding: 0 0 40px; }
  #list li {
    padding: 16px 20px; border-bottom: 1px solid #222; font-size: 18px;
  }
  #list li.active { background: #2d6cdf; font-weight: 700; }
  #list li .ccli { color: #999; font-size: 13px; margin-left: 8px; }
</style>
</head>
<body>
  <div id="plan-title"></div>
  <div id="current">Loading&hellip;</div>
  <div id="buttons">
    <button class="nav" id="prev">&larr; Prev</button>
    <button class="nav" id="next">Next &rarr;</button>
  </div>
  <button id="reload">Reload plan from Planning Center</button>
  <ul id="list"></ul>

<script>
let state = null;

function render() {
  if (!state) return;
  document.getElementById('plan-title').textContent = state.plan_title || '';
  document.getElementById('current').textContent = state.current
    ? (state.index + 1) + ' / ' + state.total + '  ' + state.current.title
    : 'No songs loaded';

  const list = document.getElementById('list');
  list.innerHTML = '';
  state.titles.forEach((title, i) => {
    const li = document.createElement('li');
    li.textContent = title;
    if (i === state.index) li.className = 'active';
    li.addEventListener('click', () => goto(i));
    list.appendChild(li);
  });
}

async function refresh() {
  const res = await fetch('/api/state', {cache: 'no-store'});
  state = await res.json();
  render();
}

async function post(path) {
  const res = await fetch(path, {method: 'POST'});
  state = await res.json();
  render();
}

document.getElementById('prev').addEventListener('click', () => post('/api/prev'));
document.getElementById('next').addEventListener('click', () => post('/api/next'));
document.getElementById('reload').addEventListener('click', () => post('/api/reload'));
function goto(i) { post('/api/goto/' + i); }

document.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowRight' || e.key === ' ') post('/api/next');
  if (e.key === 'ArrowLeft') post('/api/prev');
});

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""

_INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Song Remote</title></head>
<body style="font-family:sans-serif;padding:2em;">
<h1>Planning Center Song Remote</h1>
<p><a href="/remote">Leader remote &rarr;</a></p>
<p><a href="/display">Wall display &rarr;</a></p>
</body></html>
"""


@app.route("/")
def index():
    return _INDEX_HTML


@app.route("/display")
def display_page():
    return _DISPLAY_HTML


@app.route("/remote")
def remote_page():
    return _REMOTE_HTML


# --------------------------------------------------------------------------
# CLI / startup
# --------------------------------------------------------------------------


def _lan_ip() -> str:
    """Best-effort guess at this machine's LAN IP, for the startup banner."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local web remote + wall display for a Planning Center Services plan."
    )
    plan_selector = parser.add_mutually_exclusive_group()
    plan_selector.add_argument(
        "--date", type=str, default=None, help="Date of the plan to load, e.g. 2024-08-04."
    )
    plan_selector.add_argument(
        "--plan-id", type=str, default=None, help="Load a specific plan by its Planning Center id."
    )
    parser.add_argument(
        "--service-type-id",
        type=str,
        default=os.environ.get("PLANNING_CENTER_SERVICE_TYPE_ID") or None,
        help="Restrict lookups to one Service Type id.",
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Bind address (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    load_dotenv()
    app_id = os.environ.get("PLANNING_CENTER_APP_ID")
    secret = os.environ.get("PLANNING_CENTER_SECRET")
    if not app_id or not secret:
        log.error(
            "Missing credentials. Set PLANNING_CENTER_APP_ID and PLANNING_CENTER_SECRET "
            "in your environment or .env file (see .env.example)."
        )
        return 1

    session = build_session(app_id, secret)

    try:
        if args.plan_id:
            service_type_id, plan = get_plan_by_id(session, args.plan_id, args.service_type_id)
        elif args.date:
            try:
                target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
            except ValueError:
                log.error("Invalid --date %r; expected format YYYY-MM-DD.", args.date)
                return 1
            service_type_id, plan = find_plan_by_date(session, target_date, args.service_type_id)
        else:
            service_type_id, plan = find_upcoming_plan(session, args.service_type_id)

        plan_id = plan["id"]
        plan_title = (
            plan["attributes"].get("title")
            or plan["attributes"].get("series_title")
            or plan["attributes"].get("dates")
            or "Plan"
        )
        log.info("Using plan %r (id=%s, service_type=%s)", plan_title, plan_id, service_type_id)

        songs = _load_songs(session, service_type_id, plan_id)
        log.info("Loaded %d song(s).", len(songs))

    except PlanningCenterError as exc:
        log.error(str(exc))
        return 1

    _plan_ref.update(session=session, service_type_id=service_type_id, plan_id=plan_id)
    with _state_lock:
        _state["plan_title"] = plan_title
        _state["songs"] = songs
        _state["index"] = 0

    ip = _lan_ip()
    print(f"\nLeader remote:  http://{ip}:{args.port}/remote")
    print(f"Wall display:   http://{ip}:{args.port}/display")
    print("(Both devices need to be on the same local network as this machine.)\n")

    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
