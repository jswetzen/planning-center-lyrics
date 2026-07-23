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
