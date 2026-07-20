"""
pco_client

Shared Planning Center Services API client: authentication, plan/song/
arrangement lookups, and lyrics assembly. Used by notion-export/update_lyrics.py,
static-site/generate_static_site.py, and experimental/remote_display.py.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

import requests

API_BASE = "https://api.planningcenteronline.com/services/v2"
REQUEST_TIMEOUT = 30  # seconds

log = logging.getLogger("pco_client")


class PlanningCenterError(RuntimeError):
    """Raised for any unrecoverable Planning Center API problem."""


# --------------------------------------------------------------------------
# HTTP / API helpers
# --------------------------------------------------------------------------


def build_session(app_id: str, secret: str) -> requests.Session:
    """Return a requests.Session authenticated via HTTP Basic Auth.

    Planning Center's "Personal Access Token" scheme uses the app id as the
    Basic Auth username and the secret as the password -- there is no OAuth
    dance needed for a script like this one.
    """
    session = requests.Session()
    session.auth = (app_id, secret)
    session.headers.update({"Accept": "application/json"})
    return session


def api_get(session: requests.Session, path: str, **params: Any) -> dict:
    """GET a Planning Center API path (relative to API_BASE) and return JSON.

    Raises PlanningCenterError with a helpful message on any HTTP failure,
    since a bare requests exception rarely tells the user what to fix.
    """
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    try:
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise PlanningCenterError(f"Network error calling {url}: {exc}") from exc

    if response.status_code == 401:
        raise PlanningCenterError(
            "Planning Center rejected the credentials (401 Unauthorized). "
            "Double-check PLANNING_CENTER_APP_ID and PLANNING_CENTER_SECRET."
        )
    if response.status_code == 404:
        raise PlanningCenterError(f"Not found (404): {url}")
    if not response.ok:
        raise PlanningCenterError(
            f"Planning Center API error {response.status_code} for {url}: "
            f"{response.text[:500]}"
        )

    return response.json()


def api_post(session: requests.Session, path: str) -> dict:
    """POST a Planning Center API path (relative to API_BASE) and return JSON.

    Used for "action" endpoints like .../attachments/{id}/open, which mint a
    signed download URL rather than returning a plain resource.
    """
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    try:
        response = session.post(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise PlanningCenterError(f"Network error calling {url}: {exc}") from exc

    if not response.ok:
        raise PlanningCenterError(
            f"Planning Center API error {response.status_code} for {url}: "
            f"{response.text[:500]}"
        )

    return response.json()


def api_get_all_pages(session: requests.Session, path: str, **params: Any) -> list[dict]:
    """GET a JSON:API collection endpoint, following pagination links."""
    items: list[dict] = []
    params.setdefault("per_page", 100)
    next_url: Optional[str] = None
    next_params = dict(params)

    while True:
        payload = api_get(session, next_url or path, **(next_params if not next_url else {}))
        items.extend(payload.get("data", []))
        next_link = payload.get("links", {}).get("next")
        if not next_link:
            break
        next_url = next_link
        next_params = {}

    return items


# --------------------------------------------------------------------------
# Domain lookups: service types, plans, items, songs, arrangements
# --------------------------------------------------------------------------


def list_service_types(session: requests.Session) -> list[dict]:
    return api_get_all_pages(session, "/service_types")


def _nearest_upcoming_plan(session: requests.Session, service_type_id: str) -> Optional[dict]:
    """Return the plan with the earliest future sort_date for a service type."""
    plans = api_get_all_pages(
        session,
        f"/service_types/{service_type_id}/plans",
        filter="future",
        order="sort_date",
    )
    return plans[0] if plans else None


def find_upcoming_plan(
    session: requests.Session, service_type_id: Optional[str]
) -> tuple[str, dict]:
    """Find the nearest upcoming plan.

    If service_type_id is given, search only within it. Otherwise, search
    across every service type and return the plan with the earliest date.
    """
    if service_type_id:
        plan = _nearest_upcoming_plan(session, service_type_id)
        if not plan:
            raise PlanningCenterError(
                f"No upcoming plans found for service type {service_type_id}."
            )
        return service_type_id, plan

    log.info("No service type given; scanning all service types for the nearest plan...")
    best: Optional[tuple[str, dict]] = None
    for st in list_service_types(session):
        st_id = st["id"]
        try:
            plan = _nearest_upcoming_plan(session, st_id)
        except PlanningCenterError as exc:
            log.warning("Skipping service type %s: %s", st_id, exc)
            continue
        if not plan:
            continue
        sort_date = plan["attributes"].get("sort_date")
        if best is None or (sort_date and sort_date < best[1]["attributes"].get("sort_date", "")):
            best = (st_id, plan)

    if not best:
        raise PlanningCenterError("No upcoming plans found in any service type.")
    return best


def find_plan_by_date(
    session: requests.Session, target: date, service_type_id: Optional[str]
) -> tuple[str, dict]:
    """Find a plan whose date matches `target`, optionally scoped to one service type."""
    service_type_ids = (
        [service_type_id] if service_type_id else [st["id"] for st in list_service_types(session)]
    )

    for st_id in service_type_ids:
        plans = api_get_all_pages(session, f"/service_types/{st_id}/plans", order="sort_date")
        for plan in plans:
            sort_date = plan["attributes"].get("sort_date", "")
            if sort_date.startswith(target.isoformat()):
                return st_id, plan

    raise PlanningCenterError(
        f"No plan found on {target.isoformat()}"
        + (f" in service type {service_type_id}" if service_type_id else " in any service type")
    )


def get_plan_by_id(
    session: requests.Session, plan_id: str, service_type_id: Optional[str]
) -> tuple[str, dict]:
    """Fetch a specific plan by id.

    Plan items live under /service_types/{id}/plans/{id}, so if the caller
    didn't supply a service type we scan all of them for the matching plan.
    """
    service_type_ids = (
        [service_type_id] if service_type_id else [st["id"] for st in list_service_types(session)]
    )
    for st_id in service_type_ids:
        try:
            payload = api_get(session, f"/service_types/{st_id}/plans/{plan_id}")
            return st_id, payload["data"]
        except PlanningCenterError:
            continue
    raise PlanningCenterError(f"Plan {plan_id} not found in any accessible service type.")


def get_plan_items(session: requests.Session, service_type_id: str, plan_id: str) -> list[dict]:
    return api_get_all_pages(
        session,
        f"/service_types/{service_type_id}/plans/{plan_id}/items",
        order="sequence",
    )


def get_song(session: requests.Session, song_id: str) -> dict:
    return api_get(session, f"/songs/{song_id}")["data"]


def get_arrangement(session: requests.Session, song_id: str, arrangement_id: str) -> dict:
    return api_get(session, f"/songs/{song_id}/arrangements/{arrangement_id}")["data"]


def get_default_arrangement(session: requests.Session, song_id: str) -> Optional[dict]:
    """Fall back to a song's first listed arrangement when a plan item

    doesn't specify which arrangement was chosen.
    """
    arrangements = api_get_all_pages(session, f"/songs/{song_id}/arrangements")
    return arrangements[0] if arrangements else None


def open_attachment_url(session: requests.Session, attachment_path: str) -> Optional[str]:
    """Resolve an Attachment's ".../open" action to a signed, downloadable URL.

    The plain Attachment resource only has a `url` pointing at Planning
    Center's web app (which requires a browser login), not a direct file
    link. POSTing to its `/open` action mints a short-lived, pre-signed URL
    that serves the actual file -- this is the same mechanism the "print" /
    "download" buttons use in the Services UI. Because the URL is
    time-limited, treat it as disposable: re-run the script if a link goes
    stale.
    """
    try:
        payload = api_post(session, f"{attachment_path}/open")
    except PlanningCenterError as exc:
        log.warning("Could not resolve a download link for %s: %s", attachment_path, exc)
        return None
    return payload.get("data", {}).get("attributes", {}).get("attachment_url")


def get_arrangement_pdf_url(session: requests.Session, song_id: str, arrangement_id: str) -> Optional[str]:
    """Return a download link for the arrangement's auto-generated PDF chart.

    Planning Center generates a print-ready PDF per arrangement (lyrics,
    plus chords when the arrangement has chords enabled) exposed as an
    Attachment of type AttachmentChart::Lyric.
    """
    base_path = f"/songs/{song_id}/arrangements/{arrangement_id}/attachments"
    attachments = api_get_all_pages(session, base_path)
    if not attachments:
        return None
    return open_attachment_url(session, f"{base_path}/{attachments[0]['id']}")


def get_plan_songbook_url(session: requests.Session, service_type_id: str, plan_id: str) -> Optional[str]:
    """Return a download link for a PDF manually attached to the plan itself.

    Some churches attach one combined "songbook" PDF to the plan (rather
    than relying on Planning Center's per-song PDFs). Grab the first PDF
    attachment found, if any.
    """
    base_path = f"/service_types/{service_type_id}/plans/{plan_id}/attachments"
    attachments = api_get_all_pages(session, base_path)
    pdf_attachments = [a for a in attachments if a["attributes"].get("filename", "").lower().endswith(".pdf")]
    if not pdf_attachments:
        return None
    return open_attachment_url(session, f"{base_path}/{pdf_attachments[0]['id']}")


# --------------------------------------------------------------------------
# Song/lyrics assembly
# --------------------------------------------------------------------------


def clean_text(text: str) -> str:
    """Normalize line endings and trim trailing whitespace on each line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


class SongLyrics:
    """Holds one plan item's song title plus its lyrics/chords text."""

    def __init__(
        self,
        title: str,
        plain_lyrics: str,
        chord_chart: str,
        ccli_number: Optional[str],
        pdf_url: Optional[str] = None,
    ):
        self.title = title
        self.plain_lyrics = plain_lyrics
        self.chord_chart = chord_chart
        self.ccli_number = ccli_number
        self.pdf_url = pdf_url

    def body(self, include_chords: bool) -> str:
        if include_chords and self.chord_chart.strip():
            return self.chord_chart
        if self.plain_lyrics.strip():
            return self.plain_lyrics
        # Neither field populated in Planning Center for this arrangement.
        return "_No lyrics found in Planning Center for this song/arrangement._"


def collect_songs(
    session: requests.Session, service_type_id: str, plan_id: str, include_pdf_links: bool = True
) -> list[SongLyrics]:
    """Walk a plan's items, pull out songs, and fetch each one's lyrics."""
    items = get_plan_items(session, service_type_id, plan_id)
    songs: list[SongLyrics] = []

    for item in items:
        attrs = item.get("attributes", {})
        if attrs.get("item_type") != "song":
            continue

        title = attrs.get("title") or "Untitled Song"
        song_rel = item.get("relationships", {}).get("song", {}).get("data")
        if not song_rel:
            log.warning("Plan item %r is a song but has no linked song record; skipping lyrics.", title)
            songs.append(SongLyrics(title, "", "", None))
            continue

        song_id = song_rel["id"]
        try:
            song = get_song(session, song_id)
        except PlanningCenterError as exc:
            log.warning("Could not load song %s (%s): %s", song_id, title, exc)
            songs.append(SongLyrics(title, "", "", None))
            continue

        song_title = song["attributes"].get("title") or title
        ccli_number = song["attributes"].get("ccli_number")

        arrangement_rel = item.get("relationships", {}).get("arrangement", {}).get("data")
        arrangement: Optional[dict] = None
        try:
            if arrangement_rel:
                arrangement = get_arrangement(session, song_id, arrangement_rel["id"])
            else:
                arrangement = get_default_arrangement(session, song_id)
                if arrangement:
                    log.info(
                        "Plan item %r didn't specify an arrangement; using %r.",
                        song_title,
                        arrangement["attributes"].get("name"),
                    )
        except PlanningCenterError as exc:
            log.warning("Could not load arrangement for %r: %s", song_title, exc)

        plain_lyrics = clean_text(arrangement["attributes"].get("lyrics") or "") if arrangement else ""
        chord_chart = clean_text(arrangement["attributes"].get("chord_chart") or "") if arrangement else ""

        pdf_url = None
        if include_pdf_links and arrangement:
            pdf_url = get_arrangement_pdf_url(session, song_id, arrangement["id"])

        songs.append(SongLyrics(song_title, plain_lyrics, chord_chart, ccli_number, pdf_url))

    return songs
