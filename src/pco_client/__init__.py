"""
pco_client

Shared Planning Center Services API client: authentication, plan/song/
arrangement lookups, and lyrics assembly. Used by notion-export/update_lyrics.py,
static-site/generate_static_site.py, and experimental/remote_display.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
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

    Returns `{}` rather than raising when the response carries no JSON body:
    the Services LIVE actions (go_to_next_item, toggle_control, ...) answer
    with an empty 204 on success, and a bare `.json()` would turn a perfectly
    successful control action into a decode error mid-service.
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

    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


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


def has_scheduled_time(plan: dict) -> bool:
    """Whether a plan has any PlanTime attached to it.

    A plan with none is a dateless draft, and Planning Center reports its
    `sort_date` as "whenever you called the API" rather than null -- so such
    a plan spuriously matches *today* on every lookup, forever (see
    find_plan_by_date's docstring for the full story and the real-world case
    that surfaced it). Anything that resolves or offers up plans by date has
    to filter these out first, which is why this lives here rather than
    inline in one caller.
    """
    attrs = plan.get("attributes", {})
    return (
        attrs.get("service_time_count", 0)
        + attrs.get("rehearsal_time_count", 0)
        + attrs.get("other_time_count", 0)
    ) > 0


def list_selectable_plans(
    session: requests.Session, service_type_id: str, limit: int = 25
) -> list[dict]:
    """Plans a human could plausibly want to project, nearest first.

    Upcoming plans (including today's) with a real scheduled time. Dateless
    drafts are filtered out via has_scheduled_time -- they're the ones that
    would otherwise crowd the picker with entries like "Weekend25 Fredag
    Kväll" that can never be placed on a calendar.
    """
    plans = api_get_all_pages(
        session, f"/service_types/{service_type_id}/plans", filter="future", order="sort_date"
    )
    return [p for p in plans if has_scheduled_time(p)][:limit]


def plan_display_title(plan: dict) -> str:
    """The most human-readable name a plan has, for pickers and headings."""
    attrs = plan.get("attributes", {})
    title = attrs.get("title") or attrs.get("series_title") or f"Plan {plan.get('id')}"
    dates = attrs.get("dates")
    return f"{dates} -- {title}" if dates and dates != "No dates" else title


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
    """Find a plan whose date matches `target`, optionally scoped to one service type.

    Skips plans with no scheduled time attached (service_time_count ==
    rehearsal_time_count == other_time_count == 0). Planning Center gives
    these a `sort_date` of "whenever this API request happened" instead of a
    real stored value (confirmed empirically: re-fetching the same plan
    seconds apart returns a different, ever-increasing sort_date) -- so an
    unscheduled draft plan left over in a service type spuriously matches
    *today* on every single call, on every single day, forever. Left
    unfiltered, such a plan can permanently shadow a real dated plan that
    happens to sort later in the API's response order. A plan with no
    scheduled time also can't answer "what's the plan for this date" in any
    meaningful sense, so excluding it outright is correct, not just a
    workaround.
    """
    service_type_ids = (
        [service_type_id] if service_type_id else [st["id"] for st in list_service_types(session)]
    )

    for st_id in service_type_ids:
        plans = api_get_all_pages(session, f"/service_types/{st_id}/plans", order="sort_date")
        for plan in plans:
            if not has_scheduled_time(plan):
                continue
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


def get_plan_times(session: requests.Session, service_type_id: str, plan_id: str) -> list[dict]:
    """Return a plan's scheduled time blocks (start/end, name, time_type).

    Separate from sort_date/dates (which only carry a calendar date, not a
    time) -- this is the actual scheduled start/end used by the scheduler to
    decide when to auto-open/close. Not used by the lyrics-export scripts.
    """
    return api_get_all_pages(session, f"/service_types/{service_type_id}/plans/{plan_id}/plan_times")


def parse_pco_datetime(value: str) -> datetime:
    """Parse a Planning Center API timestamp (UTC, 'Z' suffix) into an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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
# Services LIVE
#
# Planning Center's own "Services LIVE" mode -- the thing a worship leader
# drives from the Services app on an iPad, which walks the plan item by item
# and records when each one actually started. The API exposes it as a single
# `live` resource per plan, plus three POST actions.
#
# Two things about it shape everything built on top:
#
#   1. *Exactly one* controller per plan, org-wide. `toggle_control` takes
#      control if you don't have it and releases it if you do -- and taking
#      it boots whoever held it, with no confirmation step on their end.
#      That is why nothing in this module takes control implicitly; callers
#      have to ask for it (see live_take_control).
#   2. What's live is reported indirectly, as a `current_item_time`
#      relationship pointing at an ItemTime, *not* at an Item. The ItemTime
#      is what carries the link back to the plan item, so resolving "which
#      item is on screen right now" needs the sideloaded `included` block --
#      hence the `include=` on the GET below.
# --------------------------------------------------------------------------

# Sideloaded so get_live_status can resolve current/next ItemTime -> Item and
# name the controller in one round trip. Kept as a constant because the
# fallback path below needs to talk about it in a log message.
_LIVE_INCLUDES = "current_item_time,next_item_time,controller"


@dataclass
class LiveStatus:
    """A snapshot of a plan's Services LIVE state.

    `current_item_id`/`next_item_id` are already resolved from ItemTime to
    the plan Item ids that get_plan_items/collect_songs return, so callers can
    match them against a plan's items without knowing ItemTime exists.
    """

    can_control: bool = False
    can_take_control: bool = False
    controller_name: Optional[str] = None
    current_item_id: Optional[str] = None
    next_item_id: Optional[str] = None
    title: str = ""
    series_title: str = ""
    # True when Planning Center answered at all. A False here means the poll
    # failed (network blip, plan not live-able) and the caller should keep
    # showing whatever it last had rather than blanking the projector.
    reachable: bool = True
    error: Optional[str] = None


def _live_path(service_type_id: str, plan_id: str) -> str:
    return f"/service_types/{service_type_id}/plans/{plan_id}/live"


def _rel_id(relationships: dict, name: str) -> Optional[str]:
    """Pull relationships[name].data.id, tolerating every shape PCO uses for
    'this relationship is empty' (missing key, null data, empty dict)."""
    data = (relationships.get(name) or {}).get("data")
    if isinstance(data, dict):
        return data.get("id")
    return None


def _index_item_times(included: list[dict]) -> dict[str, str]:
    """Map ItemTime id -> the plan Item id it belongs to."""
    mapping: dict[str, str] = {}
    for entry in included:
        if entry.get("type") != "ItemTime":
            continue
        item_id = _rel_id(entry.get("relationships", {}), "item")
        if item_id:
            mapping[entry["id"]] = item_id
    return mapping


def _find_person_name(included: list[dict], person_id: Optional[str]) -> Optional[str]:
    if not person_id:
        return None
    for entry in included:
        if entry.get("type") == "Person" and entry.get("id") == person_id:
            attrs = entry.get("attributes", {})
            name = attrs.get("name") or " ".join(
                part for part in (attrs.get("first_name"), attrs.get("last_name")) if part
            )
            return name.strip() or None
    return None


def get_live_status(session: requests.Session, service_type_id: str, plan_id: str) -> LiveStatus:
    """Read a plan's Services LIVE state. Never raises.

    This gets polled every couple of seconds by a projector that may be the
    only thing the congregation is looking at, so a transient Planning Center
    hiccup must not surface as an exception -- it comes back as
    `reachable=False` and the caller holds its last known good frame.
    """
    path = _live_path(service_type_id, plan_id)
    try:
        payload = api_get(session, path, include=_LIVE_INCLUDES)
    except PlanningCenterError as exc:
        # An unsupported `include` is a 400, which would otherwise take the
        # whole feature down for a cosmetic field (the controller's name).
        # Retry bare: current/next item then resolve to None, which the
        # display already renders as "waiting" rather than as stale lyrics.
        log.warning("Live poll with include=%s failed (%s); retrying without it.", _LIVE_INCLUDES, exc)
        try:
            payload = api_get(session, path)
        except PlanningCenterError as bare_exc:
            return LiveStatus(reachable=False, error=str(bare_exc))

    data = payload.get("data") or {}
    attrs = data.get("attributes", {})
    rels = data.get("relationships", {})
    included = payload.get("included", []) or []
    item_times = _index_item_times(included)

    current_item_time_id = _rel_id(rels, "current_item_time")
    next_item_time_id = _rel_id(rels, "next_item_time")

    return LiveStatus(
        can_control=bool(attrs.get("can_control")),
        can_take_control=bool(attrs.get("can_take_control")),
        controller_name=_find_person_name(included, _rel_id(rels, "controller")),
        current_item_id=item_times.get(current_item_time_id or ""),
        next_item_id=item_times.get(next_item_time_id or ""),
        title=attrs.get("title") or "",
        series_title=attrs.get("series_title") or "",
    )


def live_go_to_next_item(session: requests.Session, service_type_id: str, plan_id: str) -> None:
    api_post(session, f"{_live_path(service_type_id, plan_id)}/go_to_next_item")


def live_go_to_previous_item(session: requests.Session, service_type_id: str, plan_id: str) -> None:
    api_post(session, f"{_live_path(service_type_id, plan_id)}/go_to_previous_item")


def live_toggle_control(session: requests.Session, service_type_id: str, plan_id: str) -> None:
    """Flip control of this plan's LIVE session.

    Raw toggle, exactly as Planning Center exposes it: it takes control if
    this token doesn't have it and releases control if it does. Callers
    almost always want live_take_control/live_release_control below, which
    read the current state first so "take control" is idempotent instead of
    handing control back on a double-click.
    """
    api_post(session, f"{_live_path(service_type_id, plan_id)}/toggle_control")


def live_take_control(session: requests.Session, service_type_id: str, plan_id: str) -> LiveStatus:
    """Take control of the plan's LIVE session, if we don't already have it.

    **This boots whoever currently holds control** (typically a worship
    leader running Services LIVE on an iPad) -- Planning Center allows one
    controller per plan and gives the displaced one no warning. Never call
    this on a timer, a page load, or any other implicit path; it should be
    reachable only from a deliberate, confirmed click.
    """
    status = get_live_status(session, service_type_id, plan_id)
    if not status.reachable:
        return status
    if status.can_control:
        return status  # already ours; toggling here would *release* it
    live_toggle_control(session, service_type_id, plan_id)
    return get_live_status(session, service_type_id, plan_id)


def live_release_control(session: requests.Session, service_type_id: str, plan_id: str) -> LiveStatus:
    """Give up control, if we hold it, so a leader's own device can take over."""
    status = get_live_status(session, service_type_id, plan_id)
    if not status.reachable:
        return status
    if not status.can_control:
        return status
    live_toggle_control(session, service_type_id, plan_id)
    return get_live_status(session, service_type_id, plan_id)


# --------------------------------------------------------------------------
# Song/lyrics assembly
# --------------------------------------------------------------------------


def clean_text(text: str) -> str:
    """Normalize line endings and trim trailing whitespace on each line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


class SongLyrics:
    """Holds one plan item's song title plus its lyrics/chords text.

    `item_id` is the *plan item* id this song came from, not the song id --
    it's what Services LIVE reports as the current item, so it's the only
    handle that can answer "is this the song on screen right now". The
    lyrics-export scripts ignore it; the live projection display depends on
    it. It stays Optional because the error paths in collect_songs below can
    produce a placeholder entry for an item whose song record wouldn't load.
    """

    def __init__(
        self,
        title: str,
        plain_lyrics: str,
        chord_chart: str,
        ccli_number: Optional[str],
        pdf_url: Optional[str] = None,
        item_id: Optional[str] = None,
    ):
        self.title = title
        self.plain_lyrics = plain_lyrics
        self.chord_chart = chord_chart
        self.ccli_number = ccli_number
        self.pdf_url = pdf_url
        self.item_id = item_id

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

        item_id = item.get("id")
        title = attrs.get("title") or "Untitled Song"
        song_rel = item.get("relationships", {}).get("song", {}).get("data")
        if not song_rel:
            log.warning("Plan item %r is a song but has no linked song record; skipping lyrics.", title)
            songs.append(SongLyrics(title, "", "", None, item_id=item_id))
            continue

        song_id = song_rel["id"]
        try:
            song = get_song(session, song_id)
        except PlanningCenterError as exc:
            log.warning("Could not load song %s (%s): %s", song_id, title, exc)
            songs.append(SongLyrics(title, "", "", None, item_id=item_id))
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

        songs.append(SongLyrics(song_title, plain_lyrics, chord_chart, ccli_number, pdf_url, item_id=item_id))

    return songs


@dataclass
class PlanItem:
    """A slim projection of a plan item -- everything the live display needs
    to reason about the plan's running order, without the lyrics payload.

    collect_songs deliberately returns *only* songs, but a live session has
    to know about the other items too: Services LIVE walks the whole running
    order, so "which item is current" regularly lands on a sermon or an
    announcement, and the display has to recognize that as "hold" rather than
    as "unknown item" (or, worse, leave the previous song's lyrics up).
    """

    id: str
    title: str
    item_type: str
    sequence: int = 0

    @property
    def is_song(self) -> bool:
        return self.item_type == "song"


def get_plan_item_summaries(
    session: requests.Session, service_type_id: str, plan_id: str
) -> list[PlanItem]:
    """Return every item in a plan, in running order, songs and non-songs alike."""
    summaries = []
    for index, item in enumerate(get_plan_items(session, service_type_id, plan_id)):
        attrs = item.get("attributes", {})
        summaries.append(
            PlanItem(
                id=item["id"],
                title=attrs.get("title") or "Untitled",
                item_type=attrs.get("item_type") or "item",
                sequence=attrs.get("sequence") if isinstance(attrs.get("sequence"), int) else index,
            )
        )
    return summaries
