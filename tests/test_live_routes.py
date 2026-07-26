"""
Route-level tests for the live projection feature, driven through Flask's
test client against a temp DATA_DIR with Planning Center monkeypatched out.

The bulk of this file is the authentication boundary, because that's the part
where a mistake is both easy to make and expensive: three route groups share
one process, and the projector deliberately gets in without a password while
everything else needs the admin credential.
"""

import base64

import pytest

import admin_app
import live_routes
import live_session
from live_session import LiveSessionState, PlanCache
from pco_client import LiveStatus, PlanItem, SongLyrics

ADMIN_PW = "admin-pw"


def _auth(username, password):
    raw = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


ADMIN_HEADERS = _auth("admin", ADMIN_PW)
# The remote shares the admin credential -- see admin_app._require_auth.
REMOTE_HEADERS = ADMIN_HEADERS


@pytest.fixture
def cache():
    items = [
        PlanItem(id="i1", title="Welcome", item_type="item", sequence=1),
        PlanItem(id="i2", title="Song A", item_type="song", sequence=2),
        PlanItem(id="i3", title="Sermon", item_type="item", sequence=3),
        PlanItem(id="i4", title="Song B", item_type="song", sequence=4),
    ]
    songs = [
        SongLyrics("Song A", "lyrics A", "", "111", item_id="i2"),
        SongLyrics("Song B", "lyrics B", "", "222", item_id="i4"),
    ]
    return PlanCache.build(items, songs)


