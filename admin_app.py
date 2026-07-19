#!/usr/bin/env python3
"""
admin_app.py

Small local admin UI for the static site (see generate_static_site.py):
lets someone manually regenerate it from Planning Center and switch what's
actually being served between the generated lyrics page and a "come back
Sunday" placeholder.

This exists because song lyrics are only licensed (via CCLI) to be
displayed for the service they're used in -- leaving the generated page up
publicly all week is exactly what the open/closed toggle here prevents.
There's no schedule/automation yet, just a manual switch.

Layout on the shared data directory (see DATA_DIR):
    <DATA_DIR>/site/index.html     latest output of generate_static_site.py
    <DATA_DIR>/current/index.html  what the web-facing server actually serves
    <DATA_DIR>/state.txt           "open" or "closed"

Regenerating always refreshes site/index.html; it only touches
current/index.html (what's actually public) if the site is currently open.

Not authenticated -- like remote_display.py, this is meant to run on a
private network / behind your own reverse proxy, not exposed directly. See
the Containerized deployment section of README.md.

Usage:
    uv run admin_app.py --port 9000

Requires PLANNING_CENTER_APP_ID and PLANNING_CENTER_SECRET in the
environment (or a .env file), same as generate_static_site.py.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from flask import Flask, redirect, request, url_for

log = logging.getLogger("admin_app")

app = Flask(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TITLE_PREFIX = os.environ.get("PAGE_TITLE_PREFIX", "Lovsång Brokyrkan")

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
</style>
</head>
<body>
<h1>{title_prefix} &mdash; admin</h1>
<p><span class="badge {state}">{state_label}</span></p>
<p class="meta">Last generated: {generated_at}</p>
{error_html}
<form method="post" action="{regenerate_url}"><button>Regenerate now</button></form>
<form method="post" action="{open_url}"><button class="secondary">Open (serve lyrics)</button></form>
<form method="post" action="{close_url}"><button class="secondary">Close (serve placeholder)</button></form>
</body>
</html>
"""


def _paths(data_dir: Path) -> dict[str, Path]:
    return {
        "site": data_dir / "site" / "index.html",
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


def _regenerate(data_dir: Path) -> None:
    p = _paths(data_dir)
    p["site"].parent.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).parent / "generate_static_site.py"
    cmd = [sys.executable, str(script), "-o", str(p["site"]), "--title-prefix", TITLE_PREFIX]
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
    return _STATUS_TEMPLATE.format(
        title_prefix=escape(TITLE_PREFIX),
        state=state,
        state_label=state.upper(),
        generated_at=escape(generated_at),
        error_html=error_html,
        regenerate_url=url_for("regenerate"),
        open_url=url_for("open_site"),
        close_url=url_for("close_site"),
    )


@app.route("/regenerate", methods=["POST"])
def regenerate():
    try:
        _regenerate(DATA_DIR)
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
        _apply_state(DATA_DIR, "open")
        _write_state(DATA_DIR, "open")
    except RuntimeError as exc:
        log.error("Could not open site: %s", exc)
        return redirect(url_for("index", error=str(exc)))
    return redirect(url_for("index"))


@app.route("/close", methods=["POST"])
def close_site():
    _apply_state(DATA_DIR, "closed")
    _write_state(DATA_DIR, "closed")
    return redirect(url_for("index"))


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

    load_dotenv()
    if not os.environ.get("PLANNING_CENTER_APP_ID") or not os.environ.get("PLANNING_CENTER_SECRET"):
        log.error(
            "Missing credentials. Set PLANNING_CENTER_APP_ID and PLANNING_CENTER_SECRET "
            "in your environment or .env file (see .env.example)."
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

    print(f"\nAdmin UI: http://localhost:{args.port}/\n")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
