from datetime import date

import pytest

import pco_client


def _plan(plan_id, sort_date, service_time_count=1, rehearsal_time_count=0, other_time_count=0):
    return {
        "id": plan_id,
        "attributes": {
            "sort_date": sort_date,
            "service_time_count": service_time_count,
            "rehearsal_time_count": rehearsal_time_count,
            "other_time_count": other_time_count,
        },
    }


# --------------------------------------------------------------------------
# find_plan_by_date -- skips plans with no scheduled time attached.
#
# Planning Center gives an unscheduled draft plan a sort_date equal to
# "whenever the API happened to be called" instead of a real stored value,
# so it spuriously matches *today* on every single lookup. Real-world
# trigger: the "Special Events" service type on this account carries
# several old dateless draft plans that shadowed a real dated plan created
# for 2026-07-23 (see the scheduler roadmap task in knowitall).
# --------------------------------------------------------------------------


def test_find_plan_by_date_skips_unscheduled_ghost_plan(monkeypatch):
    target = date(2026, 7, 23)
    ghost = _plan("ghost1", "2026-07-23T12:44:12Z", service_time_count=0)
    real = _plan("real1", "2026-07-23T09:38:20Z", service_time_count=1)

    monkeypatch.setattr(pco_client, "api_get_all_pages", lambda *a, **k: [ghost, real])

    st_id, plan = pco_client.find_plan_by_date(None, target, "979578")
    assert plan["id"] == "real1"
    assert st_id == "979578"


def test_find_plan_by_date_skips_multiple_ghost_plans_regardless_of_order(monkeypatch):
    target = date(2026, 7, 23)
    ghosts = [_plan(f"ghost{i}", "2026-07-23T12:44:12Z", service_time_count=0) for i in range(5)]
    real = _plan("real1", "2026-07-23T09:38:20Z")

    monkeypatch.setattr(pco_client, "api_get_all_pages", lambda *a, **k: [*ghosts, real])

    _, plan = pco_client.find_plan_by_date(None, target, "979578")
    assert plan["id"] == "real1"


def test_find_plan_by_date_counts_rehearsal_and_other_times_as_scheduled(monkeypatch):
    target = date(2026, 7, 23)
    rehearsal_only = _plan("p1", "2026-07-23T09:00:00Z", service_time_count=0, rehearsal_time_count=1)

    monkeypatch.setattr(pco_client, "api_get_all_pages", lambda *a, **k: [rehearsal_only])

    _, plan = pco_client.find_plan_by_date(None, target, "979578")
    assert plan["id"] == "p1"


def test_find_plan_by_date_raises_when_only_ghost_plans_match(monkeypatch):
    target = date(2026, 7, 23)
    ghost = _plan("ghost1", "2026-07-23T12:44:12Z", service_time_count=0)

    monkeypatch.setattr(pco_client, "api_get_all_pages", lambda *a, **k: [ghost])

    with pytest.raises(pco_client.PlanningCenterError, match="No plan found"):
        pco_client.find_plan_by_date(None, target, "979578")


def test_find_plan_by_date_still_raises_when_nothing_matches_date(monkeypatch):
    target = date(2026, 7, 23)
    other_day = _plan("p1", "2026-07-20T09:00:00Z")

    monkeypatch.setattr(pco_client, "api_get_all_pages", lambda *a, **k: [other_day])

    with pytest.raises(pco_client.PlanningCenterError, match="No plan found"):
        pco_client.find_plan_by_date(None, target, "979578")


# --------------------------------------------------------------------------
# Services LIVE
#
# The shape being parsed here is the awkward part of the Live API: what's on
# screen is reported as a `current_item_time` relationship pointing at an
# ItemTime, and only the sideloaded ItemTime carries the link back to the
# plan Item. Everything below pins that indirection down, plus the failure
# modes that must degrade instead of raising -- this gets polled every couple
# of seconds by a projector during a live service.
# --------------------------------------------------------------------------


