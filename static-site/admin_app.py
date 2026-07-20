#!/usr/bin/env python3
"""
admin_app.py

Local admin UI for the static site (see generate_static_site.py): lets
someone manually regenerate it from Planning Center and switch what's
actually being served between the generated lyrics page and a "come back
Sunday" placeholder -- plus a rule-based scheduler (see scheduler.py) that
does the same open/close automatically, driven by each configured service
type's actual plan data for today.

This exists because song lyrics are only licensed (via CCLI) to be
displayed for the service they're used in -- leaving the generated page up
publicly all week is exactly what the open/closed toggle here prevents.
Automation never overrides that: it only opens when a plan looks real
enough to trust (see scheduler.evaluate_rule's guardrail), and manual
actions always win over automation immediately (see _tick's state machine
below).

Layout on the shared data directory (see DATA_DIR):
    <DATA_DIR>/site/index.html      latest output of generate_static_site.py
    <DATA_DIR>/site/index.plan.json which plan site/index.html was generated from
    <DATA_DIR>/current/index.html   what the web-facing server actually serves
    <DATA_DIR>/state.txt             "open" or "closed"
    <DATA_DIR>/open_plan.json        which plan is live right now (if state=="open")
                                      and whether a human or automation opened it
    <DATA_DIR>/rules.json            configured automation rules (see /settings)

Regenerating always refreshes site/index.html; it only touches
current/index.html (what's actually public) if the site is currently open
-- and, if a specific plan is currently live (open_plan.json), it refreshes
*that* plan rather than falling back to "nearest upcoming", so a manual
"Regenerate now" click mid-service can't silently swap in the wrong plan.

Gated by HTTP Basic Auth (ADMIN_USERNAME / ADMIN_PASSWORD, see
.env.example) -- it can trigger regeneration and control whether
copyrighted lyrics are publicly served, so it refuses to start without a
password set. Basic Auth itself isn't encrypted, so still keep this behind
TLS (a reverse proxy) rather than exposing it directly. See the
Containerized deployment section of README.md.

Usage:
    uv run static-site/admin_app.py --port 9000

Requires PLANNING_CENTER_APP_ID, PLANNING_CENTER_SECRET, and ADMIN_PASSWORD
in the environment (or a .env file). Optional: SCHEDULER_POLL_SECONDS
(default 300), SCHEDULER_TIMEZONE (default Europe/Stockholm) -- see
.env.example.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from flask import Flask, Response, redirect, request, url_for

from pco_client import PlanningCenterError, build_session, list_service_types
from scheduler import OpenPlan, RuleStore, clear_open_plan, evaluate_rule, read_open_plan, write_open_plan

log = logging.getLogger("admin_app")

# Loaded at import time (rather than in main(), like the other scripts in
# this repo) because DATA_DIR/TITLE_PREFIX/ADMIN_* below are module-level
# constants read by route functions -- they need .env applied before those
# assignments run, not after.
load_dotenv()

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TITLE_PREFIX = os.environ.get("PAGE_TITLE_PREFIX", "Lovsång Brokyrkan")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Scheduler config. Timezone matters because Planning Center's API returns
# UTC timestamps, but "today" and window sanity-checks need to be computed
# in local time -- naively using UTC "today" would roll over at the wrong
# local moment (e.g. an evening service could get compared against tomorrow).
SCHEDULER_POLL_SECONDS = int(os.environ.get("SCHEDULER_POLL_SECONDS", "300"))
LOCAL_TZ = ZoneInfo(os.environ.get("SCHEDULER_TIMEZONE", "Europe/Stockholm"))

# Built eagerly (like the other module-level constants above) rather than in
# main(): safe even if credentials are missing at import time since
# build_session() just stores them for later requests, it doesn't validate
# anything -- main() still refuses to *start* without them (see below).
SESSION = build_session(os.environ.get("PLANNING_CENTER_APP_ID") or "", os.environ.get("PLANNING_CENTER_SECRET") or "")
RULE_STORE = RuleStore(DATA_DIR)

# Guards every mutation of state.txt/open_plan.json/rules.json/current/index.html:
# app.run(..., threaded=True) means each HTTP request runs on its own thread
# concurrently with the background scheduler thread, and all of them touch
# this same on-disk state.
_lock = threading.Lock()


@app.before_request
def _require_auth():
    auth = request.authorization
    if (
        not auth
        or not secrets.compare_digest(auth.username or "", ADMIN_USERNAME)
        or not secrets.compare_digest(auth.password or "", ADMIN_PASSWORD)
    ):
        return Response(
            "Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="admin"'}
        )

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<title>{title_prefix}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    text-align: center; color: #1a1a1a; background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #eee; background: #111; }} }}
  p {{ font-size: 1.2rem; padding: 0 1.5rem; }}
</style>
</head>
<body>
<p>Sidan visas bara under gudstjänsten.<br>Välkommen tillbaka på söndag!</p>
</body>
</html>
"""

