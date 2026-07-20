#!/usr/bin/env python3
"""
generate_static_site.py

Fetch the songs (and lyrics) from a Planning Center Services plan and write
them out as a single self-contained static HTML page -- meant to be served
as-is by a plain static file server (no build step, no external assets/CDN,
no JavaScript) and regenerated on a schedule (e.g. a nightly cron/systemd
timer) so the page always reflects the nearest upcoming plan.

This is a sibling of notion-export/update_lyrics.py (which produces a Notion-ready
Markdown file for a manual copy/paste workflow) -- use this one instead when
you want the plan's songs reachable directly at a URL.

PDF chord-chart links are intentionally never included here: they're
short-lived signed URLs (see pco_client.get_arrangement_pdf_url), and a page
that's regenerated once and then left up all day is exactly the case where
they're likely to go stale before anyone clicks them.

Usage:
    uv run static-site/generate_static_site.py                       # nearest upcoming plan
    uv run static-site/generate_static_site.py --date 2024-08-04
    uv run static-site/generate_static_site.py --plan-id 12345 --service-type-id 6789
    uv run static-site/generate_static_site.py --include-chords -o site/index.html
    uv run static-site/generate_static_site.py --list-service-types

Requires PLANNING_CENTER_APP_ID and PLANNING_CENTER_SECRET in the
environment (or a .env file). See README.md for how to create these.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from pco_client import (
    PlanningCenterError,
    SongLyrics,
    build_session,
    collect_songs,
    find_plan_by_date,
    find_upcoming_plan,
    get_plan_by_id,
    list_service_types,
    parse_pco_datetime,
)

log = logging.getLogger("generate_static_site")

_PAGE_TEMPLATE = """<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<title>{page_title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0 auto; max-width: 42em; padding: 2rem 1.25rem 4rem;
    font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.5; color: #1a1a1a; background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #eee; background: #111; }}
    .ccli {{ color: #999; }}
    .meta {{ color: #999; }}
    hr {{ border-top-color: #333; }}
  }}
  h1 {{ font-size: 1.6rem; margin: 0 0 0.25rem; }}
  .meta {{ color: #777; font-size: 0.9rem; margin: 0 0 2rem; }}
  details {{ margin: 1.25rem 0; }}
  summary {{ font-size: 1.25rem; cursor: pointer; padding: 0.15rem 0; }}
  .ccli {{ font-style: italic; color: #777; font-size: 0.85rem; font-weight: normal; }}
  pre {{ white-space: pre-wrap; font-family: inherit; font-size: 1rem; margin: 0.75rem 0 0; }}
  hr {{ margin: 1.25rem 0; border: none; border-top: 1px solid #ddd; }}
</style>
</head>
<body>
<h1>{page_title}</h1>
<p class="meta">Uppdaterad {generated_at}</p>
{body}
</body>
</html>
"""


def format_html(
    title_prefix: str,
    plan_date: date,
    songs: list[SongLyrics],
    include_chords: bool,
) -> str:
    """Build the full static HTML page."""
    page_title = f"{title_prefix} - {plan_date.isoformat()}"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not songs:
        body = "<p><em>Inga sånger hittades i denna plan.</em></p>"
    else:
        sections = []
        for song in songs:
            summary = escape(song.title)
            if song.ccli_number:
                summary += f' <span class="ccli">CCLI #{escape(str(song.ccli_number))}</span>'
            lines = [
                "<details>",
                f"<summary>{summary}</summary>",
                f"<pre>{escape(song.body(include_chords))}</pre>",
                "</details>",
            ]
            sections.append("\n".join(lines))
        body = "\n<hr>\n".join(sections)

    return _PAGE_TEMPLATE.format(page_title=escape(page_title), generated_at=generated_at, body=body)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a static HTML page of song lyrics from a Planning Center Services plan."
    )
    plan_selector = parser.add_mutually_exclusive_group()
    plan_selector.add_argument(
        "--date", type=str, default=None, help="Date of the plan to fetch, e.g. 2024-08-04."
    )
    plan_selector.add_argument(
        "--plan-id", type=str, default=None, help="Fetch a specific plan by its Planning Center id."
    )
    parser.add_argument(
        "--service-type-id",
        type=str,
        default=os.environ.get("PLANNING_CENTER_SERVICE_TYPE_ID") or None,
        help="Restrict lookups to one Service Type id. Defaults to $PLANNING_CENTER_SERVICE_TYPE_ID, "
        "or searches all service types if unset.",
    )
    parser.add_argument(
        "--include-chords",
        action="store_true",
        help="Include the chord chart instead of plain lyrics, where available.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="site/index.html",
        help="Output .html file path. Defaults to 'site/index.html'.",
    )
    parser.add_argument(
        "--title-prefix",
        type=str,
        default=os.environ.get("PAGE_TITLE_PREFIX", "Lovsång Brokyrkan"),
        help="Prefix for the generated page title. Defaults to $PAGE_TITLE_PREFIX.",
    )
    parser.add_argument(
        "--list-service-types",
        action="store_true",
        help="List available Service Types (id + name) and exit.",
    )
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
        if args.list_service_types:
            for st in list_service_types(session):
                print(f"{st['id']}\t{st['attributes'].get('name')}")
            return 0

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
        sort_date_raw = plan["attributes"].get("sort_date") or plan["attributes"].get("dates")
        plan_date = parse_pco_datetime(sort_date_raw).date() if sort_date_raw else date.today()
        log.info(
            "Using plan %r (id=%s, service_type=%s, date=%s)",
            plan["attributes"].get("title") or plan["attributes"].get("series_title"),
            plan_id,
            service_type_id,
            plan_date,
        )

        songs = collect_songs(session, service_type_id, plan_id, include_pdf_links=False)
        log.info("Found %d song(s).", len(songs))

        html = format_html(args.title_prefix, plan_date, songs, args.include_chords)

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")

        # Sidecar recording which exact plan this run fetched -- admin_app.py
        # reads it to know what it just regenerated, whether that was via an
        # explicit --plan-id (automation) or "nearest upcoming" (manual
        # default), so it can e.g. refresh the *same* plan on a manual
        # "Regenerate now" click while a service is live.
        plan_title = plan["attributes"].get("title") or plan["attributes"].get("series_title") or f"Plan {plan_id}"
        plan_info = {
            "service_type_id": service_type_id,
            "plan_id": plan_id,
            "plan_title": plan_title,
            "plan_date": plan_date.isoformat(),
        }
        output_path.with_suffix(".plan.json").write_text(json.dumps(plan_info), encoding="utf-8")

        print(f"\n✅ Wrote {len(songs)} song(s) to {output_path}.")
        return 0

    except PlanningCenterError as exc:
        log.error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