def _live_payload(current_item_time_id="it9", included=None, **attrs):
    base_attrs = {"can_control": False, "can_take_control": True, "title": "Sunday", "series_title": "Advent"}
    base_attrs.update(attrs)
    relationships = {"controller": {"data": {"id": "person1", "type": "Person"}}}
    if current_item_time_id is not None:
        relationships["current_item_time"] = {"data": {"id": current_item_time_id, "type": "ItemTime"}}
    return {
        "data": {"id": "live1", "attributes": base_attrs, "relationships": relationships},
        "included": included
        if included is not None
        else [
            {"type": "ItemTime", "id": "it9", "relationships": {"item": {"data": {"id": "item42"}}}},
            {"type": "Person", "id": "person1", "attributes": {"name": "Ada L"}},
        ],
    }


def test_live_status_resolves_item_time_to_plan_item(monkeypatch):
    monkeypatch.setattr(pco_client, "api_get", lambda *a, **k: _live_payload())

    status = pco_client.get_live_status(None, "st1", "p1")
    assert status.current_item_id == "item42"
    assert status.controller_name == "Ada L"
    assert status.can_take_control is True
    assert status.can_control is False
    assert status.reachable is True


def test_live_status_builds_controller_name_from_first_last(monkeypatch):
    payload = _live_payload(
        included=[
            {"type": "ItemTime", "id": "it9", "relationships": {"item": {"data": {"id": "item42"}}}},
            {"type": "Person", "id": "person1", "attributes": {"first_name": "Ada", "last_name": "Lovelace"}},
        ]
    )
    monkeypatch.setattr(pco_client, "api_get", lambda *a, **k: payload)
    assert pco_client.get_live_status(None, "st1", "p1").controller_name == "Ada Lovelace"


def test_live_status_handles_nothing_live_yet(monkeypatch):
    """Before the service starts there's no current_item_time relationship at
    all -- that's 'waiting', not an error."""
    monkeypatch.setattr(pco_client, "api_get", lambda *a, **k: _live_payload(current_item_time_id=None))

    status = pco_client.get_live_status(None, "st1", "p1")
    assert status.current_item_id is None
    assert status.reachable is True


def test_live_status_handles_null_relationship_data(monkeypatch):
    """PCO spells 'empty relationship' as data: null as well as by omitting
    the key entirely."""
    payload = _live_payload()
    payload["data"]["relationships"]["current_item_time"] = {"data": None}
    monkeypatch.setattr(pco_client, "api_get", lambda *a, **k: payload)

    assert pco_client.get_live_status(None, "st1", "p1").current_item_id is None


def test_live_status_survives_missing_included_block(monkeypatch):
    """If the sideload comes back empty the item can't be resolved, but the
    call must still succeed -- the display shows 'waiting', not a 500."""
    monkeypatch.setattr(pco_client, "api_get", lambda *a, **k: _live_payload(included=[]))

    status = pco_client.get_live_status(None, "st1", "p1")
    assert status.current_item_id is None
    assert status.reachable is True


def test_live_status_retries_without_include_when_include_is_rejected(monkeypatch):
    """An unsupported `include` is a 400. Rather than lose the whole feature
    over the controller's name, retry bare."""
    calls = []

    def fake_api_get(session, path, **params):
        calls.append(params)
        if "include" in params:
            raise pco_client.PlanningCenterError("400 Bad Request")
        return _live_payload(included=[])

    monkeypatch.setattr(pco_client, "api_get", fake_api_get)

    status = pco_client.get_live_status(None, "st1", "p1")
    assert status.reachable is True
    assert len(calls) == 2 and "include" not in calls[1]


def test_live_status_reports_unreachable_instead_of_raising(monkeypatch):
    def boom(*a, **k):
        raise pco_client.PlanningCenterError("network is down")

    monkeypatch.setattr(pco_client, "api_get", boom)

    status = pco_client.get_live_status(None, "st1", "p1")
    assert status.reachable is False
    assert "network is down" in status.error


# --------------------------------------------------------------------------
# Control actions -- take/release must be idempotent, because toggle isn't.
# --------------------------------------------------------------------------


def test_take_control_toggles_only_when_not_already_controlling(monkeypatch):
    posts = []
    monkeypatch.setattr(pco_client, "api_post", lambda s, path: posts.append(path))
    monkeypatch.setattr(
        pco_client, "get_live_status", lambda *a, **k: pco_client.LiveStatus(can_control=False)
    )

    pco_client.live_take_control(None, "st1", "p1")
    assert posts == ["/service_types/st1/plans/p1/live/toggle_control"]