_STATUS_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Site Admin</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0 auto; max-width: 32em; padding: 2rem 1.25rem 4rem;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.5; color: #1a1a1a; background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #eee; background: #111; }} }}
  h1 {{ font-size: 1.4rem; }}
  .badge {{
    display: inline-block; padding: 0.2em 0.7em; border-radius: 1em;
    font-weight: 700; font-size: 0.9rem; color: #fff;
  }}
  .badge.open {{ background: #2a9d4a; }}
  .badge.closed {{ background: #999; }}
  .meta {{ color: #777; font-size: 0.9rem; }}
  .error {{ color: #d33; }}
  form {{ display: inline; }}
  button {{
    font-size: 1rem; padding: 0.6em 1.2em; margin: 0.3em 0.5em 0.3em 0;
    border: none; border-radius: 8px; background: #2d6cdf; color: #fff; font-weight: 700;
  }}
  button.secondary {{ background: #555; }}
  a {{ color: #2d6cdf; }}
</style>
</head>
<body>
<h1>{title_prefix} &mdash; admin</h1>
<p><span class="badge {state}">{state_label}</span></p>
<p class="meta">Last generated: {generated_at}</p>
{open_plan_html}
{error_html}
<form method="post" action="{regenerate_url}"><button>Regenerate now</button></form>
<form method="post" action="{open_url}"><button class="secondary">Open (serve lyrics)</button></form>
<form method="post" action="{close_url}"><button class="secondary">Close (serve placeholder)</button></form>
<p class="meta">Automation: {enabled_rule_count}/{total_rule_count} rule(s) enabled. <a href="{settings_url}">Manage rules &rarr;</a></p>
</body>
</html>
"""

_SETTINGS_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Site Admin &mdash; automation settings</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0 auto; max-width: 46em; padding: 2rem 1.25rem 4rem;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.5; color: #1a1a1a; background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #eee; background: #111; }}
    th, td {{ border-bottom-color: #333; }}
  }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  a {{ color: #2d6cdf; }}
  .error {{ color: #d33; }}
  table {{ width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: 0.85rem; }}
  th, td {{ text-align: left; padding: 0.4em 0.6em; border-bottom: 1px solid #ddd; vertical-align: top; }}
  .enabled {{ color: #2a9d4a; font-weight: 700; }}
  .disabled {{ color: #999; }}
  form {{ display: inline; }}
  button {{
    font-size: 0.9rem; padding: 0.4em 0.9em; margin: 0.15em 0.3em 0.15em 0;
    border: none; border-radius: 6px; background: #555; color: #fff; font-weight: 700;
  }}
  select, input[type=text] {{ font-size: 1rem; padding: 0.4em; margin: 0.2em 0.5em 0.2em 0; }}
  button[type=submit] {{ background: #2d6cdf; }}
</style>
</head>
<body>
<h1>{title_prefix} &mdash; automation settings</h1>
<p><a href="{index_url}">&larr; Back to status</a></p>
{error_html}
<table>
<tr><th>Service type</th><th>Enabled</th><th>Last checked</th><th>Last action</th><th>Last plan</th><th>Last window</th><th>Last reason</th><th></th></tr>
{rows_html}
</table>
<h2>Add a rule</h2>
<form method="post" action="{add_url}">
  <select name="service_type_id" required>
    <option value="" disabled selected>Choose a service type&hellip;</option>
    {service_type_options}
  </select>
  <input type="text" name="title_prefix" placeholder="Title prefix (optional)">
  <button type="submit">Add rule</button>
</form>
</body>
</html>
"""

_RULE_ROW_TEMPLATE = """<tr>
  <td>{service_type_name}</td>
  <td class="{enabled_class}">{enabled_label}</td>
  <td>{last_checked_at}</td>
  <td>{last_action}</td>
  <td>{last_plan_title}</td>
  <td>{last_window}</td>
  <td>{last_reason}</td>
  <td>
    <form method="post" action="{toggle_url}"><button>{toggle_label}</button></form>
    <form method="post" action="{delete_url}"><button>Delete</button></form>
  </td>
</tr>"""


def _paths(data_dir: Path) -> dict[str, Path]:
    return {
        "site": data_dir / "site" / "index.html",
        "site_plan": data_dir / "site" / "index.plan.json",
        "current": data_dir / "current" / "index.html",
        "state": data_dir / "state.txt",
    }


def _read_state(data_dir: Path) -> str:
    state_path = _paths(data_dir)["state"]
    if state_path.exists():
        value = state_path.read_text(encoding="utf-8").strip()
        if value in ("open", "closed"):
            return value
    return "closed"


def _write_state(data_dir: Path, state: str) -> None:
    _paths(data_dir)["state"].write_text(state, encoding="utf-8")


def _read_site_plan(data_dir: Path) -> Optional[dict]:
    """Which plan site/index.html was last generated from, if any."""
    path = _paths(data_dir)["site_plan"]
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _apply_state(data_dir: Path, state: str) -> None:
    """Write the right content into current/index.html for the given state."""
    p = _paths(data_dir)
    p["current"].parent.mkdir(parents=True, exist_ok=True)
    if state == "open":
        if not p["site"].exists():
            raise RuntimeError("No generated site yet -- regenerate first.")
        p["current"].write_text(p["site"].read_text(encoding="utf-8"), encoding="utf-8")
    else:
        p["current"].write_text(
            _PLACEHOLDER_HTML.format(title_prefix=escape(TITLE_PREFIX)), encoding="utf-8"
        )


def _regenerate(
    data_dir: Path,
    service_type_id: Optional[str] = None,
    plan_id: Optional[str] = None,
    title_prefix: Optional[str] = None,
) -> None:
    """Run generate_static_site.py. Without service_type_id/plan_id it falls
    back to "nearest upcoming plan" (generate_static_site.py's own default)
    -- callers that need a *specific* plan (automation, or a manual
    Regenerate click while that plan is live) must pass both explicitly."""
    p = _paths(data_dir)
    p["site"].parent.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).parent / "generate_static_site.py"
    cmd = [
        sys.executable,
        str(script),
        "-o",
        str(p["site"]),
        "--title-prefix",
        title_prefix or TITLE_PREFIX,
    ]
    if service_type_id:
        cmd += ["--service-type-id", service_type_id]
    if plan_id:
        cmd += ["--plan-id", plan_id]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(result.stderr.strip())
        # generate_static_site.py logs its actual failure reason as the last
        # line via log.error(); the rest is INFO noise we don't want to
        # cram into a one-line error banner/redirect URL.
        stderr_lines = result.stderr.strip().splitlines()
        message = stderr_lines[-1] if stderr_lines else "generate_static_site.py failed"
        raise RuntimeError(message)
    log.info(result.stdout.strip())


# --------------------------------------------------------------------------
# Scheduler tick -- the automation state machine.
#
#   - Manual actions (open_site/close_site routes) always win immediately
#     and clear any tracked automation window, so automation never fights a
#     human for that instance of a service.
#   - Automation only OPENS when the site is closed and nothing is already
#     tracked as live (covers both "closed, nothing happened yet" and, after
#     the self-heal below, "closed, stale record").
#   - Automation only auto-CLOSES a window it opened itself -- never
#     something a human opened manually.
# --------------------------------------------------------------------------


def _tick(data_dir: Path) -> None:
    with _lock:
        state = _read_state(data_dir)
        open_plan = read_open_plan(data_dir)

        # Self-heal: closed but a stale open_plan record lingers (e.g. a
        # crash mid-write, or a manual edit of state.txt). Clear it before
        # doing anything else so a stale record can't confuse the auto-close
        # timer or the "who opened this" logic below.
        if state != "open" and open_plan is not None:
            log.warning("Clearing stale open_plan.json record while site is closed.")
            clear_open_plan(data_dir)
            open_plan = None

        now = datetime.now(timezone.utc)

        if state == "open" and open_plan is not None and open_plan.opened_by == "automation":
            ends_at = datetime.fromisoformat(open_plan.window_ends_at) if open_plan.window_ends_at else None
            if ends_at is not None and now >= ends_at:
                log.info(
                    "Auto-closing %r (rule %s): window ended at %s",
                    open_plan.plan_title,
                    open_plan.rule_id,
                    ends_at,
                )
                _apply_state(data_dir, "closed")
                _write_state(data_dir, "closed")
                clear_open_plan(data_dir)
                state, open_plan = "closed", None

        if state == "open" or open_plan is not None:
            return  # site is already live (manually or via automation) -- leave it alone

        today_local = now.astimezone(LOCAL_TZ).date()
        for rule in RULE_STORE.load():
            if not rule.enabled:
                continue

            evaluation = evaluate_rule(SESSION, rule, today_local, LOCAL_TZ)
            action_taken = "waiting"

            if not evaluation.ok:
                action_taken = "skipped"
                log.info("Rule %s (%s): skipped -- %s", rule.id, rule.service_type_name, evaluation.reason)
            elif evaluation.window_starts_at <= now < evaluation.window_ends_at:
                try:
                    _regenerate(
                        data_dir,
                        service_type_id=rule.service_type_id,
                        plan_id=evaluation.plan_id,
                        title_prefix=rule.title_prefix,
                    )
                    _apply_state(data_dir, "open")
                    _write_state(data_dir, "open")
                    write_open_plan(
                        data_dir,
                        OpenPlan(
                            service_type_id=rule.service_type_id,
                            plan_id=evaluation.plan_id,
                            plan_title=evaluation.plan_title,
                            opened_by="automation",
                            rule_id=rule.id,
                            window_ends_at=evaluation.window_ends_at.isoformat(),
                        ),
                    )
                    action_taken = "opened"
                    log.info(
                        "Auto-opened %r via rule %s (%s)", evaluation.plan_title, rule.id, rule.service_type_name
                    )
                except RuntimeError as exc:
                    action_taken = "error"
                    log.error("Automation failed to open rule %s: %s", rule.id, exc)

            RULE_STORE.update_bookkeeping(
                rule.id,
                last_checked_at=now.isoformat(),
                last_action=action_taken,
                last_plan_title=evaluation.plan_title,
                last_window_starts_at=evaluation.window_starts_at.isoformat() if evaluation.window_starts_at else None,
                last_window_ends_at=evaluation.window_ends_at.isoformat() if evaluation.window_ends_at else None,
                last_reason=evaluation.reason,
            )

            if action_taken == "opened":
                break  # only one automation window can be live at a time


def _scheduler_loop(data_dir: Path, poll_seconds: int) -> None:
    while True:
        time.sleep(poll_seconds)
        try:
            _tick(data_dir)
        except Exception:
            # A transient network blip must not silently kill the scheduler
            # thread forever -- log and try again next interval instead of
            # letting the exception propagate out of the thread.
            log.exception("Scheduler tick failed; will retry in %ss.", poll_seconds)


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.route("/")
def index():
    state = _read_state(DATA_DIR)
    site_path = _paths(DATA_DIR)["site"]
    generated_at = (
        datetime.fromtimestamp(site_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if site_path.exists()
        else "never"
    )
    error = request.args.get("error")
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""

    open_plan = read_open_plan(DATA_DIR) if state == "open" else None
    if open_plan is None:
        open_plan_html = ""
    elif open_plan.opened_by == "automation":
        open_plan_html = f'<p class="meta">Opened automatically for &ldquo;{escape(open_plan.plan_title)}&rdquo;.</p>'
    else:
        open_plan_html = f'<p class="meta">Opened manually for &ldquo;{escape(open_plan.plan_title)}&rdquo;.</p>'

    rules = RULE_STORE.load()
    return _STATUS_TEMPLATE.format(
        title_prefix=escape(TITLE_PREFIX),
        state=state,
        state_label=state.upper(),
        generated_at=escape(generated_at),
        open_plan_html=open_plan_html,
        error_html=error_html,
        regenerate_url=url_for("regenerate"),
        open_url=url_for("open_site"),
        close_url=url_for("close_site"),
        settings_url=url_for("settings"),
        enabled_rule_count=sum(1 for r in rules if r.enabled),
        total_rule_count=len(rules),
    )


@app.route("/regenerate", methods=["POST"])
def regenerate():
    try:
        with _lock:
            service_type_id = plan_id = None
            if _read_state(DATA_DIR) == "open":
                open_plan = read_open_plan(DATA_DIR)
                if open_plan:
                    # Refresh the plan that's actually live, not whatever
                    # "nearest upcoming" would resolve to -- otherwise a
                    # manual Regenerate click during e.g. a Special Events
                    # service could silently swap in next Sunday's plan.
                    service_type_id, plan_id = open_plan.service_type_id, open_plan.plan_id
            _regenerate(DATA_DIR, service_type_id=service_type_id, plan_id=plan_id)
            if _read_state(DATA_DIR) == "open":
                _apply_state(DATA_DIR, "open")
        log.info("Regenerated site.")
        return redirect(url_for("index"))
    except RuntimeError as exc:
        log.error("Regenerate failed: %s", exc)
        return redirect(url_for("index", error=str(exc)))


@app.route("/open", methods=["POST"])
def open_site():
    try:
        with _lock:
            _apply_state(DATA_DIR, "open")
            _write_state(DATA_DIR, "open")
            site_plan = _read_site_plan(DATA_DIR)
            if site_plan:
                write_open_plan(
                    DATA_DIR,
                    OpenPlan(
                        service_type_id=site_plan["service_type_id"],
                        plan_id=site_plan["plan_id"],
                        plan_title=site_plan["plan_title"],
                        opened_by="manual",
                    ),
                )
            else:
                clear_open_plan(DATA_DIR)
    except RuntimeError as exc:
        log.error("Could not open site: %s", exc)
        return redirect(url_for("index", error=str(exc)))
    return redirect(url_for("index"))


@app.route("/close", methods=["POST"])
def close_site():
    with _lock:
        _apply_state(DATA_DIR, "closed")
        _write_state(DATA_DIR, "closed")
        clear_open_plan(DATA_DIR)
    return redirect(url_for("index"))


@app.route("/settings")
def settings():
    rules = RULE_STORE.load()
    error = request.args.get("error")
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""

    rows = []
    for r in rules:
        if r.last_window_starts_at and r.last_window_ends_at:
            starts_local = datetime.fromisoformat(r.last_window_starts_at).astimezone(LOCAL_TZ)
            ends_local = datetime.fromisoformat(r.last_window_ends_at).astimezone(LOCAL_TZ)
            window = f"{starts_local.strftime('%Y-%m-%d %H:%M')}&ndash;{ends_local.strftime('%H:%M')}"
        else:
            window = "&mdash;"
        rows.append(
            _RULE_ROW_TEMPLATE.format(
                service_type_name=escape(r.service_type_name),
                enabled_class="enabled" if r.enabled else "disabled",
                enabled_label="ON" if r.enabled else "OFF",
                last_checked_at=escape(r.last_checked_at or "never"),
                last_action=escape(r.last_action or "--"),
                last_plan_title=escape(r.last_plan_title or "--"),
                last_window=window,
                last_reason=escape(r.last_reason or "--"),
                toggle_url=url_for("toggle_rule", rule_id=r.id),
                delete_url=url_for("delete_rule", rule_id=r.id),
                toggle_label="Disable" if r.enabled else "Enable",
            )
        )
    rows_html = "\n".join(rows) if rows else '<tr><td colspan="8"><em>No rules configured yet.</em></td></tr>'

    try:
        service_type_options = "\n".join(
            f'<option value="{escape(st["id"])}">{escape(st["attributes"].get("name") or st["id"])}</option>'
            for st in list_service_types(SESSION)
        )
    except PlanningCenterError as exc:
        service_type_options = ""
        error_html += f'<p class="error">Could not load service types from Planning Center: {escape(str(exc))}</p>'

    return _SETTINGS_TEMPLATE.format(
        title_prefix=escape(TITLE_PREFIX),
        index_url=url_for("index"),
        error_html=error_html,
        rows_html=rows_html,
        add_url=url_for("add_rule"),
        service_type_options=service_type_options,
    )


@app.route("/settings/rules", methods=["POST"])
def add_rule():
    service_type_id = request.form.get("service_type_id", "").strip()
    title_prefix = request.form.get("title_prefix", "").strip() or None
    if not service_type_id:
        return redirect(url_for("settings", error="Choose a service type."))

    try:
        service_types = {st["id"]: st["attributes"].get("name") for st in list_service_types(SESSION)}
    except PlanningCenterError as exc:
        return redirect(url_for("settings", error=str(exc)))

    service_type_name = service_types.get(service_type_id, service_type_id)
    with _lock:
        RULE_STORE.add(service_type_id, service_type_name, title_prefix)
    return redirect(url_for("settings"))


@app.route("/settings/rules/<rule_id>/toggle", methods=["POST"])
def toggle_rule(rule_id: str):
    with _lock:
        rule = RULE_STORE.get(rule_id)
        if rule:
            RULE_STORE.set_enabled(rule_id, not rule.enabled)
    return redirect(url_for("settings"))


@app.route("/settings/rules/<rule_id>/delete", methods=["POST"])
def delete_rule(rule_id: str):
    with _lock:
        RULE_STORE.delete(rule_id)
    return redirect(url_for("settings"))


# --------------------------------------------------------------------------
# CLI / startup
# --------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Admin UI for the generated static lyrics site.")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Bind address (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=9000, help="Bind port (default: 9000).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not os.environ.get("PLANNING_CENTER_APP_ID") or not os.environ.get("PLANNING_CENTER_SECRET"):
        log.error(
            "Missing credentials. Set PLANNING_CENTER_APP_ID and PLANNING_CENTER_SECRET "
            "in your environment or .env file (see .env.example)."
        )
        return 1
    if not ADMIN_PASSWORD:
        log.error(
            "Missing ADMIN_PASSWORD. Set it in your environment or .env file (see .env.example) "
            "-- this UI controls whether copyrighted lyrics are publicly served, so it refuses "
            "to start unauthenticated."
        )
        return 1

    # Fresh data volume has no current/index.html yet -- make sure the
    # placeholder (or last-known site) is in place before serving starts.
    try:
        _apply_state(DATA_DIR, _read_state(DATA_DIR))
    except RuntimeError as exc:
        log.warning("Falling back to closed placeholder on startup: %s", exc)
        _apply_state(DATA_DIR, "closed")
        _write_state(DATA_DIR, "closed")

    # Run one tick immediately (not just on the first `poll_seconds` timer)
    # so a container restart mid-service catches up right away instead of
    # waiting up to a full interval before re-evaluating/auto-closing.
    try:
        _tick(DATA_DIR)
    except Exception:
        log.exception("Initial scheduler tick failed.")
    threading.Thread(target=_scheduler_loop, args=(DATA_DIR, SCHEDULER_POLL_SECONDS), daemon=True).start()

    print(f"\nAdmin UI: http://localhost:{args.port}/\n")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
