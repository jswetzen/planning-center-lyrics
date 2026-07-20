#!/usr/bin/env python3
"""
update_lyrics.py

Fetch the songs (and their lyrics/chords) from a Planning Center Services
plan and write them out as a single Notion-ready Markdown file.

Notion auto-converts pasted Markdown into blocks (headings, paragraphs,
links, and <details>/<summary> HTML into collapsible toggles), so the
intended workflow is:

    1. Run this script to generate a .md file for a plan.
    2. Open/create the page in Notion.
    3. Paste the file's contents into the page.

No Notion API calls are made -- upload is manual by design.

Usage:
    uv run notion-export/update_lyrics.py                       # nearest upcoming plan
    uv run notion-export/update_lyrics.py --date 2024-08-04      # plan on a specific date
    uv run notion-export/update_lyrics.py --plan-id 12345 --service-type-id 6789
    uv run notion-export/update_lyrics.py --include-chords -o lovsang.md
    uv run notion-export/update_lyrics.py --list-service-types

Requires PLANNING_CENTER_APP_ID and PLANNING_CENTER_SECRET in the
environment (or a .env file). See README.md for how to create these.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
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
    get_plan_songbook_url,
    list_service_types,
)

log = logging.getLogger("update_lyrics")


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def format_markdown(
    title_prefix: str,
    plan_date: date,
    songs: list[SongLyrics],
    include_chords: bool,
    songbook_url: Optional[str] = None,
) -> str:
    """Build the full Notion-ready Markdown document."""
    page_title = f"{title_prefix} - {plan_date.isoformat()}"
    parts = [f"# {page_title}", ""]

    if songbook_url:
        parts.append(f"[📄 Songbook PDF]({songbook_url})")
        parts.append("")

    if not songs:
        parts.append("_No songs found on this plan._")
        return "\n".join(parts)

    for song in songs:
        summary = song.title
        if song.ccli_number:
            summary += f" (CCLI #{song.ccli_number})"
        # Notion recognizes this exact <details>/<summary> HTML pattern on
        # paste and converts it into a native, collapsed toggle block -- it's
        # the same markup Notion itself produces when you export a toggle.
        # The blank lines around the body are required for the lyrics to be
        # parsed as block content nested inside the toggle.
        parts.append("<details>")
        parts.append(f"<summary>{summary}</summary>")
        parts.append("")
        if song.pdf_url:
            parts.append(f"[📄 Open PDF chart]({song.pdf_url})")
            parts.append("")
        parts.append(song.body(include_chords))
        parts.append("")
        parts.append("</details>")
        parts.append("")

    return "\n".join(parts).strip() + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export song lyrics from a Planning Center Services plan to Markdown."
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
        "--no-pdf-links",
        action="store_true",
        help="Skip fetching per-song/songbook PDF chart links (fewer API calls, faster).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="Output .md file path. Defaults to '<title-prefix> - <date>.md'.",
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
        plan_date = (
            datetime.fromisoformat(sort_date_raw.replace("Z", "+00:00")).date()
            if sort_date_raw
            else date.today()
        )
        log.info(
            "Using plan %r (id=%s, service_type=%s, date=%s)",
            plan["attributes"].get("title") or plan["attributes"].get("series_title"),
            plan_id,
            service_type_id,
            plan_date,
        )

        include_pdf_links = not args.no_pdf_links
        songs = collect_songs(session, service_type_id, plan_id, include_pdf_links=include_pdf_links)
        log.info("Found %d song(s).", len(songs))

        songbook_url = (
            get_plan_songbook_url(session, service_type_id, plan_id) if include_pdf_links else None
        )
        if songbook_url:
            log.info("Found a combined songbook PDF attached to the plan.")

        markdown = format_markdown(
            args.title_prefix, plan_date, songs, args.include_chords, songbook_url=songbook_url
        )

        output_path = args.output or f"{args.title_prefix} - {plan_date.isoformat()}.md"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown)

        print(f"\n✅ Wrote {len(songs)} song(s) to {output_path!r}.")
        print("   Open the page in Notion and paste the file's contents in --")
        print("   Notion converts the Markdown headings/toggles/links into blocks automatically.")
        if include_pdf_links:
            print("   Note: PDF chart links are time-limited signed URLs -- paste soon, or")
            print("   re-run the script to refresh them if a link has gone stale.")
        return 0

    except PlanningCenterError as exc:
        log.error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