def test_take_control_is_a_no_op_when_we_already_have_it(monkeypatch):
    """Double-clicking 'Take control' must not hand control straight back --
    toggle_control is a toggle, so a blind second call would release it."""
    posts = []
    monkeypatch.setattr(pco_client, "api_post", lambda s, path: posts.append(path))
    monkeypatch.setattr(
        pco_client, "get_live_status", lambda *a, **k: pco_client.LiveStatus(can_control=True)
    )

    pco_client.live_take_control(None, "st1", "p1")
    assert posts == []


def test_release_control_is_a_no_op_when_we_do_not_have_it(monkeypatch):
    posts = []
    monkeypatch.setattr(pco_client, "api_post", lambda s, path: posts.append(path))
    monkeypatch.setattr(
        pco_client, "get_live_status", lambda *a, **k: pco_client.LiveStatus(can_control=False)
    )

    pco_client.live_release_control(None, "st1", "p1")
    assert posts == []


def test_control_actions_do_not_touch_pco_when_status_is_unreachable(monkeypatch):
    """No blind writes against an API we just failed to read."""
    posts = []
    monkeypatch.setattr(pco_client, "api_post", lambda s, path: posts.append(path))
    monkeypatch.setattr(
        pco_client, "get_live_status", lambda *a, **k: pco_client.LiveStatus(reachable=False, error="x")
    )

    pco_client.live_take_control(None, "st1", "p1")
    pco_client.live_release_control(None, "st1", "p1")
    assert posts == []


def test_next_and_previous_hit_the_documented_action_paths(monkeypatch):
    posts = []
    monkeypatch.setattr(pco_client, "api_post", lambda s, path: posts.append(path))

    pco_client.live_go_to_next_item(None, "st1", "p1")
    pco_client.live_go_to_previous_item(None, "st1", "p1")
    assert posts == [
        "/service_types/st1/plans/p1/live/go_to_next_item",
        "/service_types/st1/plans/p1/live/go_to_previous_item",
    ]


# --------------------------------------------------------------------------
# collect_songs now has to carry the plan item id through.
# --------------------------------------------------------------------------


def test_collect_songs_carries_the_plan_item_id(monkeypatch):
    item = {
        "id": "item42",
        "attributes": {"item_type": "song", "title": "Song A"},
        "relationships": {"song": {"data": {"id": "s1"}}, "arrangement": {"data": {"id": "a1"}}},
    }
    monkeypatch.setattr(pco_client, "get_plan_items", lambda *a, **k: [item])
    monkeypatch.setattr(
        pco_client, "get_song", lambda *a, **k: {"attributes": {"title": "Song A", "ccli_number": "77"}}
    )
    monkeypatch.setattr(
        pco_client,
        "get_arrangement",
        lambda *a, **k: {"id": "a1", "attributes": {"lyrics": "words", "chord_chart": ""}},
    )

    songs = pco_client.collect_songs(None, "st1", "p1", include_pdf_links=False)
    assert [s.item_id for s in songs] == ["item42"]


def test_collect_songs_keeps_item_id_when_the_song_record_fails_to_load(monkeypatch):
    """The placeholder entry still needs an item id, or the display can never
    match it against what Planning Center says is live."""
    item = {"id": "item42", "attributes": {"item_type": "song", "title": "Song A"}, "relationships": {}}
    monkeypatch.setattr(pco_client, "get_plan_items", lambda *a, **k: [item])

    songs = pco_client.collect_songs(None, "st1", "p1", include_pdf_links=False)
    assert [s.item_id for s in songs] == ["item42"]


def test_plan_item_summaries_include_non_songs_in_order(monkeypatch):
    items = [
        {"id": "i1", "attributes": {"item_type": "item", "title": "Welcome", "sequence": 1}},
        {"id": "i2", "attributes": {"item_type": "song", "title": "Song A", "sequence": 2}},
    ]
    monkeypatch.setattr(pco_client, "get_plan_items", lambda *a, **k: items)

    summaries = pco_client.get_plan_item_summaries(None, "st1", "p1")
    assert [(s.id, s.is_song) for s in summaries] == [("i1", False), ("i2", True)]