@pytest.fixture
def client(tmp_path, monkeypatch, cache):
    """A test client with both credentials set, a session-less DATA_DIR, and
    every Planning Center call stubbed."""
    monkeypatch.setattr(admin_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(admin_app, "ADMIN_PASSWORD", ADMIN_PW)
    monkeypatch.setattr(admin_app, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(
        live_routes,
        "_ctx",
        live_routes.LiveContext(data_dir=tmp_path, session=None, lock=admin_app._lock),
    )
    monkeypatch.setattr(live_routes, "load_plan_cache", lambda s, force=False: cache)
    monkeypatch.setattr(live_routes, "cache_for", lambda s: cache if s else None)
    monkeypatch.setattr(live_routes, "poll_live", lambda s, force=False: LiveStatus(current_item_id="i2"))
    monkeypatch.setattr(
        live_routes, "list_service_types", lambda s: [{"id": "st1", "attributes": {"name": "Sunday"}}]
    )
    monkeypatch.setattr(
        live_routes,
        "list_selectable_plans",
        lambda s, st, limit=25: [{"id": "p1", "attributes": {"title": "Sunday", "dates": "26 July 2026"}}],
    )

    admin_app.app.config["TESTING"] = True
    with admin_app.app.test_client() as c:
        c.data_dir = tmp_path
        yield c


def _start_session(client, mode="follow"):
    session = LiveSessionState(
        service_type_id="st1",
        plan_id="p1",
        plan_title="Sunday",
        started_at="2026-07-26T09:00:00+00:00",
        mode=mode,
    )
    live_session.write_session(client.data_dir, session)
    return session


# --------------------------------------------------------------------------
# The authentication boundary
# --------------------------------------------------------------------------


def test_public_routes_stay_open(client):
    assert client.get("/healthz").status_code == 200


def test_admin_still_requires_the_admin_password(client):
    assert client.get("/admin/live/").status_code == 401
    assert client.get("/admin/live/", headers=ADMIN_HEADERS).status_code == 200


def test_remote_uses_the_admin_credential(client):
    """One credential, one Basic Auth realm.

    A separate remote password was tried and removed: browsers cache Basic
    Auth per *origin*, so a browser that had ever seen the remote realm kept
    re-sending those credentials to every path on the host, and the admin
    password appeared rejected no matter how often it was typed (six
    consecutive 401s observed against a real browser). Two Basic Auth
    identities on one origin is not something a browser holds cleanly.
    """
    assert client.get("/remote/").status_code == 401
    assert client.get("/remote/", headers=ADMIN_HEADERS).status_code == 200


def test_remote_and_admin_share_one_realm(client):
    """The realm string is the thing browsers key their credential cache on;
    two different realms on one origin is what caused the 401 loop."""
    realms = {
        client.get(path).headers.get("WWW-Authenticate") for path in ("/admin/live/", "/remote/")
    }
    assert realms == {'Basic realm="admin"'}


def test_admin_credential_drives_the_remote_endpoints(client, monkeypatch):
    _start_session(client, mode="control")
    monkeypatch.setattr(live_routes, "live_go_to_next_item", lambda *a: None)
    assert client.post("/remote/next", headers=ADMIN_HEADERS).status_code == 200


def test_wrong_password_is_still_refused_on_the_remote(client):
    """Sharing the admin credential must not mean accepting any credential."""
    assert client.get("/remote/", headers=_auth("admin", "wrong")).status_code == 401
    assert client.get("/remote/", headers=_auth("remote", "remote-pw")).status_code == 401


def test_remote_write_routes_are_gated_too(client):
    """A 401 on the page but an open POST endpoint would be worse than useless."""
    _start_session(client, mode="control")
    for path in ("/remote/next", "/remote/prev", "/remote/theme", "/remote/reload"):
        assert client.post(path).status_code == 401, path


# --------------------------------------------------------------------------
# The display token
# --------------------------------------------------------------------------


def test_display_needs_a_valid_token(client):
    live_session.ensure_display_token(client.data_dir)
    assert client.get("/project/wrong-token").status_code == 404


def test_display_opens_with_the_right_token_and_no_password(client):
    token = live_session.ensure_display_token(client.data_dir)
    assert client.get(f"/project/{token}").status_code == 200


def test_bad_token_is_indistinguishable_from_a_missing_page(client):
    """A wrong token returns exactly what an unknown URL does, so probing
    can't confirm that a display URL exists at all."""
    live_session.ensure_display_token(client.data_dir)
    assert client.get("/project/wrong-token").status_code == client.get("/project/").status_code == 404


def test_rotating_the_token_invalidates_the_old_display_url(client):
    old = live_session.ensure_display_token(client.data_dir)
    assert client.get(f"/project/{old}").status_code == 200

    client.post("/admin/live/rotate", headers=ADMIN_HEADERS)
    assert client.get(f"/project/{old}").status_code == 404
    new = live_session.read_display_token(client.data_dir)
    assert client.get(f"/project/{new}").status_code == 200


def test_display_token_grants_no_write_access(client):
    """The projector is read-only by construction -- there is no POST route
    under /project at all, so a leaked display link can't drive anything."""
    token = live_session.ensure_display_token(client.data_dir)
    for path in (f"/project/{token}", f"/project/{token}/state.json"):
        assert client.post(path).status_code == 405, path


# --------------------------------------------------------------------------
# What the display reports
# --------------------------------------------------------------------------


def test_display_state_reports_the_live_song(client):
    _start_session(client)
    token = live_session.ensure_display_token(client.data_dir)

    body = client.get(f"/project/{token}/state.json").get_json()
    assert body["status"] == "song"
    assert body["lyrics"] == "lyrics A"
    assert body["theme"] == "dark"


def test_display_state_blanks_on_a_non_song_item(client, monkeypatch):
    _start_session(client)
    monkeypatch.setattr(live_routes, "poll_live", lambda s, force=False: LiveStatus(current_item_id="i3"))
    token = live_session.ensure_display_token(client.data_dir)

    body = client.get(f"/project/{token}/state.json").get_json()
    assert body["status"] == "hold"
    assert body["lyrics"] == ""


def test_display_state_without_a_session_waits(client):
    token = live_session.ensure_display_token(client.data_dir)
    body = client.get(f"/project/{token}/state.json").get_json()
    assert body["status"] == "waiting"


# --------------------------------------------------------------------------
# Follow vs. control
# --------------------------------------------------------------------------


def test_follow_mode_refuses_to_drive_planning_center(client, monkeypatch):
    """The safety property of follow mode: a Next press fails loudly instead
    of quietly taking control away from the leader's own device."""
    _start_session(client, mode="follow")
    calls = []
    monkeypatch.setattr(live_routes, "live_go_to_next_item", lambda *a: calls.append("next"))

    res = client.post("/remote/next", headers=REMOTE_HEADERS)
    assert res.status_code == 409
    assert "follow mode" in res.get_json()["error"]
    assert calls == []


def test_control_mode_drives_planning_center(client, monkeypatch):
    _start_session(client, mode="control")
    calls = []
    monkeypatch.setattr(live_routes, "live_go_to_next_item", lambda *a: calls.append("next"))
    monkeypatch.setattr(live_routes, "live_go_to_previous_item", lambda *a: calls.append("prev"))

    assert client.post("/remote/next", headers=REMOTE_HEADERS).status_code == 200
    assert client.post("/remote/prev", headers=REMOTE_HEADERS).status_code == 200
    assert calls == ["next", "prev"]


def test_a_session_starts_in_follow_mode(client, monkeypatch):
    monkeypatch.setattr(
        live_routes, "get_plan_by_id", lambda *a: ("st1", {"id": "p1", "attributes": {"title": "Sunday"}})
    )
    client.post(
        "/admin/live/start", data={"service_type_id": "st1", "plan_id": "p1"}, headers=ADMIN_HEADERS
    )
    assert live_session.read_session(client.data_dir).mode == "follow"


def test_stopping_a_controlling_session_releases_control(client, monkeypatch):
    """Otherwise control stays parked on a projector nobody is watching, and
    the next person to open Services LIVE is locked out."""
    _start_session(client, mode="control")
    released = []
    monkeypatch.setattr(live_routes, "live_release_control", lambda *a: released.append(a))

    client.post("/admin/live/stop", headers=ADMIN_HEADERS)
    assert len(released) == 1
    assert live_session.read_session(client.data_dir) is None


def test_stopping_a_following_session_does_not_touch_planning_center(client, monkeypatch):
    _start_session(client, mode="follow")
    released = []
    monkeypatch.setattr(live_routes, "live_release_control", lambda *a: released.append(a))

    client.post("/admin/live/stop", headers=ADMIN_HEADERS)
    assert released == []


def test_take_control_records_control_mode(client, monkeypatch):
    _start_session(client, mode="follow")
    monkeypatch.setattr(live_routes, "live_take_control", lambda *a: LiveStatus(holds_control=True))

    client.post("/admin/live/take", headers=ADMIN_HEADERS)
    assert live_session.read_session(client.data_dir).mode == "control"


def test_take_control_stays_in_follow_mode_if_pco_refuses(client, monkeypatch):
    """If Planning Center didn't actually hand over control, recording
    'control' locally would leave the remote firing writes that silently
    do nothing."""
    _start_session(client, mode="follow")
    monkeypatch.setattr(live_routes, "live_take_control", lambda *a: LiveStatus(holds_control=False))

    client.post("/admin/live/take", headers=ADMIN_HEADERS)
    assert live_session.read_session(client.data_dir).mode == "follow"


# --------------------------------------------------------------------------
# Tap-to-jump
# --------------------------------------------------------------------------


def test_goto_walks_the_running_order_including_non_songs(client, monkeypatch):
    """i2 -> i4 crosses the sermon, so it's two LIVE steps, not one."""
    _start_session(client, mode="control")
    calls = []
    monkeypatch.setattr(live_routes, "live_go_to_next_item", lambda *a: calls.append("next"))

    res = client.post("/remote/goto/i4", headers=REMOTE_HEADERS)
    assert res.status_code == 200
    assert calls == ["next", "next"]


def test_goto_an_unknown_item_is_refused(client):
    _start_session(client, mode="control")
    res = client.post("/remote/goto/nope", headers=REMOTE_HEADERS)
    assert res.status_code == 409


def test_goto_refuses_a_jump_beyond_the_step_cap(client, monkeypatch):
    """A mis-tap on a long plan must not turn into an unbounded burst of
    writes against Planning Center."""
    _start_session(client, mode="control")
    monkeypatch.setattr(live_routes, "steps_between", lambda *a: live_routes.MAX_JUMP_STEPS + 1)
    calls = []
    monkeypatch.setattr(live_routes, "live_go_to_next_item", lambda *a: calls.append("next"))

    res = client.post("/remote/goto/i4", headers=REMOTE_HEADERS)
    assert res.status_code == 409
    assert calls == []


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------


def test_theme_toggles_between_dark_and_light(client):
    _start_session(client)
    token = live_session.ensure_display_token(client.data_dir)

    assert client.post("/remote/theme", headers=REMOTE_HEADERS).get_json()["theme"] == "light"
    assert client.get(f"/project/{token}/state.json").get_json()["theme"] == "light"
    assert client.post("/remote/theme", headers=REMOTE_HEADERS).get_json()["theme"] == "dark"


def test_theme_works_in_follow_mode(client):
    """Flipping the projector's colours is a local display concern -- it must
    not require taking control of Planning Center."""
    _start_session(client, mode="follow")
    assert client.post("/remote/theme", headers=REMOTE_HEADERS).status_code == 200


def test_theme_survives_a_restart(client):
    _start_session(client)
    client.post("/remote/theme", headers=REMOTE_HEADERS)
    assert live_session.read_session(client.data_dir).theme == "light"


# --------------------------------------------------------------------------
# Admin page rendering
#
# These templates are assembled with str.format over a dozen keys, so a
# missing or misspelled one is a KeyError at request time rather than at
# import time -- exactly the failure you'd rather not meet mid-service.
# --------------------------------------------------------------------------


def test_picker_renders_with_no_session(client):
    res = client.get("/admin/live/", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    assert b"Start a session" in res.data


def test_picker_lists_plans_once_a_service_type_is_chosen(client):
    res = client.get("/admin/live/?service_type_id=st1", headers=ADMIN_HEADERS)
    assert b"26 July 2026" in res.data


def test_active_page_renders_in_follow_mode(client, monkeypatch):
    _start_session(client, mode="follow")
    monkeypatch.setattr(live_routes, "poll_live", lambda s, force=False: LiveStatus(current_item_id="i2"))

    res = client.get("/admin/live/", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    assert b"Take control" in res.data
    assert b"disconnects whoever is running" in res.data  # the warning is present
    assert b"Song A" in res.data


def test_active_page_renders_in_control_mode(client, monkeypatch):
    _start_session(client, mode="control")
    monkeypatch.setattr(
        live_routes,
        "poll_live",
        lambda s, force=False: LiveStatus(current_item_id="i2", holds_control=True, controller_name="Ada"),
    )

    res = client.get("/admin/live/", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    assert b"Release control" in res.data
    assert b"Take control" not in res.data


def test_active_page_shows_the_display_url(client):
    _start_session(client)
    token = live_session.ensure_display_token(client.data_dir)
    res = client.get("/admin/live/", headers=ADMIN_HEADERS)
    assert token.encode() in res.data


def test_admin_status_page_links_to_the_live_console(client):
    """The status page's own format() call grew two keys -- make sure it
    still renders."""
    _start_session(client)
    res = client.get("/admin/", headers=ADMIN_HEADERS)
    assert res.status_code == 200
    assert b"Live projection" in res.data
    assert b"follow mode" in res.data
